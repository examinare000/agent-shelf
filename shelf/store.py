"""SQLite への catalog（notebook/document/chunk）・file_state/meta の永続化を担う境界層。

なぜ Store を独立させるか: SQLite・BLOB シリアライズ・FK 制約という揮発的な詳細を
ここに閉じ込めることで、ShelfService や search.py は「notebook 名・ID・ベクトルと ID
の配列」という単純な形だけを扱えばよくなる（ドメインを SQLite から隔離するポート）。
プロジェクト方針により sqlite3 を import してよいのはこのファイルのみ。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

_logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
  name        TEXT PRIMARY KEY,
  description TEXT,
  backend     TEXT NOT NULL DEFAULT 'codex',
  created_at  TEXT NOT NULL,
  -- 専門家ペルソナ（system prompt）。NULL = ペルソナなし（ask は従来挙動・互換維持）。
  -- backend へ送信する全テキストは mask 済みという不変条件を保つため、mask 適用後の
  -- 値を保存する（mask 自体は呼び出し側 = service の責務）。
  persona     TEXT
);

CREATE TABLE IF NOT EXISTS documents (
  id              TEXT PRIMARY KEY,
  notebook        TEXT NOT NULL REFERENCES notebooks(name),
  origin          TEXT NOT NULL,
  origin_type     TEXT NOT NULL,
  normalized_path TEXT NOT NULL,
  title           TEXT,
  converter       TEXT NOT NULL,
  content_hash    TEXT,
  added_at        TEXT NOT NULL,
  fetched_at      TEXT,
  -- 資料の説明/要約。--desc 明示指定、または未指定時は codex による自動生成。
  -- 後続の indexer がこれを検索用チャンクとして embed する土台。
  description        TEXT,
  -- description の出所。'user'（--desc明示） | 'auto'（codex自動生成） | NULL（未設定）。
  -- 要約チャンクの section ラベル判定に使う。
  description_source TEXT,
  UNIQUE(notebook, origin)
);

CREATE TABLE IF NOT EXISTS chunks (
  id          TEXT PRIMARY KEY,
  notebook    TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  source_path TEXT NOT NULL,
  section     TEXT,
  page        INTEGER,
  seq         INTEGER NOT NULL,
  text        TEXT NOT NULL,
  embedding   BLOB NOT NULL,
  dim         INTEGER NOT NULL,
  -- チャンク種別。'body'（本文抜粋・既定）| 'summary'（資料概要=既存 seq=-1）
  -- | 'digest'（学びノート）。search/citation/insights の振り分けに使う（indexer が付与）。
  kind        TEXT NOT NULL DEFAULT 'body',
  UNIQUE(notebook, doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_notebook ON chunks(notebook);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id   ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS file_state (
  source_file TEXT PRIMARY KEY,
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  model       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- 学びノートの source-of-truth（再 index で LLM 再呼び出し不要にするため DB に持つ）。
-- 検索対象化は indexer が本テーブルを読み、kind='digest' チャンクとして chunks に
-- upsert する（study_notes 自体は embedding を持たない = ここではただの記録）。
CREATE TABLE IF NOT EXISTS study_notes (
  id               TEXT PRIMARY KEY,     -- {notebook}/{doc_id}#d{n}
  notebook         TEXT NOT NULL,
  doc_id           TEXT NOT NULL,
  seq              INTEGER NOT NULL,     -- doc 内の学び連番（0 起点）
  text             TEXT NOT NULL,        -- 学び本文（mask 適用済み）
  source_span      TEXT,                 -- 由来（節・ページ範囲等）任意
  source_hash      TEXT,                 -- 生成時点の正規化 md ハッシュ（陳腐化検出）
  model            TEXT,                 -- 生成に使ったモデル名（例 qwen3:8b）
  created_at       TEXT NOT NULL,
  -- 代表チャンクの節パンくず・ページ（全文グラウンディング改良: 接地元の人間可読表示）。
  section          TEXT,
  page             INTEGER,
  -- 接地元チャンク id の JSON 配列文字列（例 '["nb/doc#3","nb/doc#5"]'）。
  -- store 層は JSON 文字列で永続化し、list_study_notes で list に復元して返す。
  source_chunk_ids TEXT,
  -- 生成パイプライン版数。旧=1（単発生成・既定）、map-reduce=2。
  pipeline         INTEGER NOT NULL DEFAULT 1,
  UNIQUE(notebook, doc_id, seq)
);

-- 文書タグ（学び抽出パイプラインが文書単位に付与するラベル。notebook 横断の
-- タグ一覧・絞り込み用途）。doc_id 単位の delete-then-insert で管理する。
CREATE TABLE IF NOT EXISTS document_tags (
  doc_id      TEXT NOT NULL,
  notebook    TEXT NOT NULL,
  tag         TEXT NOT NULL,
  PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);
CREATE INDEX IF NOT EXISTS idx_document_tags_notebook ON document_tags(notebook);
"""


class DuplicateNotebookError(ValueError):
    """create_notebook で既存名を再登録しようとした場合に送出する。"""


class UnknownNotebookError(ValueError):
    """document を存在しない notebook に紐付けようとした場合に送出する。"""


class Store:
    # SQLite の busy_timeout(ms)。shelf は長命 MCP サーバ(server.py が Store を
    # プロセス生存中保持)と別プロセスの `shelf index` CLI(cli.py が別 Store)が
    # 同一 DB ファイルへ同時アクセスする構成のため、単発の database is locked を
    # 即座に例外化させず SQLite 自身に自動リトライさせる猶予。これを設定しない
    # と、単発ロックが sqlite3.Error として keyword_topk 等に伝播し、
    # _fts_disable_after_failure がそのプロセスの生存中ずっとハイブリッド検索を
    # 無効化してしまう(サーバ再起動まで回復しない)。
    _BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str | Path) -> None:
        # 後続タスク（MCP ツールの async def + anyio.to_thread 化）で複数ワーカー
        # スレッドが同一 Store を叩く構成になる。単一 sqlite3 接続を
        # check_same_thread=False で共有し、公開メソッド全体を _lock（RLock）で
        # 包んで直列化する（タスク A2 の設計判断）。
        #
        # ロック粒度をメソッド単位より細かくしない理由: mutator はメソッド内で
        # 複数回 commit する（例: upsert_document は INSERT → _bump_generation
        # （内部で set_meta commit）→ 最終 commit）。粒度を細かくすると、途中の
        # commit の隙にスレッド B が割り込み、スレッド A の書きかけの多段書き込み
        # を中途半端な状態のまま確定させてしまう。
        #
        # threading.Lock ではなく RLock にする理由: 公開メソッドが別の公開メソッド
        # を呼ぶ（upsert_document→get_notebook、_bump_generation→get_meta/
        # set_meta、prune_missing→list_source_files 等）ため、再入可能なロックが
        # 必須。
        #
        # fts_enabled・_fts_retry_available・_fts_retry_in_progress（A1 が実装した
        # FTS ラッチ自己修復の状態一式。_fts_disable_after_failure/keyword_topk
        # 参照）も、この _lock の保護範囲に含める（read-modify-write が絡む状態の
        # ため。A1 が __init__ に残した申し送りコメントに従う）。
        self._lock = threading.RLock()
        # ":memory:" はファイルではないため、親ディレクトリ作成・WAL 化のいずれも
        # スキップする対象になる。1変数にまとめて判定を一本化する
        # （コードレビュー指摘: 以前は同じ文字列比較が2箇所に重複していた）。
        is_memory_db = str(db_path) == ":memory:"
        # DB_PATH の親ディレクトリを必要時に作成する（":memory:" はファイルではないのでスキップ）。
        if not is_memory_db:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 同時アクセスによる一時的なロック競合の頻度を下げる（_BUSY_TIMEOUT_MS の
        # クラス変数コメント参照）。_enable_wal より前に設定すること
        # （コードレビュー指摘 must-1b: 以前は _enable_wal の後にこの PRAGMA を
        # 発行していたため、WAL 化の PRAGMA 自体が busy_timeout の恩恵を受けられず、
        # 別接続が書き込みロックを保持しているだけで待たずに即 "database is
        # locked" となっていた）。foreign_keys より前に設定しても問題ない
        # （いずれも接続スコープの PRAGMA）。
        self._conn.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        # WAL 化: rollback-journal(既定)では書き込みトランザクションが読み取りを
        # ブロックするため、`shelf index`/`shelf digest` CLI の書き込みが長命
        # サーバ(shelf serve)の読み取りをブロックしうる。WAL は reader/writer が
        # 互いをブロックしないため、この構成での busy_timeout 頼みの待ち合わせを
        # 減らせる。connect 直後・スキーマ作成前に発行する。:memory: DB は
        # WAL が要求する共有メモリに対応しないためスキップする（journal_mode は
        # "memory" のままで正常動作）。journal_mode は DB ファイルに永続する
        # 属性のため、既存 DB も次回オープンで自動的に WAL 化される
        # （migration スクリプト不要）。
        if not is_memory_db:
            self._enable_wal()
        # documents.notebook の FK 制約を有効化し、「未知 notebook への追加は失敗」を
        # SQLite に守らせる（アプリ側の二重チェックを避ける）。
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_documents_columns()
        self._migrate_notebooks_columns()
        self._migrate_chunks_columns()
        self._migrate_study_notes_columns()
        self._migrate_normalize_path_separators()
        # notebook 単位のベクトル行列キャッシュ: {notebook: (generation, ids, matrix)}。
        # generation が現在値と一致する間は SQLite に再クエリしない（§ ベクタキャッシュ）。
        self._vector_cache: dict[str, tuple[int, list[str], np.ndarray]] = {}
        self.fts_enabled = False
        # FTS ラッチ回復用の状態（_fts_disable_after_failure・keyword_topk 参照）。
        # _fts_retry_available: 一度きりの再試行予算。__init__ 時点では直前まで
        # 有効化された実績がないため False のままにしておく（初回から壊れている
        # 環境でのリトライ無駄撃ちを避ける）。
        # _fts_retry_in_progress: リトライ実行中(_init_fts の再試行〜その呼び出し元
        # keyword_topk 内で続けて行われる実クエリまで)を示す。この期間中に発生した
        # 失敗は「直前まで健全だった」の誤認（コードレビュー指摘 must-2a）を防ぐため
        # 再アームの対象から除外する。
        # この2つのフラグは fts_enabled とセットで _lock（RLock）の保護範囲に
        # 含めている（タスク A2。__init__ 冒頭の _lock 定義のコメント参照）。
        self._fts_retry_available = False
        self._fts_retry_in_progress = False
        self._init_fts()

    def close(self) -> None:
        # 既知の制約: load_vectors は行列構築中に一度ロックを手放すため、その
        # 窓と並行して close() すると再取得後の generation 再確認が閉じた接続に
        # 触れて ProgrammingError になる。close は他スレッドの呼び出しが全て
        # 完了した後にのみ呼ぶこと（サーバ側のライフサイクル配線時の運用契約）。
        with self._lock:
            self._conn.close()

    def _enable_wal(self) -> None:
        """journal_mode=WAL・synchronous=NORMAL を設定する（fail-soft）。

        読み取り専用ファイルシステムやネットワーク共有（例: OneDrive 同期
        フォルダ）上の DB では WAL の要求が無視され、戻り値が "wal" 以外に
        なることがある(mode != "wal" 分岐)。加えて、読み取り専用パーミッション
        のファイルや、別接続が書き込みロックを保持している一過性の競合では
        PRAGMA 発行自体が sqlite3.OperationalError を送出しうる(コードレビュー
        指摘 must-1a/b の回帰: 修正前はこれが __init__ にそのまま伝播し
        Store の構築自体がクラッシュしていた)。既存の FTS 劣化
        （_fts_disable_after_failure）と同じ流儀で、いずれの失敗も例外にせず
        warning ログに留めて rollback-journal のまま処理を継続する。DB は
        次回 open 時に競合が解消していれば自動的に WAL 化される（§ __init__
        のコメント参照、migration スクリプト不要）。
        """
        try:
            mode = self._read_pragma_value("PRAGMA journal_mode=WAL")
            if mode != "wal":
                _logger.warning(
                    "PRAGMA journal_mode=WAL が有効化されませんでした（実際の mode=%r）。"
                    "読み取り専用ファイルシステムやネットワーク共有上の DB では WAL が"
                    "機能しない場合があります。rollback-journal のまま動作を継続します。",
                    mode,
                )
            # WAL 下では FULL ほど厳密でなくとも整合性が保たれるため、fsync 頻度を
            # 減らして書き込みコストを下げる。
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            _logger.warning(
                "WAL 化用の PRAGMA 発行に失敗したため rollback-journal のまま"
                "動作を継続します（読み取り専用ファイル・別接続によるロック競合等の"
                "一過性要因が考えられます。次回 open 時に競合が解消していれば"
                "自動的に再試行されます）: %r",
                exc,
            )

    def _read_pragma_value(self, sql: str) -> str | None:
        """PRAGMA の単一列の戻り値を読む小さな seam。

        なぜ execute から分離するか: テストで「WAL 要求が無視される環境」を
        monkeypatch で模すため（TestFtsInitProbe が _probe_fts を monkeypatch
        する既存流儀と同様）。
        """
        row = self._conn.execute(sql).fetchone()
        return row[0] if row is not None else None

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        # CREATE TABLE IF NOT EXISTS は既存テーブルに列を足さないため、
        # PRAGMA table_info で不足列を検出して ALTER TABLE で追補する（冪等）。
        # row_factory は __init__ で executescript より前に設定済みなので、
        # ここでの table_info の行も row["name"] で参照できる。table は呼び出し元が
        # 固定文字列で渡す内部専用ヘルパのため f-string 直書きでも injection の懸念はない。
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._conn.commit()

    def _migrate_documents_columns(self) -> None:
        self._add_missing_columns(
            "documents", {"description": "TEXT", "description_source": "TEXT"}
        )

    def _migrate_notebooks_columns(self) -> None:
        # 旧スキーマ DB（persona 列なし）を開いても専門家ペルソナ機能が使えるようにする。
        self._add_missing_columns("notebooks", {"persona": "TEXT"})

    def _migrate_chunks_columns(self) -> None:
        # 旧スキーマ DB（kind 列なし）を開いた場合、既存チャンクは全て 'body' として
        # 扱う（§4-A: サマリチャンクの kind='summary' 再付与は次回 index 時）。
        # SQLite の ALTER TABLE ... DEFAULT は既存行にも遡って値を埋めるため、
        # 追加直後から既存行を含めて NOT NULL 制約と既定値が両立する。
        self._add_missing_columns("chunks", {"kind": "TEXT NOT NULL DEFAULT 'body'"})

    def _migrate_study_notes_columns(self) -> None:
        # 旧スキーマ DB（section/page/source_chunk_ids/pipeline 列なし）を開いても
        # チャンク接地・パイプライン版数付き学びノートが使えるようにする。
        # pipeline は kind と同様、ALTER TABLE ... DEFAULT 1 で既存行にも遡って
        # 値を補完する（旧形式=単発生成パイプラインは常に 1）。
        self._add_missing_columns(
            "study_notes",
            {
                "section": "TEXT",
                "page": "INTEGER",
                "source_chunk_ids": "TEXT",
                "pipeline": "INTEGER NOT NULL DEFAULT 1",
            },
        )

    def _migrate_normalize_path_separators(self, _force_windows: bool = False) -> None:
        r"""Windows で構築済みの既存 DB に残る `\` 区切りの
        chunks.source_path / file_state.source_file を POSIX 区切りへ後追いで
        正規化する。放置すると indexer.py の他 notebook 保護（`f"{notebook}/"`
        プレフィックス判定）が旧 `\` 行を吸収できず、`--full` 再索引でも消えない
        prune 不能なゴミとして残り続ける。

        POSIX ではバックスラッシュはファイル名の合法文字であり、無条件 REPLACE は
        正当なファイル名を黙って書き換え prune による削除まで起こしうるため、
        Windows（os.name == "nt"）でのみ実行する。

        chunks.source_path は id が PRIMARY KEY で source_path 自体に一意制約は
        ないため、単純な REPLACE で衝突を気にせず更新できる（id/rowid/text は
        不変なので chunks_fts の追随も不要）。file_state.source_file は
        PRIMARY KEY のため、正規化後のキーが既に存在する場合（修正後のコードで
        新規に posix 行が書かれた後に旧 `\` 行が残っているケース）は UPDATE すると
        PK 衝突するので、その旧行は DELETE で捨てる。

        Args:
            _force_windows: テスト用。True の場合、os.name の値に関わらず Windows 正規化を実行。
        """
        import os

        # POSIX では正当なファイル名のバックスラッシュを保護する
        if not _force_windows and os.name != "nt":
            return

        changed = False

        chunks_updated = self._conn.execute(
            "UPDATE chunks SET source_path = REPLACE(source_path, '\\', '/') "
            "WHERE source_path LIKE '%\\%'"
        ).rowcount
        if chunks_updated > 0:
            changed = True

        legacy_file_states = self._conn.execute(
            "SELECT source_file FROM file_state WHERE source_file LIKE '%\\%'"
        ).fetchall()
        for row in legacy_file_states:
            old_key = row["source_file"]
            new_key = old_key.replace("\\", "/")
            conflict = self._conn.execute(
                "SELECT 1 FROM file_state WHERE source_file = ?", (new_key,)
            ).fetchone()
            if conflict is not None:
                self._conn.execute(
                    "DELETE FROM file_state WHERE source_file = ?", (old_key,)
                )
            else:
                self._conn.execute(
                    "UPDATE file_state SET source_file = ? WHERE source_file = ?",
                    (new_key, old_key),
                )
            changed = True

        if changed:
            # citation の source 表示が変わるため、prune_missing 等と同様に
            # generation を進めてベクタキャッシュを無効化する。
            self._bump_generation()
        self._conn.commit()

    def _init_fts(self) -> None:
        # fts5 の trigram tokenizer は SQLite のビルドオプション次第で使えない
        # 環境があるため、作成に失敗したら fts_enabled=False にフェイルソフトする
        # （キーワード検索は諦めるが、他の永続化機能はブロックしない）。
        # sqlite_master を CREATE 前に見ておくことで「今回新規作成したか」を判定し、
        # 新規作成時かつ chunks に既存データがある場合のみ一括バックフィルする
        # （移行専用。毎起動 rebuild や空テーブルへの rebuild は無駄）。
        already_existed = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
            ).fetchone()
            is not None
        )
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                "text, content='chunks', content_rowid='rowid', tokenize='trigram')"
            )
            # コードレビュー指摘#2: CREATE ... IF NOT EXISTS はテーブルが既存の場合
            # モジュール検証を行わない（USING no_such_module のような壊れた定義でも
            # テーブルが既存なら成功してしまう）。そのため MATCH を実際に実行する
            # プローブクエリで fts5 モジュール + trigram tokenizer の動作を検証する。
            self._probe_fts()
        except sqlite3.Error as exc:
            self._conn.rollback()
            # プローブ失敗時に chunks_fts を DROP しておくことで、次回オープン時に
            # already_existed=False に戻し CREATE+バックフィルを再試行できるようにする
            # （SQLITE_BUSY 等の一過性失敗から回復する手段）。
            try:
                self._conn.execute("DROP TABLE IF EXISTS chunks_fts")
            except sqlite3.Error:
                pass
            # コードレビュー指摘: 初期化時の劣化は _fts_disable_after_failure を
            # 経由させ、fts_enabled=False にする事実を必ず警告ログへ残す
            # （以前は直接代入していたため、DB オープン時に FTS が無効化された
            # ことが運用ログから観測できなかった）。
            self._fts_disable_after_failure("初期化", exc)
            return
        self.fts_enabled = True
        if not already_existed:
            has_chunks = self._conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
            if has_chunks:
                # 旧 DB（chunks_fts 導入前に作られた・chunks に既存データあり）を
                # 開いたときだけの一度きりの移行バックフィル。以後の同期は
                # upsert_chunks/delete_by_source_file/delete_notebook 側の
                # 行単位更新に委ねる（コードレビュー指摘#10: 読み取りパスでの
                # 全コーパス再構築の廃止）。
                try:
                    self._rebuild_fts()
                except sqlite3.Error as exc:
                    # 失敗時 chunks_fts テーブル自体が CREATE 済みのまま残ると、
                    # 次回起動時 already_existed=True となり二度とバックフィルが
                    # 走らず、移行前の既存チャンクが恒久的にキーワード検索から
                    # 漏れる（サイレント劣化）。テーブルごと消しておけば次回起動時
                    # already_existed=False に戻り、CREATE+バックフィルを再試行
                    # できる（自己修復）。DROP 自体の失敗は握り潰す（既に劣化
                    # ルートに入っているため、ここで追加の例外を呼び出し元に
                    # 波及させても得はない）。
                    try:
                        self._conn.execute("DROP TABLE IF EXISTS chunks_fts")
                    except sqlite3.Error:
                        pass
                    self._fts_disable_after_failure("初期化", exc)
        self._conn.commit()

    def _retry_fts_init(self) -> None:
        """FTS ラッチ回復の1回きりの再試行本体（keyword_topk から呼ばれる）。

        コードレビュー指摘 must-2b（backfill 欠落の回帰）: 通常の _init_fts は
        「chunks_fts が既存かどうか(already_existed)」を見て、既存なら
        バックフィルを省略する。しかし chunks_fts の DROP を伴わない一過性失敗
        （MATCH 自体の読み取りエラー等）では、テーブル自体は残存したまま
        fts_enabled=False になるため、通常の _init_fts を再度呼ぶだけだと
        already_existed=True と判定されバックフィルがスキップされてしまう。
        その結果、劣化中（fts_enabled=False の間）に upsert された行が
        chunks_fts に一切同期されないまま復旧し、静かに検索から永久欠落する
        （サイレント劣化）。

        これを避けるため、リトライ時は already_existed の値に関わらず
        chunks_fts を強制的に DROP してから _init_fts を呼び、常に
        CREATE+全件バックフィルの経路を通す。DROP 自体の失敗は握り潰す
        （既に劣化ルートなので追加の例外を波及させる意味がない。それでも
        _init_fts 側の CREATE ... IF NOT EXISTS が最終的な整合性を保証する）。
        """
        try:
            self._conn.execute("DROP TABLE IF EXISTS chunks_fts")
            self._conn.commit()
        except sqlite3.Error:
            pass
        self._init_fts()

    def _probe_fts(self) -> None:
        # 既存テーブルに対する CREATE ... IF NOT EXISTS はモジュール検証を行わない
        # ため、実際に MATCH を実行して fts5 モジュール + trigram tokenizer が
        # 使える環境かどうかを確認する（コードレビュー指摘#2）。結果は使わず、
        # 例外の有無だけを見る。
        self._conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("probe",)
        ).fetchone()

    def _rebuild_fts(self) -> None:
        # _init_fts の一度きりの移行バックフィル専用（コードレビュー指摘#10により
        # keyword_topk からの read-time 遅延呼び出しは廃止）。content= 外部
        # コンテンツテーブルなのでトリガーではなくこの明示 rebuild で追従させる
        # （設計判断: トリガーは使わない方針）。呼び出し元の commit に相乗りする
        # ため、ここ自体ではコミットしない。
        if self.fts_enabled:
            self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    def _fts_disable_after_failure(self, context: str, exc: Exception) -> None:
        # コードレビュー指摘#1: fts5/trigram が壊れた環境（read-only DB・モジュール
        # 消失等）では例外にせず劣化させ、以後の呼び出しでは同じ壊れた経路を
        # 再実行しない。fts_enabled=False にすることで、各メソッド冒頭の
        # `if not self.fts_enabled: return` に自然に短絡し、警告ログも
        # この失敗時の1回だけで済む（毎回ログを連発しない）。
        _logger.warning("chunks_fts の%sに失敗したためキーワード検索を無効化します: %r", context, exc)
        # FTS ラッチ回復: 次回のキーワード検索での1回きりの自己修復リトライ
        # （keyword_topk 参照）を許可するかどうかをここで決める。
        #
        # コードレビュー指摘 must-2a（不変条件違反の回帰）: 当初は
        # 「self.fts_enabled が直前まで True だったか」だけで判定していたが、
        # これはリトライ実行中の失敗を誤って「直前まで健全だった」と誤認して
        # 再アームしてしまう欠陥があった。_init_fts はテーブル作成・probe に
        # 成功した時点で一旦 self.fts_enabled=True を立てるため、その直後の
        # backfill 失敗（または、リトライ成功後に keyword_topk 内で続けて実行
        # される実クエリの失敗）はいずれも self.fts_enabled=True の状態で
        # このメソッドに到達する。障害が持続的な場合、これを毎回再アームすると
        # 毎クエリ無限リトライになってしまう。
        #
        # そのため「直前まで True だったか」に加えて「今がリトライ実行中
        # (_fts_retry_in_progress) ではないか」も条件に加える。リトライ実行中の
        # 失敗からは絶対に再アームしない。次の場合のみ次回リトライを許可する:
        #   - リトライ実行中ではない状態で、直前まで実際に fts_enabled=True で
        #     動いていたものが今回初めて壊れた場合（別プロセスによる
        #     chunks_fts の DROP・MATCH 読み取りの一過性エラー等）
        # 次の場合は意図的にリトライを許可しない（毎クエリ再試行はコストであり、
        # 無限リトライ禁止のため）:
        #   - 初回 __init__ からこの環境で一度も有効化できていない場合
        #     （fts5/trigram 非対応ビルド等の恒久的条件。リトライしても無駄）
        #   - リトライ実行中に発生した失敗（上記の誤再アーム防止）
        self._fts_retry_available = self.fts_enabled and not self._fts_retry_in_progress
        self.fts_enabled = False

    def _fts_capture_rows(self, where_clause: str, params: tuple) -> list[tuple[int, str]]:
        """外部コンテンツ FTS の 'delete' コマンドに必要な (rowid, text) を、
        chunks の上書き/削除より前に退避する（delete コマンドは索引時と同じ
        テキストを渡す必要があるため、上書き後では取得できない）。where_clause は
        呼び出し元（Store 内部メソッド）が固定文字列で渡す内部専用引数であり、
        値は全て params 経由のバインドパラメータになるため f-string 直書きでも
        injection の懸念はない（_add_missing_columns と同様の設計判断）。
        """
        if not self.fts_enabled:
            return []
        try:
            rows = self._conn.execute(
                f"SELECT rowid, text FROM chunks WHERE {where_clause}", params
            ).fetchall()
        except sqlite3.Error as exc:
            self._fts_disable_after_failure("退避読み取り", exc)
            return []
        return [(row["rowid"], row["text"]) for row in rows]

    def _fts_capture_rows_by_ids(self, ids: list[str]) -> list[tuple[int, str]]:
        """id 群に対応する chunks_fts delete 用 (rowid, text) を退避する。
        get_chunks（_GET_CHUNKS_BATCH_SIZE）と同じ分割単位で IN 句を分割する
        （コードレビュー指摘: 無分割の IN (...) は1ファイルのチャンクが多い場合に
        SQLite のバインドパラメータ上限（環境依存）を超えて sqlite3.Error となり、
        _fts_disable_after_failure でキーワード検索全体が静かに停止していた）。
        """
        if not ids:
            return []
        rows: list[tuple[int, str]] = []
        for start in range(0, len(ids), self._GET_CHUNKS_BATCH_SIZE):
            batch = ids[start : start + self._GET_CHUNKS_BATCH_SIZE]
            rows.extend(
                self._fts_capture_rows(f"id IN ({','.join('?' for _ in batch)})", tuple(batch))
            )
        return rows

    def _fts_delete_rows(self, rows: list[tuple[int, str]]) -> None:
        """_fts_capture_rows で退避した (rowid, text) を chunks_fts から削除する。"""
        if not self.fts_enabled or not rows:
            return
        try:
            self._conn.executemany(
                "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', ?, ?)",
                rows,
            )
        except sqlite3.Error as exc:
            self._fts_disable_after_failure("削除同期", exc)

    def _fts_insert_rows(self, ids: list[str]) -> None:
        """id 群に対応する現在の (rowid, text) を chunks から読み直し、
        chunks_fts へ挿入する（upsert 直後に呼ぶことで、新規行・更新行の
        双方を一括で反映できる。更新行は ON CONFLICT DO UPDATE で rowid が
        据え置かれるため、_fts_delete_rows の削除対象と同じ rowid に
        insert し直すことになる）。get_chunks と同じ _GET_CHUNKS_BATCH_SIZE 件
        ごとにバッチ分割する（コードレビュー指摘: IN句バッチ分割欠如）。
        """
        if not self.fts_enabled or not ids:
            return
        for start in range(0, len(ids), self._GET_CHUNKS_BATCH_SIZE):
            if not self.fts_enabled:
                # 前バッチの失敗で _fts_disable_after_failure により既に
                # 無効化されている場合、以降のバッチは実行しない。
                return
            batch = ids[start : start + self._GET_CHUNKS_BATCH_SIZE]
            self._fts_insert_rows_batch(batch)

    def _fts_insert_rows_batch(self, ids: list[str]) -> None:
        """_fts_insert_rows の1バッチ分（最大 _GET_CHUNKS_BATCH_SIZE 件）を処理する。"""
        # placeholders は "?" の個数分の定型文字列で、実データ(ids)は全て
        # 後続のバインドパラメータとして渡すため f-string 直書きでも
        # injection の懸念はない。
        placeholders = ",".join("?" for _ in ids)
        try:
            rows = self._conn.execute(
                f"SELECT rowid, text FROM chunks WHERE id IN ({placeholders})", ids
            ).fetchall()
            self._conn.executemany(
                "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                [(row["rowid"], row["text"]) for row in rows],
            )
        except sqlite3.Error as exc:
            self._fts_disable_after_failure("挿入同期", exc)

    # -- generation（ベクタキャッシュ無効化用カウンタ） ---------------------------

    def _current_generation(self) -> int:
        value = self.get_meta("generation")
        return int(value) if value is not None else 0

    def _bump_generation(self) -> None:
        # 書き込み系メソッド（notebook/document/chunk の変更）は全てここを通し、
        # load_vectors のプロセス内キャッシュを無効化する。
        self.set_meta("generation", str(self._current_generation() + 1))

    # -- notebook ------------------------------------------------------------

    def create_notebook(
        self,
        name: str,
        description: str | None = None,
        backend: str | None = None,
        persona: str | None = None,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO notebooks (name, description, backend, created_at, persona) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        name,
                        description,
                        backend or "codex",
                        datetime.now(UTC).isoformat(),
                        persona,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateNotebookError(f"notebook '{name}' already exists") from exc
            self._conn.commit()

    def get_notebook(self, name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, description, backend, created_at, persona "
                "FROM notebooks WHERE name = ?",
                (name,),
            ).fetchone()
            return None if row is None else dict(row)

    def list_notebooks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT n.name, n.description, n.backend, n.created_at, n.persona,
                       (SELECT COUNT(*) FROM documents d WHERE d.notebook = n.name) AS documents,
                       (SELECT COUNT(*) FROM chunks c WHERE c.notebook = n.name) AS chunks
                FROM notebooks n
                ORDER BY n.name
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def set_persona(self, name: str, persona: str | None) -> None:
        with self._lock:
            # persona は shelf persona <nb> <TEXT> で後から設定/更新される可変フィールド
            # （§7-A）。存在しない notebook への設定は upsert_document と同様に事前チェックで
            # 弾き、UnknownNotebookError に変換する（暗黙の no-op を避ける）。
            if self.get_notebook(name) is None:
                raise UnknownNotebookError(f"notebook '{name}' does not exist")
            self._conn.execute(
                "UPDATE notebooks SET persona = ? WHERE name = ?", (persona, name)
            )
            self._conn.commit()

    def delete_notebook(self, name: str) -> None:
        with self._lock:
            # file_state は notebook 列を持たないため、削除前に該当 notebook の chunks から
            # source_path 一覧を集めておき、chunks 削除後にそれをキーとして file_state も消す。
            source_paths = [
                row["source_path"]
                for row in self._conn.execute(
                    "SELECT DISTINCT source_path FROM chunks WHERE notebook = ?", (name,)
                ).fetchall()
            ]
            # コードレビュー指摘#10: notebook 削除でも chunks_fts の該当行を行単位で
            # 削除する（他の chunk 削除経路と同様、上書き前に (rowid, text) を退避）。
            old_fts_rows = self._fts_capture_rows("notebook = ?", (name,))
            self._conn.execute("DELETE FROM chunks WHERE notebook = ?", (name,))
            self._conn.execute("DELETE FROM study_notes WHERE notebook = ?", (name,))
            self._conn.execute("DELETE FROM document_tags WHERE notebook = ?", (name,))
            self._conn.execute("DELETE FROM documents WHERE notebook = ?", (name,))
            self._conn.execute("DELETE FROM notebooks WHERE name = ?", (name,))
            for source_path in source_paths:
                self._conn.execute(
                    "DELETE FROM file_state WHERE source_file = ?", (source_path,)
                )
            self._bump_generation()
            self._fts_delete_rows(old_fts_rows)
            self._conn.commit()

    # -- document --------------------------------------------------------------

    def upsert_document(
        self,
        *,
        id: str,
        notebook: str,
        origin: str,
        origin_type: str,
        normalized_path: str,
        converter: str,
        added_at: str,
        title: str | None = None,
        content_hash: str | None = None,
        fetched_at: str | None = None,
        description: str | None = None,
        description_source: str | None = None,
    ) -> None:
        with self._lock:
            # notebook 存在確認を INSERT 前の明示チェックとして行う（中位指摘#3）。
            # 従来は INSERT を try/except sqlite3.IntegrityError で包み、FK 違反を
            # UnknownNotebookError に変換していたが、この except は UNIQUE(notebook, origin)
            # 制約違反も無差別に捕捉してしまい、「notebook が存在しない」という誤った
            # 診断になっていた。事前チェックで FK 違反経路を切り離せば、INSERT 自体は
            # try/except なしで実行でき、真の UNIQUE 違反は素の sqlite3.IntegrityError
            # として呼び出し側に伝わる。
            if self.get_notebook(notebook) is None:
                raise UnknownNotebookError(f"notebook '{notebook}' does not exist")

            self._conn.execute(
                """
                INSERT INTO documents
                    (id, notebook, origin, origin_type, normalized_path, title,
                     converter, content_hash, added_at, fetched_at,
                     description, description_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    notebook=excluded.notebook,
                    origin=excluded.origin,
                    origin_type=excluded.origin_type,
                    normalized_path=excluded.normalized_path,
                    title=excluded.title,
                    converter=excluded.converter,
                    content_hash=excluded.content_hash,
                    added_at=excluded.added_at,
                    fetched_at=excluded.fetched_at,
                    description=excluded.description,
                    description_source=excluded.description_source
                """,
                (
                    id, notebook, origin, origin_type, normalized_path, title,
                    converter, content_hash, added_at, fetched_at,
                    description, description_source,
                ),
            )
            self._bump_generation()
            self._conn.commit()

    def get_document(self, id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, notebook, origin, origin_type, normalized_path, title,
                       converter, content_hash, added_at, fetched_at,
                       description, description_source
                FROM documents WHERE id = ?
                """,
                (id,),
            ).fetchone()
            return None if row is None else dict(row)

    def list_documents(self, notebook: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, notebook, origin, origin_type, normalized_path, title,
                       converter, content_hash, added_at, fetched_at,
                       description, description_source
                FROM documents WHERE notebook = ? ORDER BY id
                """,
                (notebook,),
            ).fetchall()
            return [dict(row) for row in rows]

    def find_documents_by_origin(self, origin: str) -> list[dict]:
        """origin（resolve 済み絶対パス文字列）で全 notebook を横断検索する。

        `shelf shelve` の既投入スキップ判定（設計書 §13.7）専用の read。
        list_documents は notebook 単位のフィルタだが、こちらは notebook を
        引数に取らず全表走査する——同一 origin が別 notebook に投入済みかどうかも
        検出する必要があるため（notebook 跨ぎの重複投入・再分類ドリフト防止）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, notebook FROM documents WHERE origin = ? ORDER BY id",
                (origin,),
            ).fetchall()
            return [dict(row) for row in rows]

    def find_documents_by_content_hash(
        self, content_hash: str, *, exclude_doc_id: str | None = None
    ) -> list[dict]:
        """content_hash（変換後 markdown の sha256）で全 notebook を横断検索する。

        find_documents_by_origin と同じ設計（notebook 引数を取らず全表走査）——
        同一内容の資料が別パス・別 notebook から投入された場合も検出する必要が
        あるため（B3: notebook 跨ぎの内容重複検出）。exclude_doc_id は「自分自身を
        除いた重複」を返したい呼び出し元（add_source の重複警告）向け。
        content_hash が NULL（未計算・バックフィル前）の行は、SQL の NULL 比較
        セマンティクス（`= ?` は NULL に対して常に偽）により自然に除外される。
        """
        query = "SELECT id, notebook FROM documents WHERE content_hash = ?"
        params: list[str] = [content_hash]
        if exclude_doc_id is not None:
            query += " AND id != ?"
            params.append(exclude_doc_id)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def delete_document(self, id: str) -> None:
        with self._lock:
            old_fts_rows = self._fts_capture_rows("doc_id = ?", (id,))
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (id,))
            self._fts_delete_rows(old_fts_rows)
            self._conn.execute("DELETE FROM study_notes WHERE doc_id = ?", (id,))
            self._conn.execute("DELETE FROM document_tags WHERE doc_id = ?", (id,))
            self._conn.execute("DELETE FROM documents WHERE id = ?", (id,))
            self._bump_generation()
            self._conn.commit()

    # -- chunk -------------------------------------------------------------

    def upsert_chunks(self, rows: list[dict]) -> None:
        """rows の各 dict は id/notebook/doc_id/source_path/seq/text/embedding が必須、
        section/page/kind は省略可（kind 既定 'body'）。dim は embedding の長さから自動算出する。"""
        with self._lock:
            ids = [row["id"] for row in rows]
            # コードレビュー指摘#10: 書き込み時に chunks_fts を行単位で同期することで、
            # 読み取りパス（keyword_topk）での全コーパス再構築を不要にする。既存 id への
            # 上書き（ON CONFLICT DO UPDATE）は旧テキストの索引を残さないよう、上書き前に
            # (rowid, text) を退避しておく（新規 id は何もヒットせず、退避は空になる）。
            # _fts_capture_rows_by_ids が get_chunks と同じ単位で IN 句を分割する。
            old_fts_rows = self._fts_capture_rows_by_ids(ids)
            values = [
                (
                    row["id"],
                    row["notebook"],
                    row["doc_id"],
                    row["source_path"],
                    row.get("section"),
                    row.get("page"),
                    row["seq"],
                    row["text"],
                    np.asarray(row["embedding"], dtype=np.float32).tobytes(),
                    len(row["embedding"]),
                    row.get("kind", "body"),
                )
                for row in rows
            ]
            self._conn.executemany(
                """
                INSERT INTO chunks
                    (id, notebook, doc_id, source_path, section, page, seq, text, embedding, dim,
                     kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    notebook=excluded.notebook,
                    doc_id=excluded.doc_id,
                    source_path=excluded.source_path,
                    section=excluded.section,
                    page=excluded.page,
                    seq=excluded.seq,
                    text=excluded.text,
                    embedding=excluded.embedding,
                    dim=excluded.dim,
                    kind=excluded.kind
                """,
                values,
            )
            self._bump_generation()
            # 上書き前の旧索引を消してから、上書き後の現在値で入れ直す。新規 id は
            # old_fts_rows に含まれないため単純追加になる。
            self._fts_delete_rows(old_fts_rows)
            self._fts_insert_rows(ids)
            self._conn.commit()

    def delete_by_source_file(self, source_path: str) -> None:
        with self._lock:
            old_fts_rows = self._fts_capture_rows("source_path = ?", (source_path,))
            self._conn.execute("DELETE FROM chunks WHERE source_path = ?", (source_path,))
            self._bump_generation()
            self._fts_delete_rows(old_fts_rows)
            self._conn.commit()

    def get_chunk(self, id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, notebook, doc_id, source_path, section, page, seq, text, kind
                FROM chunks WHERE id = ?
                """,
                (id,),
            ).fetchone()
            return None if row is None else dict(row)

    _GET_CHUNKS_BATCH_SIZE = 500  # SQLite バインドパラメータ上限を踏まえた分割単位。

    def get_chunks(self, ids: Sequence[str]) -> list[dict]:
        """ids に対応するチャンクをまとめて取得し、入力 ids の順序へ並べ替えて返す
        （存在しない id はスキップ）。

        service._load_chunks が get_chunk を id ごとに N 回呼ぶ N+1（ハイブリッド
        検索の候補プールは最大 2×top_k 件あり2倍化する）を1クエリへ集約する
        （コードレビュー指摘#11）。SQLite のバインドパラメータ数には上限がある
        ため _GET_CHUNKS_BATCH_SIZE 件ごとにクエリを分割する（呼び出し実態の
        プールは小さいため通常は1回で完結する）。
        """
        if not ids:
            return []
        with self._lock:
            rows_by_id: dict[str, dict] = {}
            for start in range(0, len(ids), self._GET_CHUNKS_BATCH_SIZE):
                batch = ids[start : start + self._GET_CHUNKS_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"""
                    SELECT id, notebook, doc_id, source_path, section, page, seq, text, kind
                    FROM chunks WHERE id IN ({placeholders})
                    """,
                    tuple(batch),
                ).fetchall()
                for row in rows:
                    rows_by_id[row["id"]] = dict(row)
            return [rows_by_id[id_] for id_ in ids if id_ in rows_by_id]

    def list_chunks(self, notebook: str, doc_id: str, *, kind: str = "body") -> list[dict]:
        """doc_id 内の指定 kind のチャンクを seq 昇順で返す（map-reduce 学び抽出の入力用）。

        id 辞書順ではなく seq 昇順で返す必要がある: digests.group_into_windows が
        隣接チャンクの連続性（同じ節が固まっていること）を前提にウィンドウ境界を
        判定するため、chunker.py が付与した元の並び順を保つ（get_chunk が単一 id
        取得なのに対し、こちらは 1 doc 分をまとめて読む用途）。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, section, page, seq, text
                FROM chunks
                WHERE notebook = ? AND doc_id = ? AND kind = ?
                ORDER BY seq
                """,
                (notebook, doc_id, kind),
            ).fetchall()
            return [dict(row) for row in rows]

    def keyword_topk(self, notebook: str, fts_query: str, limit: int) -> list[tuple[str, float]]:
        """全文グラウンディング改良のハイブリッド検索用キーワード索引。

        `(chunk_id, bm25スコア)` のリストを bm25 昇順（値が小さいほど良い一致＝
        fts5 の仕様）で返す。fts_enabled=False（fts5/trigram 非対応環境）・
        空/空白のみのクエリ・不正な MATCH 構文（ユーザー由来の生クエリなので
        例外にせず劣化させる）・read-only DB やモジュール消失等の実行時エラーは
        全て `[]` を返す（呼び出し側=search.py がキーワード検索を諦めてベクタ検索
        のみにフォールバックできるように）。chunks_fts の同期は upsert_chunks 等の
        書き込み経路側で完了済みのため、ここでは読み取りのみを行う（コードレビュー
        指摘#10）。

        FTS ラッチ回復: 長命 MCP サーバ(shelf serve)のプロセス生存中に一過性の要因
        （別プロセスによる chunks_fts の DROP、MATCH 読み取り自体の一時的エラー等）
        で fts_enabled=False に落ちた場合、サーバ再起動なしでは永久にキーワード
        検索を失う。これを避けるため、直前まで有効だったものが今回初めて壊れた
        場合に限り(_fts_disable_after_failure 参照)、劣化後最初のこの呼び出しで
        chunks_fts を強制的に作り直して(_retry_fts_init)1回だけ再試行する。
        already_existed に関わらず常に全件バックフィルする設計のため、劣化中
        （fts_enabled=False の間）に upsert された行も復旧時に取りこぼさない
        （コードレビュー指摘 must-2b）。この1回きりの再試行の実行中に発生した
        失敗（backfill 自体の失敗・成功直後にこの呼び出し内で続けて実行される
        実クエリの失敗のいずれも）は「直前まで健全だった」と誤認されないよう
        再アームされない（コードレビュー指摘 must-2a）。毎クエリ再試行はコスト
        なので、この予算は再試行の成否に関わらず使い切りで、以後は無限リトライ
        しない。
        """
        with self._lock:
            retried = not self.fts_enabled and self._fts_retry_available
            if retried:
                self._fts_retry_available = False
                self._fts_retry_in_progress = True
            try:
                if retried:
                    self._retry_fts_init()
                if not self.fts_enabled:
                    return []
                if not fts_query.strip():
                    return []
                try:
                    rows = self._conn.execute(
                        """
                        SELECT c.id AS id, bm25(chunks_fts) AS score
                        FROM chunks_fts
                        JOIN chunks c ON c.rowid = chunks_fts.rowid
                        WHERE chunks_fts MATCH ? AND c.notebook = ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts_query, notebook, limit),
                    ).fetchall()
                except sqlite3.Error as exc:
                    # コードレビュー指摘#1: MATCH SELECT 自体の失敗（read-only DB・
                    # fts5 モジュール消失等）も例外にせず劣化させる。以前は書き込みを
                    # 伴う遅延同期がこの try/except の外側にあり、この契約を破っていた。
                    self._fts_disable_after_failure("読み取り", exc)
                    return []
                return [(row["id"], row["score"]) for row in rows]
            finally:
                # リトライ実行中フラグは、この呼び出し内で発生した失敗（backfill
                # 失敗・成功直後の実クエリ失敗）が _fts_disable_after_failure で
                # 誤って再アームされないためのガード。この呼び出しを抜けたら
                # 次回以降は通常の（新規の失敗のみ再アームする）判定に戻す。
                if retried:
                    self._fts_retry_in_progress = False

    # -- document_tags（文書タグ。学び抽出パイプラインが付与） -------------------

    def replace_document_tags(self, notebook: str, doc_id: str, tags: list[str]) -> None:
        """doc_id の既存タグを全削除してから tags を書き込む（replace_study_notes と
        同じ delete-then-insert の流儀。再生成時に前回分が残留しないようにする）。

        notes と同時に更新したい呼び出し元は replace_study_notes_and_tags を使うこと
        （該当メソッドの docstring 参照）。
        """
        with self._lock:
            self._replace_document_tags_no_commit(notebook, doc_id, tags)
            self._conn.commit()

    def _replace_document_tags_no_commit(
        self, notebook: str, doc_id: str, tags: list[str]
    ) -> None:
        """replace_document_tags の本体（コミットなし）。replace_study_notes_and_tags
        と共有する。"""
        self._conn.execute("DELETE FROM document_tags WHERE doc_id = ?", (doc_id,))
        if tags:
            self._conn.executemany(
                "INSERT INTO document_tags (doc_id, notebook, tag) VALUES (?, ?, ?)",
                [(doc_id, notebook, tag) for tag in tags],
            )

    def replace_study_notes_and_tags(
        self, notebook: str, doc_id: str, notes: list[dict], tags: list[str]
    ) -> None:
        """study_notes と document_tags を単一トランザクション（1コミット）で更新する。

        service._digest_one の reduce フェーズ完了後は notes（新 pipeline・新
        source_hash）と tags を必ず両方セットで確定させる必要がある。
        replace_study_notes()/replace_document_tags() を別々に呼ぶ2段書き込みだと、
        1段目（notes）成功後に2段目（tags）が失敗した場合、notes は新 pipeline・
        新 source_hash で確定するのに tags だけ古いまま残り、以後の skip 判定
        （source_hash と pipeline の一致のみを見る。tags は見ない）が
        再生成不要と誤判定し続け、--force なしでは自己修復しない恒久劣化バグに
        なる（コードレビュー指摘）。delete_notebook/delete_document が複数テーブルを
        1コミットで更新するのと同じ流儀に揃え、途中で例外が起きた場合は
        rollback() して呼び出し前の状態を保つ（全体アトミック）。
        """
        with self._lock:
            try:
                self._replace_study_notes_no_commit(notebook, doc_id, notes)
                self._replace_document_tags_no_commit(notebook, doc_id, tags)
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()

    def list_document_tags(self, notebook: str, doc_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag FROM document_tags WHERE notebook = ? AND doc_id = ? ORDER BY tag",
                (notebook, doc_id),
            ).fetchall()
            return [row["tag"] for row in rows]

    def list_tags_by_notebook(self, limit_per_notebook: int = 15) -> dict[str, list[str]]:
        """notebook ごとに、タグが付いた doc 数の降順（同数はタグ名昇順で決定的に）で
        上位 limit_per_notebook 件のタグ名を返す。UI のタグ一覧・絞り込み候補表示用。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT notebook, tag, COUNT(DISTINCT doc_id) AS doc_count
                FROM document_tags
                GROUP BY notebook, tag
                ORDER BY notebook, doc_count DESC, tag ASC
                """
            ).fetchall()
            result: dict[str, list[str]] = {}
            for row in rows:
                bucket = result.setdefault(row["notebook"], [])
                if len(bucket) < limit_per_notebook:
                    bucket.append(row["tag"])
            return result

    def list_notebook_tags(self, notebook: str) -> list[str]:
        """notebook 内の distinct タグ名をタグ名昇順で、上限なしで返す。

        list_tags_by_notebook の limit_per_notebook=15 は UI 一覧表示専用の
        docstring 契約であり、digest() の reduce プロンプトへ渡すタグ再利用
        カタログにこれを流用すると15件超のタグが暗黙に切り捨てられる不具合
        だった（コードレビュー指摘#14）。専用APIとして分離し、呼び出し元は
        用途に応じて list_tags_by_notebook（UI/カタログ）とこちら
        （reduce タグカタログ）を使い分ける。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT tag FROM document_tags WHERE notebook = ? ORDER BY tag",
                (notebook,),
            ).fetchall()
            return [row["tag"] for row in rows]

    # -- study_notes（学びノート・source-of-truth。indexer が kind='digest' チャンク化） ----

    def replace_study_notes(self, notebook: str, doc_id: str, notes: list[dict]) -> None:
        """doc_id の既存学びノートを全削除してから notes を書き込む（再生成の冪等な置換）。

        `shelf digest --force` の再生成や失敗リトライで「前回分が残ったまま重複する」
        ことを避けるため、insert-or-update ではなく delete-then-insert で「今の状態」
        を過不足なく反映する。notes の各 dict は text が必須、source_span/source_hash/
        model/section/page/source_chunk_ids/pipeline は省略可（既存呼び出し元は
        section 以降を渡さない旧形式 dict のままでよい＝後方互換）。id は
        "{notebook}/{doc_id}#d{n}"（n は 0 起点連番）で決定的に生成する（§4-A）。
        source_chunk_ids は接地元チャンク id の list[str] を渡す（store 層で
        JSON 文字列に変換して永続化する。呼び出し側=service は list のまま
        扱えばよい）。

        notes と document_tags を同時に更新したい呼び出し元（service._digest_one）は
        このメソッドを単独で使わず replace_study_notes_and_tags を使うこと
        （1コミットで両方書かないと、片方だけ成功した場合に自己修復不能な
        不整合が残る。該当メソッドのdocstring参照）。
        """
        with self._lock:
            self._replace_study_notes_no_commit(notebook, doc_id, notes)
            self._conn.commit()

    def _replace_study_notes_no_commit(
        self, notebook: str, doc_id: str, notes: list[dict]
    ) -> None:
        """replace_study_notes の本体（コミットなし）。呼び出し元がトランザクション
        境界を制御できるよう分離する（replace_study_notes_and_tags と共有）。
        """
        self._conn.execute(
            "DELETE FROM study_notes WHERE notebook = ? AND doc_id = ?", (notebook, doc_id)
        )
        created_at = datetime.now(UTC).isoformat()
        values = [
            (
                f"{notebook}/{doc_id}#d{seq}",
                notebook,
                doc_id,
                seq,
                note["text"],
                note.get("source_span"),
                note.get("source_hash"),
                note.get("model"),
                created_at,
                note.get("section"),
                note.get("page"),
                json.dumps(note["source_chunk_ids"])
                if note.get("source_chunk_ids") is not None
                else None,
                note.get("pipeline") if note.get("pipeline") is not None else 1,
            )
            for seq, note in enumerate(notes)
        ]
        if values:
            self._conn.executemany(
                """
                INSERT INTO study_notes
                    (id, notebook, doc_id, seq, text, source_span, source_hash, model,
                     created_at, section, page, source_chunk_ids, pipeline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def list_study_notes(self, notebook: str, doc_id: str | None = None) -> list[dict]:
        with self._lock:
            query = (
                "SELECT id, notebook, doc_id, seq, text, source_span, source_hash, model, "
                "created_at, section, page, source_chunk_ids, pipeline "
                "FROM study_notes WHERE notebook = ?"
            )
            params: tuple[str, ...] = (notebook,)
            if doc_id is not None:
                query += " AND doc_id = ?"
                params += (doc_id,)
            query += " ORDER BY doc_id, seq"
            rows = self._conn.execute(query, params).fetchall()
            notes = []
            for row in rows:
                note = dict(row)
                # DB 上は JSON 文字列で持つ（sqlite3 が list を直接扱えないため）。
                # 呼び出し側は list[str] | None として素直に扱えるようここで復元する。
                raw_chunk_ids = note["source_chunk_ids"]
                note["source_chunk_ids"] = json.loads(raw_chunk_ids) if raw_chunk_ids else None
                notes.append(note)
            return notes

    def load_vectors(self, notebook: str) -> tuple[list[str], np.ndarray]:
        """cosine 検索用に notebook 内の全ベクトルを1つの行列としてロードする。

        プロセス内キャッシュ（generation 一致時のみ再利用）で、同一世代内の
        繰り返し呼び出しによる不要な SQLite 再クエリを避ける。

        行列の materialize（np.frombuffer のループ）はロック外で行う: 別プロセス
        の `shelf index` 実行中は generation バンプにより毎回キャッシュ再構築が
        走るため、この行列構築までロック内に置くと ask() が全リクエスト直列化
        してしまう（設計判断・タスク A2）。行フェッチのみロック内で行い、フェッチ
        後にロックを一旦手放して行列を組み立て、再度ロックを取ってキャッシュへ
        書き戻す。書き戻し時は generation が構築開始時から変わっていないかを
        再確認し、変わっていれば（別スレッドの書き込みが割り込んだ）古い行列を
        キャッシュしない（キャッシュ汚染防止）。
        """
        with self._lock:
            generation = self._current_generation()
            cached = self._vector_cache.get(notebook)
            if cached is not None and cached[0] == generation:
                return cached[1], cached[2]

            rows = self._conn.execute(
                "SELECT id, embedding, dim FROM chunks WHERE notebook = ? ORDER BY id",
                (notebook,),
            ).fetchall()

        if not rows:
            ids, matrix = [], np.zeros((0, 0), dtype=np.float32)
        else:
            ids = [row["id"] for row in rows]
            dim = rows[0]["dim"]
            matrix = np.zeros((len(rows), dim), dtype=np.float32)
            for i, row in enumerate(rows):
                matrix[i] = np.frombuffer(row["embedding"], dtype=np.float32)

        with self._lock:
            if self._current_generation() == generation:
                self._vector_cache[notebook] = (generation, ids, matrix)
        return ids, matrix

    # -- file_state（recall と同型） -----------------------------------------

    def get_file_state(self, source_file: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime, size, model FROM file_state WHERE source_file = ?",
                (source_file,),
            ).fetchone()
            return None if row is None else dict(row)

    def set_file_state(self, source_file: str, mtime: float, size: int, model: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO file_state (source_file, mtime, size, model) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_file) DO UPDATE SET mtime=excluded.mtime,
                    size=excluded.size, model=excluded.model
                """,
                (source_file, mtime, size, model),
            )
            self._conn.commit()

    def delete_file_state(self, source_file: str) -> None:
        """source_file の file_state 行を削除する(次回 index_notebook を early-skip
        させず強制的に再処理させるための狙い撃ち無効化。service.digest() が
        study_notes 更新後に使う)。存在しない source_file を渡しても例外にしない
        (DELETE は該当行0件でもエラーにならない SQL の性質どおり)。
        """
        with self._lock:
            self._conn.execute("DELETE FROM file_state WHERE source_file = ?", (source_file,))
            self._conn.commit()

    def list_source_files(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT source_file FROM file_state").fetchall()
            return [row["source_file"] for row in rows]

    def prune_missing(self, existing_source_files: set[str]) -> int:
        """corpus 上に存在しなくなったファイルの chunks/file_state を削除する。削除件数を返す。"""
        with self._lock:
            tracked = self.list_source_files()
            stale = [f for f in tracked if f not in existing_source_files]
            for source_file in stale:
                old_fts_rows = self._fts_capture_rows("source_path = ?", (source_file,))
                self._conn.execute("DELETE FROM chunks WHERE source_path = ?", (source_file,))
                self._fts_delete_rows(old_fts_rows)
                self._conn.execute(
                    "DELETE FROM file_state WHERE source_file = ?", (source_file,)
                )
            if stale:
                self._bump_generation()
            self._conn.commit()
            return len(stale)

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()
