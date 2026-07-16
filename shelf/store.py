"""SQLite への catalog（notebook/document/chunk）・file_state/meta の永続化を担う境界層。

なぜ Store を独立させるか: SQLite・BLOB シリアライズ・FK 制約という揮発的な詳細を
ここに閉じ込めることで、ShelfService や search.py は「notebook 名・ID・ベクトルと ID
の配列」という単純な形だけを扱えばよくなる（ドメインを SQLite から隔離するポート）。
プロジェクト方針により sqlite3 を import してよいのはこのファイルのみ。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

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
  id          TEXT PRIMARY KEY,          -- {notebook}/{doc_id}#d{n}
  notebook    TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,          -- doc 内の学び連番（0 起点）
  text        TEXT NOT NULL,             -- 学び本文（mask 適用済み）
  source_span TEXT,                      -- 由来（節・ページ範囲等）任意
  source_hash TEXT,                      -- 生成時点の正規化 md ハッシュ（陳腐化検出）
  model       TEXT,                      -- 生成に使ったモデル名（例 qwen3:8b）
  created_at  TEXT NOT NULL,
  UNIQUE(notebook, doc_id, seq)
);
"""


class DuplicateNotebookError(ValueError):
    """create_notebook で既存名を再登録しようとした場合に送出する。"""


class UnknownNotebookError(ValueError):
    """document を存在しない notebook に紐付けようとした場合に送出する。"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        # DB_PATH の親ディレクトリを必要時に作成する（":memory:" はファイルではないのでスキップ）。
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        # documents.notebook の FK 制約を有効化し、「未知 notebook への追加は失敗」を
        # SQLite に守らせる（アプリ側の二重チェックを避ける）。
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_documents_columns()
        self._migrate_notebooks_columns()
        self._migrate_chunks_columns()
        # notebook 単位のベクトル行列キャッシュ: {notebook: (generation, ids, matrix)}。
        # generation が現在値と一致する間は SQLite に再クエリしない（§ ベクタキャッシュ）。
        self._vector_cache: dict[str, tuple[int, list[str], np.ndarray]] = {}

    def close(self) -> None:
        self._conn.close()

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
        try:
            self._conn.execute(
                "INSERT INTO notebooks (name, description, backend, created_at, persona) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, description, backend or "codex", datetime.now(UTC).isoformat(), persona),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateNotebookError(f"notebook '{name}' already exists") from exc
        self._conn.commit()

    def get_notebook(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT name, description, backend, created_at, persona "
            "FROM notebooks WHERE name = ?",
            (name,),
        ).fetchone()
        return None if row is None else dict(row)

    def list_notebooks(self) -> list[dict]:
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
        # persona は shelf persona <nb> <TEXT> で後から設定/更新される可変フィールド
        # （§7-A）。存在しない notebook への設定は upsert_document と同様に事前チェックで
        # 弾き、UnknownNotebookError に変換する（暗黙の no-op を避ける）。
        if self.get_notebook(name) is None:
            raise UnknownNotebookError(f"notebook '{name}' does not exist")
        self._conn.execute("UPDATE notebooks SET persona = ? WHERE name = ?", (persona, name))
        self._conn.commit()

    def delete_notebook(self, name: str) -> None:
        # file_state は notebook 列を持たないため、削除前に該当 notebook の chunks から
        # source_path 一覧を集めておき、chunks 削除後にそれをキーとして file_state も消す。
        source_paths = [
            row["source_path"]
            for row in self._conn.execute(
                "SELECT DISTINCT source_path FROM chunks WHERE notebook = ?", (name,)
            ).fetchall()
        ]
        self._conn.execute("DELETE FROM chunks WHERE notebook = ?", (name,))
        self._conn.execute("DELETE FROM study_notes WHERE notebook = ?", (name,))
        self._conn.execute("DELETE FROM documents WHERE notebook = ?", (name,))
        self._conn.execute("DELETE FROM notebooks WHERE name = ?", (name,))
        for source_path in source_paths:
            self._conn.execute("DELETE FROM file_state WHERE source_file = ?", (source_path,))
        self._bump_generation()
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
        rows = self._conn.execute(
            "SELECT id, notebook FROM documents WHERE origin = ? ORDER BY id",
            (origin,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_document(self, id: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (id,))
        self._conn.execute("DELETE FROM study_notes WHERE doc_id = ?", (id,))
        self._conn.execute("DELETE FROM documents WHERE id = ?", (id,))
        self._bump_generation()
        self._conn.commit()

    # -- chunk -------------------------------------------------------------

    def upsert_chunks(self, rows: list[dict]) -> None:
        """rows の各 dict は id/notebook/doc_id/source_path/seq/text/embedding が必須、
        section/page/kind は省略可（kind 既定 'body'）。dim は embedding の長さから自動算出する。"""
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
        self._conn.commit()

    def delete_by_source_file(self, source_path: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE source_path = ?", (source_path,))
        self._bump_generation()
        self._conn.commit()

    def get_chunk(self, id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT id, notebook, doc_id, source_path, section, page, seq, text, kind
            FROM chunks WHERE id = ?
            """,
            (id,),
        ).fetchone()
        return None if row is None else dict(row)

    # -- study_notes（学びノート・source-of-truth。indexer が kind='digest' チャンク化） ----

    def replace_study_notes(self, notebook: str, doc_id: str, notes: list[dict]) -> None:
        """doc_id の既存学びノートを全削除してから notes を書き込む（再生成の冪等な置換）。

        `shelf digest --force` の再生成や失敗リトライで「前回分が残ったまま重複する」
        ことを避けるため、insert-or-update ではなく delete-then-insert で「今の状態」
        を過不足なく反映する。notes の各 dict は text が必須、source_span/source_hash/
        model は省略可。id は "{notebook}/{doc_id}#d{n}"（n は 0 起点連番）で決定的に
        生成する（§4-A）。
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
            )
            for seq, note in enumerate(notes)
        ]
        if values:
            self._conn.executemany(
                """
                INSERT INTO study_notes
                    (id, notebook, doc_id, seq, text, source_span, source_hash, model,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        self._conn.commit()

    def list_study_notes(self, notebook: str, doc_id: str | None = None) -> list[dict]:
        query = (
            "SELECT id, notebook, doc_id, seq, text, source_span, source_hash, model, "
            "created_at FROM study_notes WHERE notebook = ?"
        )
        params: tuple[str, ...] = (notebook,)
        if doc_id is not None:
            query += " AND doc_id = ?"
            params += (doc_id,)
        query += " ORDER BY doc_id, seq"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def load_vectors(self, notebook: str) -> tuple[list[str], np.ndarray]:
        """cosine 検索用に notebook 内の全ベクトルを1つの行列としてロードする。

        プロセス内キャッシュ（generation 一致時のみ再利用）で、同一世代内の
        繰り返し呼び出しによる不要な SQLite 再クエリを避ける。
        """
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

        self._vector_cache[notebook] = (generation, ids, matrix)
        return ids, matrix

    # -- file_state（recall と同型） -----------------------------------------

    def get_file_state(self, source_file: str) -> dict | None:
        row = self._conn.execute(
            "SELECT mtime, size, model FROM file_state WHERE source_file = ?",
            (source_file,),
        ).fetchone()
        return None if row is None else dict(row)

    def set_file_state(self, source_file: str, mtime: float, size: int, model: str) -> None:
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
        self._conn.execute("DELETE FROM file_state WHERE source_file = ?", (source_file,))
        self._conn.commit()

    def list_source_files(self) -> list[str]:
        rows = self._conn.execute("SELECT source_file FROM file_state").fetchall()
        return [row["source_file"] for row in rows]

    def prune_missing(self, existing_source_files: set[str]) -> int:
        """corpus 上に存在しなくなったファイルの chunks/file_state を削除する。削除件数を返す。"""
        tracked = self.list_source_files()
        stale = [f for f in tracked if f not in existing_source_files]
        for source_file in stale:
            self._conn.execute("DELETE FROM chunks WHERE source_path = ?", (source_file,))
            self._conn.execute("DELETE FROM file_state WHERE source_file = ?", (source_file,))
        if stale:
            self._bump_generation()
        self._conn.commit()
        return len(stale)

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()
