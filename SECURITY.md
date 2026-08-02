# セキュリティポリシー

## 脅威モデル（現状の設計）

- **信頼境界は VPN（Tailscale tailnet）**: HTTP transport（`shelf serve --http`）に認証機構はなく、接続元の制限は tailnet 境界に委譲しています。パブリックネットワークに露出するアドレスへの bind は想定外であり、非推奨です。
- **DNS リバインディング保護は有効**: 許可 Host は bind 先（`host:port` と `host`）+ `--allowed-host` での明示追加のみ（`shelf/server.py` の `build_transport_security`）。VPN 境界内でもブラウザ経由攻撃の緩和として保護は無効化しません。
- **MCP surface は読み取り 3 ツールのみ**: `ask` / `list_notebooks` / `consult`。notebook 作成・資料投入・削除などコーパスを変更する操作は CLI（人間操作）に限定し、MCP には公開していません。
- **シークレットマスキングは取込時に適用**: 資料は corpus への永続化前に `mask()` を通します。マスク規則の正本は `distill/extract.py`（`SHELF_EXTRACT_PY` で差し替え可能。`shelf/masking.py` が importlib で読み込む単一ソース方式）。文書 title は「backend へ送出される全テキストは mask 済み」という不変条件（[ADR-0002](docs/adr/0002-masked-invariant-for-backend-text.md)）のもと、永続化時に加えて、ルーティングカタログ投影・要約/分類プロンプト構築・digest map/reduce プロンプト構築の直前でも再度 mask を適用する二重防御としています（修正適用前に永続化された既存行にも遡って有効）。

### 既知の制限（修正済み：2026-08-02）

マスク規則はクォート付き複数語の secret 値の過少マスク（例：`password: "correct horse battery staple"`）を修正済みです。ただし以下の制限が残ります:

- **JSON キー形式**: `"password": "..."` のようにラベルがクォートに包まれた形式は、ラベルと区切り文字（`:`）の間でクォートが終端するため新旧とも未マスク。
- **末尾非空白**: 閉じクォート直後に `,` `)` `}` `;` 等の非空白が続く形（JSON5/YAML flow/Python kwarg 等）は、短勝ちマッチ再発防止のため旧実装と同じ先頭トークンのみマスクに留まります（露出増なし）。
- **日本語引用符**: `「」` のような日本語引用符は新旧とも先頭トークン限定（vault の mask_vault のみ日本語ラベルを補完）。

詳細は [ADR-0008](docs/adr/0008-mask-quoted-value-pattern.md) および [docs/trial-log/mask-quoted-values.md](docs/trial-log/mask-quoted-values.md) を参照してください。

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
