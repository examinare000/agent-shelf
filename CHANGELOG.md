# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **ruff の lint ルールを拡大（I: isort・B: flake8-bugbear）**: 従来固定していた
  `["E4", "E7", "E9", "F"]` に `"I"`/`"B"` を追加。import 順の自動整形（I001、
  6ファイル）に加え、無意味化していた `# noqa: BLE001`（BLE ルールを select
  していないため常に無効だった、shelf/ 配下5箇所）を削除した。`distill/extract.py`
  は agent-recall と共有する上流資産のため、整形による同期 diff ノイズを避ける
  目的で `[tool.ruff.lint.per-file-ignores]` により I ルールを除外し対象外とした。
- **CI に Python 3.11/3.13 のマトリクスと pyright 型チェックを追加**: 既存の
  ubuntu/windows × 単一 Python 版から ubuntu/windows × (3.11, 3.13) の4組合せへ拡大
  （`requires-python = ">=3.11"` の下限・上位版の両方を CI で検証）。pyright は
  静的解析のため実行時 Python 差では結果が変わらないので ubuntu×3.11 の1点のみで
  実行（`uv run pyright shelf/`。tests/ は Protocol の構造的型付けに由来する既知
  ノイズが多いため対象外）。dev 依存に `pyright` を追加。
- **CONTRIBUTING.md を実態へ整合**: 「linting and formatting checks」の記載を
  「linting checks（ruff format は本プロジェクトでは不採用）」へ修正し、
  新規導入した型チェック（pyright）の実行方法を追記。
- **mcp SDK を 2.0 系へ更新**: Dependabot 更新（1.28.1→2.0.0）が `mcp.server.fastmcp`
  モジュール削除により CI を破壊していたため、`mcp>=1.0.0` から `mcp>=2.0.0` へ最低要求を
  引き上げ、v2 の破壊的変更に追従した。`FastMCP` クラスは `mcp.server.mcpserver.MCPServer`
  へ改名（`from mcp.server.fastmcp import FastMCP` は v2 に一切存在しない）。
  `call_tool()` の戻り値はタプルから `CallToolResult` オブジェクトへ変更され、
  `.content`（TextContent のリスト）と `.structured_content`（`{"result": ...}` 形式、
  list を返すツールのみ生成）で参照する。`server.settings.host`/`.port`/`.transport_security`
  への直接代入は廃止され、`server.run(transport="streamable-http", host=..., port=...,
  transport_security=...)` のキーワード引数として渡す方式に変更。

### Fixed
- **lint/型チェック強化（ruff B905・pyright）に伴う細い実穴を2件修正**:
  `indexer.py` の chunk 埋め込み対応付け `zip(rows, embeddings)` に `strict=True`
  を付けたところ、Embedder が契約（入力 texts と同数・同順の embedding を返す。
  `embedder.py` の Protocol docstring に明文化）に違反した場合に静かな取り違えで
  はなく即座に検知するようになった（過不足どちらの方向も検知することをテストで
  固定）。`service.py` の `_summarize_for_shelve` は、title の mask 呼び出しが
  try ブロック内にあったため mask 自身が例外を投げるとフォールバック return で
  `masked_title` が未束縛のまま参照される `UnboundLocalError` になる実穴があった
  （pyright: reportPossiblyUnboundVariable で検出）。mask 呼び出しを try の外側へ
  移し、常に束縛済みにした。この結果 title の mask 失敗は fail-closed（例外が
  そのまま伝播）になる。これは同ファイル内 `_resolve_description`（summary 生成
  失敗として fail-open に扱う既存設計）とは意図的に異なる選択で、「分類プロンプト
  へ必ず使われる title を mask できないまま処理を続けるより安全側」という判断
  （docstring に明記）。伝播粒度も明記した: `shelve()` のディレクトリ一括投入
  では、この fail-closed により1ファイルの title mask 失敗がバッチ全体を中断させ
  全ファイル未投入で終わる（`ConversionError` 等の1ファイル固有エラーが per-file
  収集されループ継続するのとは対照的）。mask 関数自体の破損はファイル個別ではなく
  バッチ内全ファイルに及ぶ系統的障害であるため、部分投入より全体停止を安全側として
  選んでいる。
- **emit_mcp: url 未指定/空文字列 + transport=http の直接呼び出しで診断しにくい
  クラッシュ・壊れた設定出力を修正**: `build_codex_toml_text(transport="http",
  url=None, ...)` を `emit()` を経由せず直接呼んだ場合、従来は
  `_toml_basic_string` 内で `AttributeError: 'NoneType' object has no attribute
  'replace'` という原因の分かりにくい形で落ちていた。さらに `url=""`（argparse
  で `--url ""` として普通に渡り得る）は `url is None` 判定をすり抜け、壊れた
  `url = ""` を無例外で書き出していた。`emit()` の検証条件 `not url` と同一の
  ガードへ修正し、`ValueError` で明確に失敗するようにした（pyright:
  reportArgumentType の指摘を機に発見）。`emit()` の `builders` dict が持つ
  3 ビルダー（claude/codex/gemini）全てに同じガードを適用した:
  `build_gemini_json_text`（同型シグネチャだがガード自体が無く
  `httpUrl: null`/`""` を書き出していた）と `build_claude_sh_text`（素の
  f-string 補間で `claude mcp add --transport http shelf "None"` を無例外で
  出力し、出力先が実行ビット付き claude.sh のため3者中最も影響が大きかった）
  の両方に同じガードを追加した。
- **クォート付き複数語の secret 値の過少マスク修正**: 汎用の password/secret/token regex が
  クォート文字列内の複数語を先頭 1 トークンのみマスクしていた問題を修正。値パターンを
  クォート全体優先（ダブル/シングルクォート、内部エスケープ許容、改行をまたいで飲み込まない）へ
  変更し、どちらでもなければ従来の `\S+` へフォールバック。対象は `ラベル: 値` / `ラベル= 値` の
  直書き形式のみ。既知の制限: JSON キー形式（`"password": "..."`）は本修正の前後を通じて
  未対応のまま。また閉じクォート直後に `,` `)` `}` `;` 等の非空白が続く形（JSON5/YAML flow/Python kwarg）は
  早期閉じ誤認防止のため旧実装と同じ先頭トークンのみのマスクに留まる（露出増なし）。

### Security
- **要約/分類/digest プロンプトへの title 未 mask 露出を修正**: v0.5.0 のカタログ投影・永続化時 mask（[ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md)）は、取込時の要約生成プロンプト（`build_summary_prompt` の add/shelve 双方の呼び出し）・shelve 要約失敗時のフォールバック分類プロンプト（`build_classification_prompt`）・digest map/reduce プロンプトの title 引数には未適用で、converter 抽出直後の生 title・既存 DB 行の未 mask title がそれぞれ backend へ素通しになる経路が残っていた。プロンプト構築の直前で mask を適用する
- **notebook description・persona の未 mask 露出を修正**: title と同型の穴が notebook description・persona にも残存していた。永続化時（`create_notebook` / shelve 新規 notebook 作成）は mask 未適用のまま store へ書き込まれ、投影時（`_build_catalog` のカタログ組み立て）・読み出し時（`ask`/`consult`/`digest` の専門家プロンプト構築直前）も既存 DB 行の未 mask 値をそのまま backend へ渡していた。永続化時 mask（新規行の恒久対処）と投影・読み出し時 mask（修正適用前の既存行への遡及対処）の二重防御を、title と同じ設計（[ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md)）で適用した。なお description には title の「再 add で自然更新」に相当する更新 API が無く、notebook 再作成でのみ更新される点に注意（既存の未 mask description を持つ notebook を浄化するには、投影/読み出し時 mask の二重防御が唯一の恒久対策となる）
- **フレッシュレビュー指摘の残存露出経路を追加修正**: (must) `Shelver.plan()` が新規 notebook 作成時に working_catalog へ積む `NotebookCard.description` は分類 LLM 応答（`decision.description`）そのもので mask 未適用のまま、次ファイルの分類プロンプトへ生で流出していた。`Shelver` に mask callable を注入し working_catalog 構築時にのみ適用（永続化用の `result.created` は生のまま保持し、永続化前 mask は呼び出し元 service.py の責務のまま維持。shelving.py は無変更）。(should) `list_notebooks()` の `description`・CLI `shelf persona` 表示の `persona` も既存 DB 行を素通ししていたため、`list_notebooks()` は `self._masked` を、CLI 表示は `_build_service`（実モデル DL）を経由せず `shelf.masking.mask` を表示直前にのみ適用する形で塞いだ（fix/persona-lazy-service の「表示のみの分岐で `_build_service` を呼ばない」制約を維持）
- **adversarial-verifier 実証済みの残存穴を追加修正**: `shelf shelve --dry-run` の JSON 出力（`created_notebooks[*].description`・`plan[*].reason`）と MCP `consult` 戻り値の `routed[*].reason` が、それぞれ plan.created（意図的に生 description を保持する shelver.py の設計）・分類/ルーティング LLM の自由記述をそのまま素通ししていた。mask 正本には既知の残存制限があり「LLM 出力に secret が混入しうる」という脅威モデルが適用されるため、いずれも `self._masked` を通す。**既知の制限として本修正の範囲外に残す事項**（詳細は SECURITY.md 既知の制限節）: (1) 修正適用前に永続化された `documents.description` の未 mask 既存行は、索引時の要約チャンク経由で backend へ流れうる（投影時二重防御の対象外。indexer は description を書き換えないため、`shelf add --desc` 指定時または `auto_summary=True` での再投入時のみ更新される。`shelf ingest` は `auto_summary=False` のため更新されない）、(2) `ShelfService(shelver=...)` の直接注入は mask 配線をバイパスする（現状呼び出し箇所ゼロの死んだ注入口・テスト専用）
- **再検証で判明した subquery の未 mask 露出を追加修正**: MCP `consult` 戻り値の `routed[*].subquery` が未 mask のまま残っていた。`subquery` はルーティング応答の同一 JSON から `reason` と一緒に取り出す兄弟フィールドで、reason の修正時に掃引が漏れていた。クライアント出力に加えて専門家プロンプト（`_answer_with_expert` の question 引数）へも投入されるため reason より露出が広い。`_consult_target()` の読み出し点1箇所で mask した値をクライアント出力・専門家プロンプトの両方へ使う形で修正した。あわせて SECURITY.md/ADR-0002 の不変条件の記述を、実装が満たす範囲（ルーティング/分類のメタ情報フィールド）へ絞り、回答本文（answer/insights/citations）は対象外である旨を明示的に開示した。masking 依存機能が packaging 上 `distill/` の実在に依存する既知の制約（wheel 配布では動作しない）も SECURITY.md へ新規開示した

## [0.5.0] - 2026-08-02

Windows 実運用（ヘッドレス HTTP サーブ）へ向けたブラッシュアップリリース。
並行性（async 化・consult 並行化・Store スレッド安全化・WAL）、Windows 対応（CI matrix・.cmd 起動・予約名）、
運用性（doctor・/health・HTTP env 設定）、取込形式（EPUB/FB2/XPS）、ルーティング品質を横断的に強化した。

### Security
- **取込資料 title の未 mask 露出を修正**: `documents.title` が永続化時点から mask 未適用で、司書ルーティングプロンプトへ未 mask のまま露出し得た（従来は ingest 時の一回限りのプロンプトにしか出ず露出面が狭かったが、本リリースの代表資料カタログ投影で毎 consult へ恒常露出する経路になるところをレビューで検出）。永続化時と投影時の双方で mask を適用する二重防御とした。「backend へ送る全テキストは mask 済み」という不変条件は [ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md) 参照
- **/health の公開情報を限定**: 死活監視用 `/health` エンドポイントは認証・Host 検査の外にあるため、応答は `status`/`version` のみに限定
- **意図しない全インターフェース bind への警告**: `serve --host`（または env `SHELF_HTTP_HOST`）が `0.0.0.0`/`::` の場合に stderr へ警告を出力
- **SECURITY.md に脅威モデル節を追加**: tailnet 信頼境界・認証なし・読み取り専用 MCP surface・取込時マスキングを明文化し、リモート公開可否を判断可能に
- **既知の制限（未修正）**: マスク規則のうち password/secret/token 系の値キャプチャが空白を含まないため、クォート付き複数語の値は先頭 1 トークンのみマスクされる過少マスクの余地がある。正本 `distill/extract.py` は agent-recall と共有のため修正は同期方針決定待ち（[ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md)）

### Added
- **EPUB/FB2/XPS 対応**: リフロー形式専用の変換経路を `pick_converter` に追加。リフロー形式のページ番号は再レイアウトの副産物で読者の手元の版と一致しないため、ページマーカーを挿入せず引用は見出しパンくず基準とする（[ADR-0001](docs/adr/0001-reflow-citation-heading-breadcrumbs.md)）。スキャン PDF 検出時のエラーには ocrmypdf 等での事前 OCR を促す案内を追記
- **`shelf doctor` プリフライト診断**: ヘッドレス運用での環境不備を起動前に発見する読み取り専用診断サブコマンド（DB 未作成時は生成せず情報提供に留める）。診断結果を終了コードへ反映
- **`/health` 死活監視エンドポイント**: HTTP サーブ時の外形監視用
- **HTTP リスナー設定の環境変数対応**: `SHELF_HTTP_ENABLED`/`SHELF_HTTP_HOST`/`SHELF_HTTP_PORT`/`SHELF_ALLOWED_HOSTS` で `serve` の設定を env からも解決可能に（優先順位: フラグ > env > 既定）。`--stdio` 明示フラグを追加し、`SHELF_HTTP_ENABLED=true` 環境でも stdio を強制できる脱出口を確保。Windows サービス定義（Task Scheduler + ps1）との連携を薄くする
- **ローカルファイル投入のサイズ上限**: `SHELF_MAX_FILE_MB`（既定 300MB）を追加。誤投入・暴走防止のため上限超過を拒否・スキップ（走査中のファイル削除レース等での stat() 失敗も安全に処理）
- **Windows CI matrix**: 本番サーバが Windows のため CI に windows-latest を追加し実挙動を検証可能に。既存テストの POSIX 依存（os.name/chmod/symlink/バイナリパス）を排除
- **司書ルーティングへの代表資料投影**: 未 digest notebook のルーティング精度低下を緩和するため、投入順の文書タイトルをカタログへ代表資料として投影（SQL 側 LIMIT で N+1 を回避、shelve 経路では発行しない）
- **shelve 命名リマップの可視化**: LLM が命名指示を無視し既定名へサイレントにリマップされる経路を shelve 計画結果へ表示
- **マスク規則の仕様固定テスト**: distill/extract.py のマスク規則 5 正規表現の現行挙動を positive/negative/冪等性/override 経路のテストで保護
- **設計書 2 本を移入**: コード・テスト約 15 箇所の宙吊り §参照を解消するため、`docs/design-shelf-mcp.md`・`docs/design-shelf-reference-service.md` を personal リポジトリからスクラブして移入
- **distill/SKILL.md**: extract.py が参照する未作成の SKILL.md を追加（使い方・出力先・state ファイル）
- **e2e / dispatch テスト**: new→add→index→ask の一気通貫テストと ls/new/index/ask の main() dispatch テストを追加
- **SQLite WAL 化**: shelf は長命 MCP サーバ(`shelf serve`)と別プロセスの CLI(`shelf index`/`shelf digest`)が同一 DB ファイルへ同時アクセスする構成のため、既定の rollback-journal では CLI の書き込みトランザクションがサーバの読み取りをブロックしていた（`busy_timeout` 頼みの待ち合わせのみ）。`Store.__init__` で `PRAGMA busy_timeout` の設定直後・スキーマ作成前に `PRAGMA journal_mode=WAL`・`PRAGMA synchronous=NORMAL` を発行し、reader/writer が互いをブロックしない WAL モードへ移行。journal_mode は DB ファイルに永続する属性のため、既存 DB も新コードで開くだけで自動的に WAL 化される（migration スクリプト不要）。読み取り専用ファイルシステムやネットワーク共有等で WAL が有効化できない場合や、これらの PRAGMA 発行自体が読み取り専用パーミッション・別接続との書き込みロック競合で `sqlite3.OperationalError` を送出する場合も、例外にせず warning ログへフェイルソフトし rollback-journal のまま起動を継続する（既存の FTS 劣化と同じ流儀）。次回 open 時に競合が解消していれば自動的に WAL 化される
  - **注意**: WAL モードでは DB ファイル本体に加えて `-wal`・`-shm` のサイドカーファイルが増える。DB を OneDrive 等のクラウド同期フォルダやネットワーク共有（SMB/NFS）に置いている場合、WAL が要求する共有メモリ・ロック機構が動作せず機能しないことがある（その場合は自動的に rollback-journal のままフェイルソフトする）。バックアップを取る際は `-wal`・`-shm` を含めたサイドカー込みでコピーするか、整合性の取れた単一ファイルを得られる `VACUUM INTO` を推奨する
- **FTS ラッチの自己修復リトライ**: 別プロセスによる `chunks_fts` の DROP や MATCH 読み取り自体の一過性エラー等でキーワード索引が壊れ `fts_enabled=False` に落ちた場合、従来は長命サーバのプロセス再起動までハイブリッド検索を喪失していた。直前まで有効だった FTS が今回初めて壊れた場合に限り、劣化後最初の `keyword_topk` 呼び出しで `chunks_fts` を強制的に作り直して(DROP+全件バックフィル)1回だけ再試行するようにした。already_existed の判定に関わらず常に全件バックフィルするため、劣化中に upsert された行も復旧時に取りこぼさない。この再試行の実行中に発生した失敗（backfill 自体の失敗・成功直後に続けて実行される実クエリの失敗）は「直前まで健全だった」と誤認されず再アームされない（障害が持続的でも毎クエリ再試行にはならない）。毎クエリ再試行はコストのため予算は使い切りで、初回から fts5/trigram 非対応の環境など直前まで一度も有効化できていない場合は無駄なリトライを行わない
- **content_hash 記録 + notebook 横断の内容重複検出**: `documents.content_hash` はスキーマ・`upsert_document` 双方で対応済みだったが `_ingest_file` が渡していなかったため常に NULL だった。変換後 markdown の sha256（`_content_hash_of`、digest の source_hash 計算と共有）を計算して記録し、`add_source`/`add_directory` の応答へ同一内容の既存資料を `duplicates: [{doc_id, notebook}]` として additive に警告表示する（空なら省略・既存の notes と同じ流儀）。UX は warn + 記録のみで skip はしない（同一書籍を複数の棚に意図的に置く運用は正当なため）。`Store.find_documents_by_content_hash` を新設（notebook を跨いだ全表検索。既存行の backfill は再 add で自然に埋まる。一括 backfill コマンドはスコープ外）

### Changed
- **MCP 3 ツール（ask/list_notebooks/consult）の async 化**: 単一の長時間リクエストがイベントループごと全クライアントをブロックしていた問題を解消。backend 呼び出しをワーカースレッド（上限 40）へ逃がし、他クライアントの応答性を維持。`anyio` を推移的依存から直接依存へ明示化（`abandon_on_cancel` 導入版 4.1 以上）。輻輳時挙動（ワーカースレッド上限とキャンセル時のスロット解放遅延）はタイムアウト契約に記載
- **consult の expert 呼び出しを並行化**: `SHELF_ROUTE_TOP_N` 段の逐次レイテンシを縮めるため ThreadPoolExecutor で並行化し、共有 embedder は Lock で直列化。応答順序・per-target 劣化・例外伝播の意味論は維持
- **Store のスレッド安全化**: MCP ツール非同期化に備え Store を RLock で公開メソッド単位に保護し、ShelfService の遅延構築（_get_librarian/_shelver 等）の check-then-set レースを解消。load_vectors の行列構築はロック外に置き、index 中の ask 直列化を回避
- **consult warning の原因別分離**: 回答不能（資料からは分からない）と解析失敗（backend エラー・パース失敗）を利用者が区別できるよう warning 文言を分離
- **README を実装と一致**: 検証規則で拒否されるクイックスタート例と実在しないエンジン記載を修正し、対応形式・全コマンド・リモート提供・環境変数の欠落を補完。多段ルーティングの推奨設定と並行化後の理論上の最悪壁時計も追記

### Fixed
- **persona 表示専用パスの embedder 構築ハング**: persona コマンドの表示専用パスが不要に実 embedder を構築し、モデル未キャッシュ環境で pytest 全体を恒久ハングさせていた。`_build_service` の呼び出しを set/clear 分岐内へ遅延
- **Windows で npm 由来 .cmd シムの起動失敗**: CreateProcess が .cmd を解決できず consult backend の起動に失敗していたため、bare コマンド名に限り `shutil.which` で事前解決してから実行（相対パス+workdir の既存契約は維持）
- **Windows timeout 時の孫プロセス孤児化**: .cmd 経由起動では timeout 時に cmd.exe のみ kill され node.exe 等の孫が孤児化していたため、Windows では `taskkill /T /F` でプロセスツリーごと kill
- **Windows 予約デバイス名の notebook 名**: con/prn/aux/nul/com1-9/lpt1-9 はディレクトリ作成不能になるため、validate では拒否し shelve 自動命名では安全な名前へリマップ
- **emit-mcp の Gemini 出力キー**: Gemini CLI が streamable-http 接続で `url` キーを SSE と誤認するため、gemini 出力を `httpUrl` キーに変更
- **setup 対話フローのカスタム粒度入力が無視されるバグ**: 非数値・0 以下を拒否する検証付きで digest_max_notes/top_k を個別入力できるよう修正
- **ask/consult の insights[].note_id が chunks.id を誤って返していた**: `_build_insights` が本来 `study_notes.id`（`{notebook}/{doc_id}#d{n}` 形式・設計書 §217 の契約）を返すべき `note_id` に、索引用の `chunks.id`（`{notebook}/{doc_id}#{digest_seq}` 形式）をそのまま入れていた。`note_id` の値を `study_notes.id` 形式へ正規化し、従来の値（`chunks.id`）は応答互換のため新設の `chunk_id` キーへ additive に残した。**外部クライアントへの影響**: `insights[].note_id` の値の意味が変わる（従来の生値が必要な場合は新設の `chunk_id` を参照すること）

### Migration Notes
- **既存の予約名 notebook のロックアウト**: 本リリースで Windows 予約デバイス名
  （con/prn/aux/nul/com1-9/lpt1-9、大文字小文字不問）が notebook 名として拒否されるようになった。
  本修正の適用前にこれらの名前で notebook を作成済みの場合、ask/add/digest/persona などの
  操作は notebook 名検証で以後すべて弾かれ、データを直接救済する手段はない。対応が必要な場合は
  `shelf rm <予約名の notebook>` で削除したうえで、別名で資料を再投入すること。
- **既存 DB の WAL 化は自動適用**: 新コードで DB を開くだけで journal_mode が WAL へ移行する（上記 Added 参照）。適用前に `VACUUM INTO` 等でバックアップを取ることを推奨。

### Versioning Note
- 本リポジトリ（OSS）と personal リポジトリ（v0.5.0）は独立採番。本リリースを personal へ還流する際は personal 側を 0.6.0 として適用することを推奨（還流ガイド: [docs/backport-0.5.0.md](docs/backport-0.5.0.md)）。

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
