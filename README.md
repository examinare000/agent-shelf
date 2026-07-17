shelf — a local-first RAG MCP server ("librarian") over your curated books/documents, with pluggable CLI engines (codex / gemini / agy / ollama).

# shelf

書籍・資料コーパスへの委譲QA（ローカル検索+サブスクCLI合成）MCPサーバ。

## 概要

shelf は、あなたの蔵書・資料コーパスをローカルで検索し、外部 LLM（Codex / Gemini / Anthropic / Ollama）に委譲して回答を生成する MCP サーバです。

**主な特徴:**
- **従量 API 不使用**: ローカル embeddings（FastEmbed）+ SQLite で検索
- **ハイブリッド RAG**: 検索結果をテンプレートにより複数の LLM エンジンへ同時投入し、最適なバックエンドで回答合成
- **エンジン抽象**: Codex / Gemini / Anthropic / Ollama（ローカル）をプラグイン可能に。デフォルトは Codex（無料枠利用可）
- **司書（Librarian）**: ルーティング推論により、複数 notebook から最適な情報源を自動選別

## セットアップ

### 前提条件

- Python 3.11 以上
- [uv](https://github.com/astral-sh/uv)（パッケージマネージャ）

### インストール

```bash
git clone https://github.com/examinare000/agent-shelf.git
cd agent-shelf
uv sync
```

## MCP 登録

### Claude Code

```bash
claude mcp add shelf -- uv run --directory /path/to/shelf shelf serve
```

その後、Claude Code 内で `shelf` MCP サーバへアクセス可能になります。

### Codex CLI

`~/.codex/config.toml`（またはプロジェクト内 `config.toml`）に以下を追加：

| Key | Value |
|-----|-------|
| `mcp_servers.shelf.command` | `uv` |
| `mcp_servers.shelf.args` | `["run", "--directory", "/path/to/shelf", "shelf", "serve"]` |

またはTOML形式：

```toml
[mcp_servers.shelf]
command = "uv"
args = ["run", "--directory", "/path/to/shelf", "shelf", "serve"]
```

### Gemini CLI

`~/.config/gemini/settings.json`（またはプロジェクト内 `settings.json`）に以下を追加：

```json
{
  "mcpServers": {
    "shelf": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/shelf", "shelf", "serve"]
    }
  }
}
```

## コーパス投入（CLI）

### 1. ノートブック（カテゴリ）作成

```bash
shelf new "技術書" --desc "プログラミング・システム設計に関する書籍"
```

### 2. 資料の追加

```bash
shelf add "技術書" ~/Documents/book1.pdf
shelf add "技術書" ~/Documents/architecture.pdf
```

### 3. 埋め込みインデックスの構築

```bash
shelf index "技術書"
```

インデックスは `.catalog/shelf.db` へ保存されます（gitignore 対象）。

## 環境変数一覧

| 環境変数 | 既定値 | 説明 |
|---------|--------|------|
| `SHELF_DB_PATH` | `.catalog/shelf.db` | SQLite ローカル DB パス |
| `SHELF_CORPUS_DIR` | `./corpus` | コーパス投入ディレクトリ |
| `SHELF_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 埋め込みモデル |
| `SHELF_DEFAULT_BACKEND` | `codex` | デフォルト LLM バックエンド（codex/gemini/agy/ollama） |
| `SHELF_TOP_K` | `10` | 検索結果の上位 K 件 |
| `SHELF_ANSWER_TIMEOUT` | `300` | LLM 応答タイムアウト（秒） |
| `SHELF_DEEP_DIVE` | `false` | 深掘り検索有効化（true/1） |
| `SHELF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama デーモン接続先 |
| `SHELF_OLLAMA_MODEL` | `qwen3:8b` | Ollama で使うモデル |
| `SHELF_ROUTER_BACKEND` | `` | 司書（ルーティング推論）専用バックエンド（未指定時は SHELF_DEFAULT_BACKEND を使用） |
| `SHELF_ROUTE_TOP_N` | `1` | ルーティングで選択する notebook 数（最大） |
| `SHELF_ROUTE_FALLBACK` | `` | ルーティング失敗時の方針（`all` = 全 notebook、空 = 対象ゼロ） |
| `SHELF_DIGEST_MAX_NOTES` | `5` | 資料 1 つあたりの生成学びノート数 |
| `SHELF_DIGEST_INPUT_MAX_CHARS` | `4000` | ダイジェスト生成時の入力テキスト上限文字数 |
| `SHELF_SHELVE_BACKEND` | `ollama` | 自動分類・新規 notebook 生成時のバックエンド |
| `SHELF_EXTRACT_PY` | `<repo>/distill/extract.py` | 機微情報マスク規則の読み込み元（下記参照） |

## 機微情報マスクの正本

出力前のマスク処理（`shelf/masking.py`）は、規則の drift を防ぐため単一ファイル
`distill/extract.py` の `mask()` を importlib で読み込む設計。単体利用では同梱コピーが
そのまま正本になる。別頒布の [recall](https://github.com/examinare000/agent-recall)（記憶基盤）と併用する場合は、`SHELF_EXTRACT_PY` を
recall 側の `distill/extract.py` に向けることで、両者のマスク規則を確実に一致させられる。

## テスト実行

```bash
uv run pytest
```

カバレッジ付き実行：

```bash
uv run pytest --cov=shelf
```

## ライセンス

MIT License。詳細は [LICENSE](LICENSE) を参照。
