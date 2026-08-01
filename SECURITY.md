# セキュリティポリシー

## 脅威モデル（現状の設計）

- **信頼境界は VPN（Tailscale tailnet）**: HTTP transport（`shelf serve --http`）に認証機構はなく、接続元の制限は tailnet 境界に委譲しています。パブリックネットワークに露出するアドレスへの bind は想定外であり、非推奨です。
- **DNS リバインディング保護は有効**: 許可 Host は bind 先（`host:port` と `host`）+ `--allowed-host` での明示追加のみ（`shelf/server.py` の `build_transport_security`）。VPN 境界内でもブラウザ経由攻撃の緩和として保護は無効化しません。
- **MCP surface は読み取り 3 ツールのみ**: `ask` / `list_notebooks` / `consult`。notebook 作成・資料投入・削除などコーパスを変更する操作は CLI（人間操作）に限定し、MCP には公開していません。
- **シークレットマスキングは取込時に適用**: 資料は corpus への永続化前に `mask()` を通します。マスク規則の正本は `distill/extract.py`（`SHELF_EXTRACT_PY` で差し替え可能。`shelf/masking.py` が importlib で読み込む単一ソース方式）。

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
