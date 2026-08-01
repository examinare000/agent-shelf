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
