# 2026-08-02 ブラッシュアップ計画立案

## 目的
/goal「知の番人」完成に向けた未達洗い出しとタスク分解（計画: ~/.claude/plans/dapper-kindling-koala.md）。

## 試行と結果

### 初版計画 → adversarial-verifier REJECT（12 修正で解消）
主要な反証:
- A3（async 化）単独では consult の壁時計 900s は縮まない → expert 並行化タスクを新設（A4）
- Store の RLock は「公開メソッド全体」以外の粒度は不正（mutator が途中 commit するため）。ShelfService の遅延 check-then-set（_get_librarian/_shelver）もスコープ漏れ
- load_vectors の行列構築をロック内に置くと index 中の ask が全て再構築で直列化 → ロック外へ
- Windows CI の実コストはモデル DL ではなく既存テストの POSIX 依存 58 箇所の移植
- EPUB は pymupdf4llm 直で動く（実測）が、リフローの「ページ番号」は虚構 → 引用は page=None + 見出しパンくず基準（→ [ADR-0001](../adr/0001-reflow-citation-heading-breadcrumbs.md) へ昇格）
- .cmd 解決後の timeout kill は cmd.exe のみ殺して node.exe を孤児化 → CREATE_NEW_PROCESS_GROUP + taskkill /T /F
- /health custom_route は認証・Host 検査の外 → status+version のみに絞る
- WAL では FTS 無効化ラッチ（別プロセスの DROP chunks_fts）は防げない → A1 に回復性を追加

### ユーザー指摘による再スコープ
「VPN(tailscale) 経由の MCP 接続は実現済み。personalized-claude repo の shelf mcp を確認」
→ 確認結果: personalized-claude に Windows デプロイ資材一式（serve-shelf.ps1/register-task.ps1/firewall-rule.ps1/install-ollama.ps1）と Mac 側登録スクリプトが実在、`avalon.tail18a7d0.ts.net:8765/mcp` 登録済み。本番バックエンドは ollama qwen3:8b。設計書 2 本も personal/docs に実在。
→ **棄却した案**: デプロイ資材の新規作成（旧 A9）・設計書のゼロから再構築（旧 B6）。既存資産の再発明になるため。
→ 採用: スコープを OSS 上流のコード品質に絞り、設計書は personal からスクラブ移入、成果は還流フロー（C1）で personal へ。

## 備忘
- personal shelf は v0.5.0 だが async/WAL/which 修正は無い（grep 確認済み）→ 本リポジトリでの修正が還流価値を持つ
- 接続テスト時 avalon はタイムアウト（サーバ停止中か。コード起因ではない）

## 実装第 1 波のレビュー知見（2026-08-02）

- **B1 (README)**: 指摘なしで通過。コミット済み（docs/readme-accuracy）
- **B2 (EPUB)**: page_chunks=False が chunk 手動結合と完全同一テキストを返すことを実装エージェントが実測 → 独自結合コード不要の最小実装が成立。レビューで「事前検証値の又聞き数値をコメントに実測として書く」幻覚が検出され修正（教訓: 別環境の検証値は追試せずに断定転記しない）。コミット済み（feat/epub-support）
- **A5 (runner)**: レビューで must 級回帰を検出 — which 事前解決が相対パス+workdir の既存契約を破る（親 cwd 基準解決）。bare 名限定で解決。taskkill 非 0 終了の見逃し、CREATE_NEW_PROCESS_GROUP と taskkill /T の因果誤解（プロセスグループ無関係）も修正。**棄却**: creationflags 付与（taskkill 方式には不要と判明し削除）
- **A1 (WAL/FTS)**: 両レビューで must 級を検出 — (1) _enable_wal 例外未処理で読み取り専用 DB が開けなくなる退行 + WAL 変換は busy_timeout の恩恵を受けない (2) FTS 自己修復が「リトライ自身の失敗」で自己再アームし無限リトライ化 (3) DROP 以外の失敗ではリトライ成功時に backfill されず無効化中の行が静かに永久欠落。修正中
- **横断発見**: cli.py persona 表示分岐が無条件 _build_service()（実モデル DL）→ モデル未キャッシュ環境で全体 pytest 恒久ハング。3 エージェントが独立に同一原因を特定。fix/persona-lazy-service で修正・コミット済み
- **教訓**: 状態機械（FTS ラッチ）への「1 回きりリトライ」追加は、リトライ中の失敗経路・部分復旧（backfill 有無）まで意味論を詰めないと必ず穴が出る。レビュー 2 本体制（アンチパターン + フレッシュ）が相補的に別の must を検出しており有効

## B6 設計書移入の申し送り（後続タスク候補）

- 宙吊り参照 3 件が残存: tests/test_embedder.py:4 の「§14」（両文書に不在）、shelf/setup.py:26/:176 の「タスク仕様 §1-3/§1-4」（別文書への参照とみられ未解決）→ B8 か還流時にコメント修正
- 文書とコードの乖離 6 件（文書は無改変で移入）: ①digest が map-reduce 化済みなのに文書は単発生成 ②masking/jsonutil/emit_mcp/setup がモジュールツリーに不在 ③ingest/emit-mcp/setup コマンドが CLI 仕様に不在 ④setup/ 資産は personalized-claude 固有 ⑤リポジトリレイアウトが二層→フラットに変化 ⑥StudyNote に section フィールド追加。→ 将来の文書改訂タスクの種

## B5 マスク規則テストで発見した既知の欠陥（要ユーザー判断）

- **過少マスク（実害あり）**: 汎用 password/secret/token regex の値キャプチャが `\S+` のため、`password: "hunter 2 with spaces"` のようなクォート付き複数語の値は先頭 1 トークンのみマスクされ残りが漏れる。extract.py は agent-recall と共有の改変禁止資産のため修正せずテスト docstring に記録。**修正には agent-recall との同期方針決定が必要**（C1 還流時にユーザーへ提起）
- 副次観察: regex 適用順で「token: sk-…」のラベル種別情報が失われる / sk-・ghp_ 系に単語境界なし（過剰方向のみで実害なし）

## 並行 worktree 運用の教訓（2026-08-02）

- **worktree 基点の陳腐化が 2 回発生**（A6, A2）: エージェント worktree はローカル main の進行に追随しないため、マージが積まれるほど基点が古くなる。A2 は古い store.py を見て「ブリーフの属性が存在しない＝ブリーフの齟齬」と誤判断した（実際はマージ済みコードに実在）。対策: ブリーフに「git log で基点ハッシュを確認し、指定ハッシュ以降でなければ checkout -B <branch> main で載せ替え」を必須手順として明記し、完了報告に基点ハッシュを含めさせる。エージェントの「齟齬」報告は基点確認とセットで検証する

- **原因判明（3 回目の再発 B3 で確定）**: worktree 分離はローカル main ではなく origin/main（8f011a9）基点で作成される。本セッションは push しないためローカル main の進行が worktree に反映されない。対策を強化: 以降のブリーフでは「`git merge-base --is-ancestor <最新マージhash> HEAD` で確認し、失敗したら `git checkout -B <branch> main`」という機械的検証手順を指定する（「確認せよ」だけではエージェントが誤認する — B3 は「8f011a9 は 1be8b40 以降を含む」という矛盾した確認報告をした）

## A2 レビューからの繰り延べ（B8/後続へ）

- load_vectors のキャッシュ汚染防止分岐（generation 再確認）に Event ベースの決定的レーステストが未整備（実装は正しいと両レビュアー確認済み。次回 PR で追加推奨）
- close() と load_vectors のロック解放窓の競合は運用契約コメントで対処（production では close 未使用。A3 のサーバライフサイクル配線時に「全呼び出し完了後にのみ close」を遵守）
- エンジン実装（engines/*.py）自体のスレッド安全性は未確認（subprocess 起動の並行実行 — A3/A4 で顕在化するため A4 レビュー時に確認事項へ）

## B7 レビューの重要発見（2026-08-02）

- **セキュリティ**（→ [ADR-0002](../adr/0002-masked-invariant-for-backend-text.md) へ昇格）: documents.title は永続化時点から mask 未適用だった（従来は ingest 時の一回限りのプロンプトにしか出ず露出面が狭かったため見過ごされていた）。titles のカタログ投影で毎 consult へ恒常露出する経路になるところをレビューが検出。教訓: 「新しい露出経路を作るとき、載せるデータの sanitize 履歴を遡って確認する」— 既存データが直感的に安全とは限らない
- **順序前提の検証**: 「id 昇順=投入順」という直感的な前提が doc_id の実生成方式（スラグ+ハッシュ）で崩れていた。テストが単純 id を使ったため偶然 green。教訓: 順序に依存する機能のテストは、順序が逆転するデータで書く

## 完了宣言の反証検証（adversarial-verifier、REJECT → 追修正）

検出された実質的不足（追修正ラウンドで対応）:
- test_doctor の chmod テストに Windows ガードなし → push 直後に windows-latest が赤くなることが実行前から判明（同リリース内の test_cli には同型ガードの先例あり — 掃引後に新規 POSIX 依存が入り CI 未実行のため検出されず）
- serve 初回起動はモデル DL がリスナー bind より前に走り /health 無応答の窓がある。doctor の fastembed チェックは ok=True 固定 + docstring の DL タイミング記述が誤りで、この失敗を予告できない → docstring 修正 + README 文書化（根本対処の遅延ロードは還流後の課題として繰延）
- SECURITY.md に既知の過少マスク欠陥が未記載（ADR/backport ガイドのみだった）→ 開示追記
- v0.5.0 タグ未作成（過去リリースは全てタグ付き）→ 追修正マージ後に付与
- push は外部公開操作のためユーザー判断に委ねる（未 push の間 Windows CI は実行されない — 引き継ぎ事項として明示）

崩されなかった主張: 1260 green・ruff clean・全 Must/Should 実装の実在・CHANGELOG サンプル突合 5 件・WAL 自動移行の実測・README クイックスタートの動作。

## PR #13 Windows CI 初実走 8 件失敗の切り分け（2026-08-02）

release/0.5.0-publish への push 後、windows-latest ジョブ（91468134053）が 8 件失敗。macOS ローカルでは Windows 実行不能なため、実装読解 + 「ホストに依らず再現できるテスト」を書くことで切り分けた。

- **実バグ 4 件（コード修正）**:
  - `emit_mcp.build_codex_toml_text`: `str(repo_root)` を TOML basic string へ無エスケープで埋め込んでいた。Windows パスの `\U`・`\u` が不正 Unicode エスケープと解釈され `tomllib.TOMLDecodeError`。`_toml_basic_string()` を追加しエスケープ。POSIX でも `Path("C:\\Users\\x")` は文字列としてバックスラッシュを保持するため（`\` は POSIX の区切り文字でない）、ホストに依らず再現・検証できた（`tests/test_emit_mcp.py::test_stdio_escapes_windows_backslash_path`）。
  - `Store._migrate_normalize_path_separators`: `_enable_wal` は既に fail-soft（A1 修正済み）だが、その後に無条件実行される本メソッドの UPDATE は未捕捉のままだった。readonly DB・ロック競合のどちらでも `__init__` がクラッシュ（Windows CI の2件はどちらもこのメソッド内で失敗、traceback で確認）。try/except sqlite3.OperationalError で fail-soft化。`_force_windows=True` + `chmod`/別接続ロックで POSIX ホストから再現。
  - `convert._convert_reflow`: `raise ConversionError(...) from e` が元の pymupdf 例外（絶対パスを含む）を `__context__`/`__cause__` 経由で連鎖させたまま呼び出し元へ渡していた。message には出ないが、ログ出力・トレースバック経由で「そのまま見せない」という意図に反して漏れうる設計不整合であり、Windows では元例外のトレースバックがフレームローカル経由で pymupdf 内部の未解放ファイルハンドルを延命させ、テストの tempdir cleanup で WinError 32 を誘発する疑い。except ブロックの外側で raise することで `__context__` を自動的に None にし、連鎖を断ち切った（`sys.exc_info()` は except ブロックを抜けるとクリアされる、というテストで確認済みの CPython の挙動を利用）。
  - **反証検証の余地**: convert.py の修正は「元例外のトレースバックが Windows のファイルハンドル延命に寄与する」という因果を Windows 実機で実測できていない（macOS では該当しない）。ただし「安全なエラーメッセージを謳いながら生の例外を chain する」設計不整合自体は独立した正当な修正理由であり、chain を切ることに副作用はない。

- **ホスト差 3 件（テスト側で skipif、コード側は変更なし）**:
  - `test_stdio_script_is_valid_bash_syntax`/`test_http_script_is_valid_bash_syntax`: CI ログの stderr は空文字なのに returncode=1 で失敗、stdout に UTF-16 らしき制御バイト列が混じり末尾が「...to install.」で終わる。GitHub Actions windows-latest では無引数の `bash` が `C:\Windows\System32\bash.exe`（WSL 未導入時の案内スタブ）に解決されうる既知のランナー特性と一致（`wsl --install` 系の案内メッセージ）。生成スクリプト自体はローカル実 bash で構文検証済み（既存テストが両方 green）のため、ホスト差と判定し skipif(os.name=="nt")。
  - `test_init_skips_normalization_on_posix_preserving_backslash_in_filenames`: ブリーフで原因確定済み（`monkeypatch.setattr(os, "name", "posix")` が pathlib のクラス選択に波及し、Windows ホストで `Path()` が `PosixPath` を選んで `NotImplementedError`）。monkeypatch を撤去、POSIX ホストは os.name が元々 posix なので patch 不要、skipif(os.name=="nt") + docstring 修正。
  - `test_relative_path_command_skips_which_and_uses_workdir`: `os.path.relpath(sys.executable, start=tmp_path)` が Windows CI でドライブ跨ぎ（チェックアウト D: / TEMP C:）により ValueError。`runner.py` 自体は relpath/relative_to を使わない（grep 確認済み）ためテスト前提側の問題。**棄却した対処**: tmp_path 配下へ python バイナリをコピー/シンボリックリンクして同一ドライブを強制する案 — コピーは venv python が `@executable_path` 相対の dylib 参照に依存するため macOS で dyld ロード失敗を実測（`Library not loaded: @executable_path/../lib/libpython3.12.dylib`）、シンボリックリンクは Windows で管理者権限が必要になりうり両方とも新たなホスト依存を生む。「cmd[0] にパス区切りを含む場合は which を呼ばない」という本来の回帰観点は同ファイル内の `test_which_is_not_called_when_cmd_contains_path_separator`（OS 非依存）が既にカバーしているため、skipif(os.name=="nt") で妥当と判断。

- **環境メモ**: 本セッション中、`.pytest_cache`/`.ruff_cache` への書込みで断続的に `Operation not permitted`（sandbox）が発生。pytest は警告のみで実行継続（結果は信頼できる）、`ruff check .` はキャッシュ作成不能で即失敗したため `ruff check --no-cache .` で回避（サンドボックスを無効化せず、ツール側の正規オプションで書込み自体を回避）。両ファイルとも所有者は自分・パーミッションは通常通りで原因不明（cwd はサンドボックスの書込み許可対象のはずだが再現）。

- **未 push**: コミット・push はユーザー/git-composer 側の判断。push するまで Windows CI は再実行されない。

## fix/prompt-title-mask: title 未 mask のプロンプト直渡し経路の残存修正（2026-08-02）

B7（永続化時＋カタログ投影時の title mask）適用後も、personal 側還流の反証検証で
「プロンプト構築へ直接 title を渡す」経路が3箇所（要約自動生成 add/shelve 双方の
`build_summary_prompt`、digest map/reduce の `title=doc.get("title")`）に残存している
ことが判明。B7 の mask 適用点はカタログ投影（`_project_notebook_titles`）と永続化
（`_persist_converted`）の2点のみで、「backend へ渡る全プロンプト経路」を悉皆的に
洗い出せていなかった（ADR-0002 の不変条件は宣言されたが実装が全経路を網羅していな
かった）。

- 3経路それぞれで Red→Green（FakeAnswerBackend.calls[0]["prompt"] に secret が
  含まれないことを固定）。add/shelve の要約経路は `_resolve_description`/
  `_summarize_for_shelve` 内でプロンプト構築直前に `self._mask` を適用。digest は
  `doc.get("title")` の代入直後に mask し、map/reduce 両方で共有する変数を経由させた
  （2箇所への個別適用ではなく代入点1箇所で両方をカバーできる設計）。
- **掃引で追加発見**: `build_summary_prompt/build_map_prompt/build_reduce_prompt/
  build_routing_prompt` の4関数への grep だけでは不十分で、shelve の要約失敗時
  フォールバック（`_shelve_fallback_classification_text`）が5つ目のプロンプト
  builder `build_classification_prompt`（shelving.py）へ生 title を渡していた
  ことも判明。要約成功パスは修正済みでも失敗パス（ok=False/例外）だけ生 title の
  ままという非対称な穴で、grep 対象を4関数に限定していたら見逃していた。
  教訓: 「関数名の grep」ではなく「同一データ（title）の全流出先」で追跡する方が
  漏れに強い。
- 教訓（ADR-0002 への追記候補）: 「プロンプト構築の直前に mask」という決定は
  正しかったが、適用が漏れていた事実は「不変条件を文書化しただけでは実装の悉皆性を
  保証しない」ことを示す。新しい prompt builder を追加するレビュー観点として
  「引数に title/description 等の DB 由来テキストが含まれる場合、呼び出し元で
  self._mask を通しているか」を機械的にチェックする必要がある。
- 検証: `uv run pytest -q`（1265 passed）・`uv run ruff check`（All checks passed）。
  CHANGELOG.md Unreleased・SECURITY.md 脅威モデル節（title の二重防御対象に
  要約/分類プロンプトを追記）を更新。ADR-0002 自体は「投影時」を「プロンプト構築時
  全般」と広く解釈すれば矛盾しないため未改訂（決定の原則は変えず適用範囲の実装漏れを
  埋めた変更として扱う）。

## deps/mcp-2.0-migration: mcp SDK 1.28.1→2.0.0 移行（2026-08-02）

Dependabot PR #12 が `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` で
CI 失敗中だった移行作業。公式移行ガイドの記述は概要のみだったため、`uv sync` 後の
実インストール物（inspect.signature 等）を実際に対話実行して確認した上で実装した
（推測で書かない、というブリーフの指示通り）。

- **確認した実 API（v2.0.0、site-packages を直接 introspect）**:
  - `MCPServer("shelf")` は `mcp.server.mcpserver.MCPServer`（`mcp.server` からも
    re-export されている）。`custom_route`/`tool()` デコレータのシグネチャは v1 と
    同一で変更不要だった。
  - `server.settings` は `debug`/`log_level`/`dependencies` 等のみを持つ pydantic
    モデルで **host/port/transport_security フィールドが存在しない**。
    `server.settings.host = "..."` は `ValueError: "Settings" object has no field
    "host"` を送出する（実行して確認済み）。host/port/transport_security は
    `server.run(transport="streamable-http", host=..., port=..., transport_security=...)`
    のキーワード引数として渡す方式に変わっていた（ブリーフの推測 #5 と一致、実測で
    確定)。
  - `call_tool()` は `(content_blocks, {"result": ...})` のタプルではなく
    `CallToolResult` オブジェクト1個を返す。`.content`（TextContent のリスト）と
    `.structured_content`（`{"result": <戻り値>}` 形式。list を返すツールのみ生成
    され、素の `dict` を返すツールでは `None` のまま）を実際に呼んで確認した。
  - `TransportSecuritySettings`（`mcp.server.transport_security`）はモジュール
    パス・フィールドとも無変更。
  - `mcp.server.fastmcp` は v2 に一切存在しない（deprecated shim すら無し、
    import で即 `ModuleNotFoundError`）。
- **test_boundaries.py の "fastmcp" エントリの扱い**: 実は v1 時点から
  `_RESTRICTED_TO_OWNER["fastmcp"]` は死んだエントリだった。実際の import 文は
  `from mcp.server.fastmcp import FastMCP` で AST 上のトップレベルモジュール名は
  `"mcp"`（`split(".")[0]`）であり `"fastmcp"` という名前が `modules` 集合に入る
  ことは無かった（このリポジトリで `import fastmcp` 直下 import をしたファイルも
  皆無、git log 確認済み）。v2 でも同型の実効性のない防御的エントリとして
  `"mcpserver"` にリネームし、コメントでその旨（実効性が無い理由）を明記した。
  **棄却した案**: エントリを削除する — ブリーフが「新モジュール名に合わせて更新」
  と明示していたこと、および将来 `mcp.server.mcpserver` 相当が本当にトップレベル
  import される変更が入った場合の防御として意図が伝わる形で残す方を選んだ。
- **Red→Green の順序**: server.py の import を先に MCPServer へ直してから
  test_server.py の CallToolResult 対応、cli.py の kwargs 化前に一度
  `_FakeMcpServer` を新シグネチャへ更新した状態で旧 cli.py 実装に対して実行し、
  `AttributeError: '_FakeMcpServer' object has no attribute 'settings'` で正しい
  理由の Red を確認してから cli.py を書き換えた（サンドボックスの `git stash`
  書き込み拒否で往復に手間取ったが、`/tmp/claude` へのバックアップコピーで代替）。
- 検証: `uv run pytest -q`（1282 passed）・`uv run ruff check --no-cache .`
  （All checks passed）。CI が実行するのはこの2コマンドのみ（pyright/mypy 等の
  typecheck step は無い）。

### 完了宣言の反証検証（adversarial-verifier、FAIL → 追修正）

コア移行（改名・CallToolResult・run() kwargs 化）自体は実 API 照合で正しいと
確認されたが、以下の残件で FAIL:

- **旧称 FastMCP の残存**: `git grep -n -i fastmcp` で `shelf/service.py:3`
  （docstring が「FastMCP の ask/list_notebooks 2 ツール」のまま。移行前から
  ツール数も陳腐化していた — 実際は ask/list_notebooks/consult の3ツール）・
  `docs/design-shelf-mcp.md:105,374`・`docs/design-shelf-reference-service.md:63`
  がヒット。grep 対象を「移行に必要な範囲」（server.py/cli.py/tests/pyproject.toml）
  に限定し、docstring・設計書中のクラス名言及まで悉皆的に洗い出せていなかった
  （B7 の「関数名の grep だけでは不十分」教訓と同型の見落とし方）。
  → service.py は MCPServer 改名+3ツールへ修正、design-shelf-mcp.md の2箇所と
  design-shelf-reference-service.md の1箇所は MCPServer への改名のみ実施
  （design-shelf-mcp.md の「2 ツール」は T11 タスク定義時点の歴史的記述であり
  consult 追加前の設計書のため、ツール数はブリーフが明示的に指示した service.py
  のみ修正し、他は改名のみに留めた）。
- **CHANGELOG.md 未記載**: 依存の最低バージョン引き上げ（mcp>=1.0.0→2.0.0）と
  破壊的変更3点への追従が [Unreleased] に記載されていなかった。先例
  （CHANGELOG.md:54 の anyio 直接依存化の書式）に倣い `### Changed` を新設して追記。
- **テストダブルの構造的欠陥（推奨修正・採用）**: `_FakeMcpServer.run(transport,
  **kwargs)` が任意の kwarg 名を黙って受け取るため、settings→run() 引数移動の
  ような破壊的変更クラスをテストで検出できず、今回 CI が赤くなったのは import
  エラーという別経路の偶然の静的検出のみだったという指摘。cli.py が実際に
  組み立てた kwargs を実 `MCPServer.run_streamable_http_async` のシグネチャへ
  `inspect.signature(...).bind_partial(**kwargs)` で bind するテストを追加し、
  cli.py 側の kwarg 名を意図的に `transportSecurity`（誤字）へ変えて
  `TypeError: got an unexpected keyword argument 'transportSecurity'` の Red を
  実際に確認してから正しい実装へ復元し Green を確認した（テストダブル自体では
  検出できないことを実演した上での追加）。

崩されなかった主張: コア移行の実 API 照合結果（MCPServer 改名・
CallToolResult.content/.structured_content・run() kwargs 化のいずれも実測通り）。

検証: `uv run pytest -q`（1283 passed）・`uv run ruff check --no-cache .`
（All checks passed）・`git grep -n -i fastmcp` のヒットが trial-log 自身・
test_boundaries.py の意図的な履歴コメント・CHANGELOG.md の移行事実の記述のみに
絞られたことを確認。

棚卸し: テストダブルの実シグネチャ結合（と mcp>=2.0.0 の上限なし維持の判断）を
docs/adr/0003-bind-test-doubles-to-real-sdk-signatures.md へ昇格した。

## fix/mask-description-persona: ADR-0002 残存違反（description/persona）の修正（2026-08-07）

title は fix/prompt-title-mask で既に永続化時＋投影時＋プロンプト構築直前の三重の
悉皆修正が完了していたが、notebook description と persona には同型の穴が残っていた。
ブリーフが指定した6箇所（create_notebook・shelve 新規 notebook 作成の永続化時2箇所、
_build_catalog の投影時1箇所、ask・_consult_target・digest の読み出し直前3箇所）に
`_masked` ヘルパを適用し、Red→Green を2段階（永続化時→投影時→読み出し時の3ラウンド、
テストは brief 指定の7本）で実施。全て「正しい理由」（secret がプロンプト/DB値に
残っていること）で Red になったことを確認済み。

- routing.py/shelving.py/store.py は無変更（ブリーフの境界制約どおり、shelf/service.py
  と tests/test_service.py のみ変更）。
- `_masked` は既存の `self._mask(x) if self._mask is not None else x` パターンの
  一括置換ではなく、今回変更した6箇所限定で使用（ブリーフの明示的なスコープ制約）。
- 検証: `uv run pytest -q`（1290 passed = 既存1283 + 新規7）・
  `uv run ruff check --no-cache .`（All checks passed）。
- 文書更新: SECURITY.md 脅威モデル節（title→title・description・persona へ拡張、
  ask/consult の専門家プロンプト構築も列挙に追加）・CHANGELOG.md [Unreleased] Security
  （description は title と異なり更新 API が無く notebook 再作成でのみ更新される旨を
  明記）・docs/adr/0002-masked-invariant-for-backend-text.md へ「追記（2026-08-07）」節。
- **棄却した案**: なし（ブリーフの実施順・対象箇所がそのまま実装可能で、代替案の
  検討を要する判断分岐は発生しなかった）。

### フレッシュレビュー追修正（must 1・should 1、2026-08-07）

上の修正完了後のフレッシュレビューで、悉皆性の見落としが2種類検出された。

- **must（shelver.py の working_catalog 経由の未 mask 流出）**: 上の修正ブリーフは
  対象を「全て shelf/service.py」に明示限定していたため、shelve() が委譲する
  `Shelver.plan()`（shelver.py・別モジュール）の working_catalog 経由の流出経路が
  検討対象から外れていた。分類 LLM 応答（`decision.description`）は取込資料の
  title/description と異なり「LLM が生成したテキスト」であり mask を一度も通って
  いない生データである点が、service.py 側の「DB 由来の既存行」ケースとは異なる
  新しい流出源だった。fix/prompt-title-mask の「関数名の grep だけでは不十分」
  教訓と同型で、今回は「モジュール境界（service.py 限定）で切った探索範囲」が
  見落としの原因だった。教訓: mask 不変条件のレビューはモジュール単位ではなく
  「LLM 出力・DB 由来テキストが次の呼び出しへ渡る全経路」を有向グラフとして
  追跡する必要がある。
  修正: `Shelver.__init__` に `mask: Callable[[str], str] | None = None` を追加し、
  working_catalog へ積む NotebookCard.description にのみ適用（`result.created`
  ＝永続化用の生データは無変更のまま維持し、永続化前 mask は service.py 側の
  既存責務を壊さない）。shelving.py（純粋関数層）は無変更。
- **should（読み出し系の残り2経路）**: `list_notebooks()`（MCP ツール出力）と
  CLI `shelf persona` 表示（人間向け出力）は、backend へのプロンプト構築点では
  ないため最初の修正の「backend へ送出されるテキスト」という文言の字面では
  対象外に見えたが、レビューは「AI エージェントの文脈へ直接流れる／人間の目に
  触れる」という実質的な露出面で捉えて指摘した。CLI persona 表示は
  fix/persona-lazy-service（`_build_service` を表示のみの分岐で呼ぶと実モデル DL
  で恒久ハングする既知の回帰）の制約と両立させる必要があり、`shelf.masking.mask`
  を関数内 import で直接使う軽量経路（`_build_service` 内の `from shelf.masking
  import mask` と同じ形）で解いた。既存の `test_display_only_path_does_not_build_service`
  ガードテストは無変更のまま green を維持。
- 検証: `uv run pytest -q`（1293 passed = 1290 + 新規3）・
  `uv run ruff check --no-cache .`（All checks passed）。routing.py/shelving.py/
  store.py は無変更（shelver.py・service.py・cli.py のみ変更）。
- 文書更新: SECURITY.md（shelve の増分カタログ・MCP list_notebooks・CLI persona
  表示を対象に追記）・CHANGELOG.md [Unreleased] Security に新エントリ追加・
  docs/adr/0002-masked-invariant-for-backend-text.md の追記節に、今回の見落とし
  原因（モジュール境界で切った探索範囲）と「backend へ送出される全テキスト」の
  解釈範囲（MCP ツール戻り値・CLI 表示を含む）の明確化を追加。
- **棄却した案**: なし。

### adversarial-verifier REJECT からの最終ラウンド追修正（2026-08-07）

上のフレッシュレビュー追修正の完了宣言を adversarial-verifier が REJECT した。
実証付きの穴1件（dry_run の JSON 出力）と、文書の過大宣言（不変条件の対象を
2項目の列挙で閉じてしまい実態を過小に見せていた）が指摘された。

- **見落としの原因**: dry_run 分岐（`shelve(dry_run=True)`）はこれまでのどの
  ラウンドのテストも通していなかった。`test_shelve_masks_created_notebook_description_before_persisting`
  等は全て `dry_run=False` の非 dry-run 経路のみを検証しており、`plan.created`
  （shelver.py が永続化用に意図的に保持する生 description）を dry_run=True の
  JSON レスポンスへそのまま積む分岐が、mask 適用漏れに気づけないまま素通しで
  残っていた。「永続化前に mask する」という直感が強すぎて、「永続化せずに
  そのままクライアントへ返す」経路（dry-run は非破壊なので副作用が無い＝
  安全、という誤った連想）を見落としたのが根本原因。reason フィールド
  （分類/ルーティング LLM の自由記述）も、description/persona/title が
  「DB 由来の既存データ」という同じカテゴリで扱われ続けたのに対し、reason は
  「LLM が都度生成する自由記述」という異なるカテゴリのデータであり、
  「DB 由来テキストの mask 漏れ」という探索フレームでは最初から検討対象に
  入っていなかった。
- **検証者の残存リスク指摘（4点）**: (1) dry_run の description/reason 未 mask
  （今回修正）、(2) consult 戻り値 routed[].reason 未 mask（今回修正）、
  (3) CLI persona 表示の `from shelf.masking import mask` は wheel 配布時に
  `distill/` ディレクトリを含まない場合に ImportError となりうる — これは
  本 ADR の修正群固有の問題ではなく `shelf/masking.py` の importlib 読み込み
  方式自体が持つ既存の系統的問題（pyproject.toml のパッケージデータ配布設定に
  依存）であり、本ラウンドのスコープ外として現状維持（後続課題）、
  (4) mask() の冪等性（二重適用しても安全という前提）は 50 万件規模のランダム
  文字列ファズテストで反例ゼロと別途確認済み — 二重防御設計の安全性根拠として
  有効。
- **教訓**: mask 不変条件の悉皆確認は「backend へのプロンプト」だけでなく
  「クライアントへ返る LLM 生成フィールド（description/reason）」も同一の
  データ追跡グラフに含める必要がある。「DB 由来の既存データ」と「LLM が
  都度生成する自由記述」は生成元が異なるため、片方のカテゴリでの探索完了を
  もう片方の完了と混同しないこと。
- 修正: `shelve()` の dry_run 分岐で `created_notebooks[*].description` と
  `plan[*].reason` に `self._masked` を適用、`_consult_target()` の `reason` に
  `self._masked` を適用。
- 検証: `uv run pytest -q`（1296 passed = 1293 + 新規3）・
  `uv run ruff check --no-cache .`（All checks passed）。
- 文書更新: SECURITY.md（不変条件の対象を「クライアント向け読み取り出力全般」
  という開いた書き方へ変更し、dry-run JSON・consult reason を明記。加えて
  「既知の制限」節にスコープ外事項2点を開示: ①索引時の要約チャンク経由の
  露出は投影時二重防御の対象外、②`ShelfService(shelver=...)` 直接注入は
  mask 配線をバイパスする死んだ注入口）・CHANGELOG.md [Unreleased] Security・
  docs/adr/0002-masked-invariant-for-backend-text.md 追記節を同様に更新。
- **棄却した案**: なし。

### adversarial-verifier 再検証 REJECT からの3度目の追修正（2026-08-07・収束方向）

再検証は「収束方向」としつつ、実装の穴1件（`routed[*].subquery` 未 mask）と
文書の過大宣言・事実誤りを指摘した。

- **見落としの原因（3度目の再発）**: `reason` を mask した際、同一のルーティング
  応答 JSON（routing.py:126-136）から一緒に取り出す兄弟フィールド `subquery` の
  掃引を行わなかった。fix/prompt-title-mask で確立した「関数名の grep ではなく
  同一データの全流出先を追跡する」教訓が、今回は「同一 JSON 応答内の兄弟
  フィールド」という単位で3度目の再発をした（1度目: title のプロンプト直渡し
  経路、2度目: shelver.py のモジュール境界、3度目: 同一 JSON の兄弟フィールド）。
  `subquery` はクライアント出力に加えて専門家プロンプトへも投入されるため
  `reason` より実害が大きい。教訓: LLM 応答の1フィールドを mask 修正する際は、
  同じパース結果オブジェクトが持つ他のフィールドも同時に洗い出す。
- **文書の過大宣言（過小の反動）**: 前々回のレビューで「列挙で閉じる過小」を
  指摘されたことへの反動で、前回の追記が「クライアントへ返る全ての LLM 生成
  フィールドは mask 済み」という実装を超える過大宣言になっていた（answer/
  insights/citations は mask していない）。過小と過大の両方を避けるため、対象を
  「ルーティング/分類のメタ情報フィールド」という性質で区切り、回答本文は
  「索引時の入力チャンク mask を根拠とした明示的除外」として書く方式に変更した。
- **事実誤りの訂正**: 前回追記した「再 add または再 index で自然に mask 済み値へ
  更新される」が虚偽だった。indexer は `documents.description` を書き換えない
  （upsert/update 呼び出しがない）ため、単純な再 index では更新されない。実際に
  更新されるのは `shelf add --desc` 明示指定時、または `auto_summary=True`
  （既定）での再投入時のみで、`shelf ingest` は `auto_summary=False` のため
  更新されない。SECURITY.md・ADR-0002 の該当箇所を訂正した。
- **packaging 制約の新規開示**: `shelf persona` 表示を含む masking 依存機能は
  `distill/extract.py`（`<リポジトリルート>/distill/`）の実在に依存するが、
  `pyproject.toml` の `packages = ["shelf"]` は wheel に `distill/` を含めない。
  リポジトリ直接 checkout・`SHELF_EXTRACT_PY` 指定以外の配布形態（pip 経由の
  wheel インストール等）では masking 依存機能が動作しないという既存の系統的
  制約を SECURITY.md 既知の制限へ新規開示した。
- **検証者が誤検知として棄却した項目**: shelve notes の `raw_name`（フォールバック
  notebook 名）は SECRET_RES 系正規表現（`sk-`/`ghp_`/`AKIA` 等）が要求する文字
  パターンと `validate_notebook_name` が許可する文字集合（英小文字・数字・-/_
  のみ）が構造的に排他であるため、secret を含み得ないと判定され false positive
  として棄却された。
- 修正: `_consult_target()` で `target.subquery` を読み出し点1箇所で
  `self._masked` に通し、クライアント出力（`routed[*].subquery`）・専門家
  プロンプト（`_answer_with_expert` の question 引数）の両方に使う。
- 検証: `uv run pytest -q`（1297 passed = 1296 + 新規1）・
  `uv run ruff check --no-cache .`（All checks passed）。
- 文書更新: SECURITY.md（不変条件をメタ情報フィールドへ絞り回答本文を明示的
  除外・事実誤りの訂正・packaging 制約の新規開示）・CHANGELOG.md
  [Unreleased] Security・docs/adr/0002-masked-invariant-for-backend-text.md
  「追記2」節を同様に更新。
- **棄却した案**: なし。

## Track S 完了: adversarial-verifier 最終判定 ACCEPT（2026-08-07）

3 ラウンドの反証検証で実証された穴 3 件（dry-run description / dry-run reason・
consult reason / consult subquery）が全て塞がれ、文書の宣言範囲（メタ情報
フィールドに限定・回答本文は根拠付き明示的除外）が実装と一致したことを検証者が
過大・過小の両方向からの悉皆列挙で確認し ACCEPT。最終状態: 1297 passed・
ruff clean・追加テスト 14 本・テスト削除 0 行。新テストの非空虚性は `_masked`
恒等化プラグインで failed になることを検証者が独立確認済み。

検証者の最終ラウンド指摘（後続課題として記録、本トラックのスコープ外）:
- **engines の stderr 素通し（新規指摘）**: `ask()` の `{"error": f"backend
  failed: {expert.error}"}`（service.py:394）と `consult()` の `warning`
  （service.py:1571）は `RawAnswer.error` を素通しする。コードコメントは
  「engines 側で安全な文言に整形済み」とするが、実際は `summarize_stderr` が
  サブプロセス stderr の先頭行をそのまま連結しており「整形済み」は過大表現。
  ADR-0002 の現行スコープ外だが後続課題に値する。
- **pyright 既存エラーの実数**: CI への pyright 導入（Track B）のスコープは
  「2 件」ではなく実質 11 件超（convert.py 4・doctor.py 1・emit_mcp.py 1・
  service.py 3・store.py 1 ほか。`"UTC" is unknown import symbol` 2 件は
  検証環境の Python 解決による artifact でコード欠陥ではない）。
- **`_masked` の戻り型**: `str | None -> str | None` のため service.py:1550
  （subquery を `_answer_with_expert(question: str)` へ渡す箇所）で pyright
  エラーが 1 件新規発生。実行時は routing.py の isinstance 検証 + フォール
  バック構成（subquery=question）で str が保証される。@overload 追加または
  当該箇所のインライン条件式で解消可能（Track B で扱う）。
