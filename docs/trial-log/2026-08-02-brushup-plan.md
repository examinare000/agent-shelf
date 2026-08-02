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
