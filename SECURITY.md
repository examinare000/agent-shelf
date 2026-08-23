# セキュリティポリシー

## 脅威モデル（現状の設計）

- **信頼境界は VPN（Tailscale tailnet）**: HTTP transport（`shelf serve --http`）に認証機構はなく、接続元の制限は tailnet 境界に委譲しています。パブリックネットワークに露出するアドレスへの bind は想定外であり、非推奨です。
- **DNS リバインディング保護は有効**: 許可 Host は bind 先（`host:port` と `host`）+ `--allowed-host` での明示追加のみ（`shelf/server.py` の `build_transport_security`）。VPN 境界内でもブラウザ経由攻撃の緩和として保護は無効化しません。
- **MCP surface は読み取り 3 ツールのみ**: `ask` / `list_notebooks` / `consult`。notebook 作成・資料投入・削除などコーパスを変更する操作は CLI（人間操作）に限定し、MCP には公開していません。
- **シークレットマスキングは取込時に適用**: 資料は corpus への永続化前に `mask()` を通します。マスク規則の正本は `distill/extract.py`（`SHELF_EXTRACT_PY` で差し替え可能。`shelf/masking.py` が importlib で読み込む単一ソース方式）。`SHELF_EXTRACT_PY` 環境変数を制御できる者は任意の Python ファイルを import 時に exec_module でき、マスク規則の無効化や arbitrary code execution が可能なため、この環境変数は信頼された管理者のみが設定可能な信頼境界に置いてください。文書 title・notebook description・persona、およびルーティング/分類の**メタ情報**フィールド（`reason`・`subquery`・新規作成 notebook の `description`）は「backend へ送出されるプロンプト、およびクライアントへ返るメタ情報フィールドは mask 済み」という不変条件（[ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md)）のもと、永続化時に加えて、ルーティングカタログ投影・要約/分類プロンプト構築・shelve の増分カタログ（新規作成 notebook の description）・digest map/reduce プロンプト構築・ask/consult の専門家プロンプト構築の直前でも再度 mask を適用する二重防御としています（修正適用前に永続化された既存行にも遡って有効）。**回答本文（`answer`/`insights`/`citations`）はこの二重防御の対象外**です — 索引時に入力チャンク（本文）が mask 済みであることを根拠に、生成された回答テキスト自体への追加 mask は行っていません。**クライアント向け読み取り出力全般**（MCP `list_notebooks` ツール・MCP `consult` 戻り値の `reason`/`subquery`/`persona`・CLI `shelf persona` 表示・`shelf shelve --dry-run` の JSON 出力の `description`/`reason`）も、メタ情報フィールドについては同じ不変条件の対象です。

### 既知の制限（修正済み：2026-08-02）

マスク規則はクォート付き複数語の secret 値の過少マスク（例：`password: "correct horse battery staple"`）を修正済みです。ただし以下の制限が残ります:

- **JSON キー形式**: `"password": "..."` のようにラベルがクォートに包まれた形式は、ラベルと区切り文字（`:`）の間でクォートが終端するため新旧とも未マスク。
- **末尾非空白**: 閉じクォート直後に `,` `)` `}` `;` 等の非空白が続く形（JSON5/YAML flow/Python kwarg 等）は、短勝ちマッチ再発防止のため旧実装と同じ先頭トークンのみマスクに留まります（露出増なし）。
- **日本語引用符**: `「」` のような日本語引用符は新旧とも先頭トークン限定。

修正の設計判断（クォート全体優先・改行除外・閉じ直後非空白での不採用）と「旧実装より露出を増やさない」不変条件の検証経緯は、`tests/test_masking.py` の `TestQuotedValueMasking` docstring と CHANGELOG の該当エントリを参照してください。

### 既知の制限（ADR-0002 の適用範囲外・後続課題）

- **索引時の要約チャンク経由の露出は投影時二重防御の対象外**: 修正適用前に取り込まれた `documents.description` の未 mask 既存行は、索引（index）が要約テキストをチャンク化して埋め込む経路を通じて backend へ流れる可能性があります。この経路には投影時二重防御が及んでいません。indexer は `documents.description` を書き換えないため（re-index では更新されません）、`shelf add --desc` で明示指定した場合、または `auto_summary=True`（既定）での再投入時のみ mask 済み値へ更新されます。`shelf ingest` は複数資料の一括投入用に `auto_summary=False` かつ `--desc` 相当の指定を持たないため、この経路では更新されません。
- **`ShelfService(shelver=...)` の直接注入は mask 配線をバイパスします**: 呼び出し側が独自の `Shelver` インスタンスを注入した場合、`_get_shelver()` が行う `mask=self._mask` の自動配線を経由しません。現状この注入口はテスト専用で実運用の呼び出し箇所はありません（死んだ注入口）。
- **masking 機能は packaging 上 `distill/` の実在に依存します**: `shelf persona` 表示を含む masking 依存機能は `shelf/masking.py` が importlib で読み込む `<リポジトリルート>/distill/extract.py` の実在に依存しますが、`pyproject.toml` の `packages = ["shelf"]`（wheel ビルド設定）は `distill/` を wheel に含めません。リポジトリを直接 checkout して実行する形態、または `SHELF_EXTRACT_PY` で代替パスを指定する形態以外の配布（pip 経由の wheel インストール等）では masking 依存機能が動作しません。

## 脆弱性、および悪意のあるコード・プロンプト指示の混入の報告

agent-shelf で脆弱性、および悪意のあるコード・プロンプト指示の混入を発見した場合は、
公開 Issue ではなく GitHub の Private vulnerability reporting
（リポジトリの Security タブ → Report a vulnerability）から非公開で報告してください。

- 対象範囲: 最新の `main` ブランチおよび最新のリリースタグ
- 対応方針: 個人メンテナによるベストエフォート対応です。応答時間・修正時期を保証するものではありません。

## Reporting a Vulnerability

Please report vulnerabilities and any suspected malicious code/prompt injection privately via GitHub
Private Vulnerability Reporting (the repository's Security tab → Report a vulnerability) rather than
filing a public issue. This applies to the latest `main` branch and the latest release tag. Fixes are
provided on a best-effort basis by an individual maintainer; no response time or fix timeline is
guaranteed.
