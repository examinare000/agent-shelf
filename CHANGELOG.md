# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **SQLite WAL 化**: shelf は長命 MCP サーバ(`shelf serve`)と別プロセスの CLI(`shelf index`/`shelf digest`)が同一 DB ファイルへ同時アクセスする構成のため、既定の rollback-journal では CLI の書き込みトランザクションがサーバの読み取りをブロックしていた（`busy_timeout` 頼みの待ち合わせのみ）。`Store.__init__` で `PRAGMA busy_timeout` の設定直後・スキーマ作成前に `PRAGMA journal_mode=WAL`・`PRAGMA synchronous=NORMAL` を発行し、reader/writer が互いをブロックしない WAL モードへ移行。journal_mode は DB ファイルに永続する属性のため、既存 DB も新コードで開くだけで自動的に WAL 化される（migration スクリプト不要）。読み取り専用ファイルシステムやネットワーク共有等で WAL が有効化できない場合や、これらの PRAGMA 発行自体が読み取り専用パーミッション・別接続との書き込みロック競合で `sqlite3.OperationalError` を送出する場合も、例外にせず warning ログへフェイルソフトし rollback-journal のまま起動を継続する（既存の FTS 劣化と同じ流儀）。次回 open 時に競合が解消していれば自動的に WAL 化される
  - **注意**: WAL モードでは DB ファイル本体に加えて `-wal`・`-shm` のサイドカーファイルが増える。DB を OneDrive 等のクラウド同期フォルダやネットワーク共有（SMB/NFS）に置いている場合、WAL が要求する共有メモリ・ロック機構が動作せず機能しないことがある（その場合は自動的に rollback-journal のままフェイルソフトする）。バックアップを取る際は `-wal`・`-shm` を含めたサイドカー込みでコピーするか、整合性の取れた単一ファイルを得られる `VACUUM INTO` を推奨する
- **FTS ラッチの自己修復リトライ**: 別プロセスによる `chunks_fts` の DROP や MATCH 読み取り自体の一過性エラー等でキーワード索引が壊れ `fts_enabled=False` に落ちた場合、従来は長命サーバのプロセス再起動までハイブリッド検索を喪失していた。直前まで有効だった FTS が今回初めて壊れた場合に限り、劣化後最初の `keyword_topk` 呼び出しで `chunks_fts` を強制的に作り直して(DROP+全件バックフィル)1回だけ再試行するようにした。already_existed の判定に関わらず常に全件バックフィルするため、劣化中に upsert された行も復旧時に取りこぼさない。この再試行の実行中に発生した失敗（backfill 自体の失敗・成功直後に続けて実行される実クエリの失敗）は「直前まで健全だった」と誤認されず再アームされない（障害が持続的でも毎クエリ再試行にはならない）。毎クエリ再試行はコストのため予算は使い切りで、初回から fts5/trigram 非対応の環境など直前まで一度も有効化できていない場合は無駄なリトライを行わない
- **content_hash 記録 + notebook 横断の内容重複検出**: `documents.content_hash` はスキーマ・`upsert_document` 双方で対応済みだったが `_ingest_file` が渡していなかったため常に NULL だった。変換後 markdown の sha256（`_content_hash_of`、digest の source_hash 計算と共有）を計算して記録し、`add_source`/`add_directory` の応答へ同一内容の既存資料を `duplicates: [{doc_id, notebook}]` として additive に警告表示する（空なら省略・既存の notes と同じ流儀）。UX は warn + 記録のみで skip はしない（同一書籍を複数の棚に意図的に置く運用は正当なため）。`Store.find_documents_by_content_hash` を新設（notebook を跨いだ全表検索。既存行の backfill は再 add で自然に埋まる。一括 backfill コマンドはスコープ外）

### Migration Notes
- **既存の予約名 notebook のロックアウト**: 本リリースで Windows 予約デバイス名
  （con/prn/aux/nul/com1-9/lpt1-9、大文字小文字不問）が notebook 名として拒否されるようになった。
  本修正の適用前にこれらの名前で notebook を作成済みの場合、ask/add/digest/persona などの
  操作は notebook 名検証で以後すべて弾かれ、データを直接救済する手段はない。対応が必要な場合は
  `shelf rm <予約名の notebook>` で削除したうえで、別名で資料を再投入すること。

## [0.4.3] - 2026-07-25

personal 環境への grounded digest 還流時のレビューで発見された 3 件の堅牢化を逆移植。

### Fixed
- **notes/tags の非原子 2 段書き込み**: `_digest_one` が `replace_study_notes` → `replace_document_tags` を独立 commit で連続実行しており、1 段目成功後に 2 段目が失敗すると notes は pipeline=2 + 新 source_hash・tags は古いままになり、skip 判定が以後 skipped を返して自己修復しなかった。`replace_study_notes_and_tags`（単一トランザクション・失敗時全体ロールバック）へ統合
- **タグ正規化の mask 順序バグ**: normalize → mask の順序では mask 出力（`<REDACTED-KEY>` 等）が許可文字集合を破って DB へ保存されていた。`normalize_tag`/`normalize_tags`/`parse_reduce` へ mask を additive パラメータとして注入し、mask → 正規化の順を保証（mask 後の事後 dedup コードは正規化の重複除去に吸収され削除）

### Added
- `_digest_one` に doc_id・window 数の info ログを追加（map 呼び出し数＝コストの実行前可視化）

### Tests Removed & Consolidated
- **重複テスト解消**: v0.4.2 の PR #9 で追加した test_probe_failure_drops_fts_table_for_retry_on_next_open（TestFtsInitProbe クラス）と v0.4.2 タグ前後の TestFtsProbeFailureSelfHeals.test_probe_failure_drops_fts_table_so_next_open_retries_backfill は同一シナリオのテストであったため、PR #9 版を残し TestFtsProbeFailureSelfHeals クラスごと削除（クラス内に他のテストなし、テスト数1017→1016に減少）

## [0.4.2] - 2026-07-25

### Fixed
- **FTS プローブ失敗時の自己修復**: _init_fts で CREATE VIRTUAL TABLE は成功しても _probe_fts が一過性失敗（SQLITE_BUSY等）した場合、空の chunks_fts テーブルが残存するため次回起動で already_existed=True になり移行バックフィルが永久に走らず既存チャンクがキーワード検索から恒久的に漏れる問題を修正。probe 失敗時に DROP TABLE IF EXISTS chunks_fts を実行して次回再試行可能に

### Tests Added
- プローブ失敗時の自己修復テスト 1 件: test_probe_failure_drops_fts_table_so_next_open_retries_backfill

## [0.4.1] - 2026-07-25

### Fixed（personalから移植した堅牢化）
- **FTS 幽霊行バグ**: delete_document と prune_missing で削除済みチャンクが FTS 索引に残り、キーワード検索に出現する問題を修正。削除前に FTS 行を capture し、DELETE 後に削除する二段構え処理を導入
- **FTS バックフィル自己修復**: _init_fts で chunks_fts テーブル CREATE 後のバックフィル失敗時、CREATE 済みテーブルが残るため次回起動で already_existed=True になり移行前チャンクが恒久的にキーワード検索から漏れる問題を修正。失敗時に DROP TABLE IF EXISTS chunks_fts を実行して次回再試行可能に
- **Windows source_path OS 区切り混入**: Windows で構築済みの既存 DB に残る `\` 区切りの chunks.source_path / file_state.source_file を POSIX 区切りへ後追いで正規化する _migrate_normalize_path_separators を導入（POSIX では正当なファイル名の `\` を保護するため os.name == "nt" ゲート付き）。indexer.py:71 と service.py:1374 でも .as_posix() 正規化を確保
- **Windows cp932 で consult 全滅**: engines/runner.py が text=True（locale 依存）で Windows cp932 になるため、encoding="utf-8", errors="replace" を明示化。cli.py に _reconfigure_stdio_utf8 を追加し main() で stdout/stderr を UTF-8 に固定
- **Windows timeout 時の子プロセス放置**: runner.py の timeout 後 os.killpg/getpgid 直呼びで Windows では AttributeError が except Exception に飲まれ timed_out=False 化。hasattr(os, "killpg") フォールバックを導入し Windows では proc.kill() で直接の子のみ kill
- **司書 backend 失敗の観測性**: Librarian.route() の戻り値を list[RouteTarget] から RouteOutcome（targets + router_error）に変更。backend 呼び出し失敗を router_error で伝搬し、service.consult() で warning 分岐で区別表示（従来は「資料からは分からない」に潰れていた）
- **PRAGMA busy_timeout**: shelf は長命 MCP サーバと別プロセス shelf index CLI が同一 DB へ同時アクセスするため、単発ロックを SQLite 自身に自動リトライさせる PRAGMA busy_timeout = 5000 を Store.__init__ に設定

### Tests Added（テスト件数 1002→1015: +13件、skip 0）
- FTS 幽霊行削除の回帰テスト 2 件: test_delete_document_removes_its_chunks_from_keyword_index, test_prune_missing_removes_pruned_chunks_from_keyword_index
- バックフィル自己修復テスト 1 件: test_rebuild_failure_drops_fts_table_so_next_open_retries_backfill
- Windows パス正規化テスト 5 件（全実行・skip 0、monkeypatch._force_windows=True で実装依存性を排除）: test_init_normalizes_backslash_source_path_in_chunks, test_init_normalizes_backslash_source_file_in_file_state, test_init_resolves_conflicting_file_state_by_keeping_posix_row, test_init_bumps_generation_when_rows_are_normalized, test_init_migration_is_idempotent
- POSIX パス保護テスト 1 件（常時実行・skip なし）: test_init_skips_normalization_on_posix_preserving_backslash_in_filenames
- busy_timeout テスト 1 件: test_busy_timeout_pragma_is_set_to_nonzero_ms
- router_error の検証テスト 2 件: test_backend_failure_with_conservative_fallback_returns_no_targets (assertion 追加), test_consult_reports_router_error_when_librarian_backend_fails
- Windows timeout フォールバック分岐テスト 1 件（monkeypatch.delattr で killpg を削除して proc.kill() 分岐を強制実行）: test_timeout_with_windows_fallback_uses_proc_kill

### Migration
- 既存 DB は自動マイグレーション対応（Store 初期化時に _migrate_normalize_path_separators 実行）
- POSIX 環境ではパス正規化は実行されない（`\` は合法的なファイル名）

### Intentional Divergence from personal
- **_migrate_normalize_path_separators に os.name == "nt" ゲート追加**: personal 版は無条件で正規化を行っていたが、OSS 版では POSIX 環境での正当なファイル名の `\` を保護するため Windows 環境でのみ実行（personal レビュー指摘を踏まえた改善）

## [0.4.0] - 2026-07-17

### Added
- **Digest map-reduce パイプライン**: 大規模資料から効率的に学びノートを抽出するため、単発 LLM 呼び出しから分割-集約パイプラインへ移行。ウィンドウ単位での抽出（map）→ 文書全体での統合・厳選（reduce）で精度と網羅性を向上
  - ウィンドウサイズ `SHELF_DIGEST_MAP_WINDOW_CHARS`（既定8000字）
  - Map フェーズ出力上限 `SHELF_DIGEST_MAP_NOTES`（既定5）
  - Reduce フェーズ出力上限 `SHELF_DIGEST_MAX_NOTES` を既定 5 から 20 に拡大
- **Digest 専用バックエンド**: `SHELF_DIGEST_BACKEND` で map-reduce フェーズを低コスト LLM（ローカル ollama など）へ逃がす機能
- **ハイブリッド検索**: cosine ベクトル検索 + FTS5 trigram BM25 の RRF 統合。日本語自然文の検索精度を向上。`SHELF_HYBRID_SEARCH` で制御（既定true）
- **ドキュメントタグ**: Digest 時に自動付与されるタグ（既存カタログから選別、NFKC/lower 正規化）。司書の情報源選別精度を向上
- **学ぶノート根拠接地**: 各 study_note に source_chunk_ids / section / page が記録され、回答の根拠トレーサビリティが向上
- **Insights レスポンスの強化**: ask/consult の各 insight エントリに section/page が付与（検索結果の位置情報が明示化）

### Changed
- **`SHELF_DIGEST_MAX_NOTES` 既定値**: 5 → 20 に拡大（文書全体の学びノート保有量を増加）
- **粒度プリセットの調整**: GRANULARITY_PRESETS で digest_max_notes の目盛りを 3/5/10 → 10/20/40 に更新（新既定20ベース）

### Removed
- **環境変数 `SHELF_DIGEST_INPUT_MAX_CHARS` の廃止**: 旧単発パイプライン専用の設定を削除。新パイプラインでは `SHELF_DIGEST_MAP_WINDOW_CHARS` で制御

### Fixed（マージ前レビュー反映）
- **Digest の耐障害性**: reduce 失敗時に既存の学びノート・タグを破壊したまま再試行不能になる問題を修正。失敗時は DB を更新せず原因付きでエラー報告し、次回 digest で自動再試行
- **FTS 同期の再設計**: 検索時の全コーパス再構築を廃止し、書き込み経路での行単位差分同期へ変更。FTS 障害時（読み取り専用 DB・fts5 非対応ビルド等）はベクトル検索単体へフェイルソフト
- **Digest コスト抑制**: window 分割をセクション境界ではなく文字数予算のみで行うよう変更（見出しの多い文書での LLM 呼び出し爆発を防止）。reduce 入力にも文字予算と均等間引きを導入
- **ハイブリッド検索の精度**: FTS 検索語を質問文の先頭 32 語ではなく全体からの均等サンプリングに変更（長い日本語質問の後半キーワード脱落を解消）
- **タグの堅牢化**: タグ正規化に文字種許可リストを追加（ルーティングプロンプトへの間接注入を緩和）。マスク後のタグ衝突による digest 中断も修正
- **その他**: チャンク一括取得による N+1 解消、digest タグカタログの 15 件上限撤廃、LLM 出力 JSON 抽出の共通化（`shelf/jsonutil.py`）

### Migration
- DB スキーマは自動マイグレーション（Store 初期化時に冪等実行）
- 既存の旧世代学びノート（pipeline=1）は自動認識され、新パイプラインでの再生成対象に
- 既存デプロイの移行手順: デプロイ → サーバ再起動 → notebook ごとに `shelf digest <nb>` → `shelf ask`/`shelf consult` でスモーク確認

## [0.3.1] - 2026-07-17

### Added
- Initial public release of agent-shelf
- Local-first RAG MCP server with FastEmbed + SQLite index over curated documents
- Pluggable LLM engine abstraction supporting Codex, Gemini, Anthropic, and Ollama
- Librarian (司書) routing to select optimal notebook sources from multi-document queries
- Hybrid RAG synthesis: multi-engine template-based answer generation
- CLI ingest pipeline: notebook creation, document ingestion, index building
- Masking layer for sensitive data extraction via configurable `distill/extract.py`
- Environment variable config for database path, corpus directory, embedding model, timeouts, and router backends
- Comprehensive test suite with pytest and ruff code quality checks
- Python 3.11+ support with `uv` package manager

### Features
- **ローカルベース検索**: 従量 API 不使用、FastEmbed + SQLite で実装
- **エンジン抽象**: Codex（既定）・Gemini・Anthropic・Ollama（ローカル）をプラグイン可能に
- **ハイブリッド RAG**: 複数 LLM に検索結果を同時投入し、バックエンド毎に回答を合成
- **司書（Librarian）**: 複数 notebook から最適な情報源を自動選別
- **機微マスク**: 出力前に機微情報を削除（recall との連携対応）
