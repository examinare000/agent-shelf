shelf — a local-first RAG MCP server ("librarian") over your curated books/documents, with pluggable CLI engines (codex / gemini / agy / ollama).

# shelf

書籍・資料コーパスへの委譲QA（ローカル検索+サブスクCLI合成）MCPサーバ。

## 概要

shelf は、あなたの蔵書・資料コーパスをローカルで検索し、外部 LLM（codex / gemini / agy / ollama）に委譲して回答を生成する MCP サーバです。

**主な特徴:**
- **従量 API 不使用**: ローカル embeddings（FastEmbed）+ SQLite で検索
- **ハイブリッド RAG**: 検索結果をテンプレートにより複数の LLM エンジンへ同時投入し、最適なバックエンドで回答合成
- **エンジン抽象**: codex（Codex CLI）/ gemini（Gemini CLI）/ agy（Antigravity CLI・Gemini 系）/ ollama（ローカル）をプラグイン可能に。デフォルトは codex（無料枠利用可）
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
shelf new tech-books --desc "技術書: プログラミング・システム設計"
```

notebook 名は `^[a-z0-9_-]{1,64}$`（小文字英数字・`_`・`-`、1〜64字）に限定されます。
notebook 名は `corpus/` 配下のディレクトリ名や DB のフィルタキーにそのまま使われるため、
境界で一度だけ検証すればパストラバーサル・インジェクションの経路を塞げる
「単一検問所」として ASCII に限定しています（`shelf/names.py`）。

日本語のラベルや説明は `--desc` に載せてください。description は司書（consult の
ルーティング推論）が notebook を選ぶ際の判断材料としてプロンプトに載るため（「概要:」として
提示される）、内容を表す説明を書いておくとルーティング精度に直結します。

### 2. 資料の追加

```bash
shelf add tech-books ~/Documents/book1.pdf
shelf add tech-books ~/Documents/architecture.pdf
shelf add tech-books https://example.com/article.html
```

対応形式（`shelf/convert.py`）:

| 形式 | 拡張子 / スキーム | 変換器 |
|------|------------------|--------|
| PDF | `.pdf` | pymupdf4llm（ページマーカー付与。テキスト層があれば再 OCR をスキップ） |
| Office / HTML | `.docx` `.xlsx` `.xls` `.pptx` `.html` `.htm` | markitdown |
| テキスト / コード | `.md` `.txt` `.rst` `.py` `.js` `.ts` `.sh` `.toml` `.yaml` `.yml` `.json` | そのまま読み込み |
| URL | `http://` `https://`（サイズ上限 20MB） | markitdown |

対応形式は今後拡充予定です。

### 3. 埋め込みインデックスの構築

```bash
shelf index tech-books
```

インデックスは `.catalog/shelf.db` へ保存されます（gitignore 対象）。

## CLI コマンド一覧

`shelf --help` / `shelf <command> --help` が正です。概要:

| コマンド | 説明 |
|---------|------|
| `serve` | MCP サーバを起動（既定 stdio。`--http` で streamable-http、`--host` / `--port` / `--allowed-host` を併用） |
| `ls [notebook]` | notebook 一覧、notebook 指定時は document 一覧 |
| `new <notebook>` | notebook を作成（`--desc` 説明、`--backend` エンジン指定） |
| `add <notebook> <origin>` | 資料（ファイル・ディレクトリ・URL）を投入（`--desc` / `--no-summary`） |
| `rm <notebook>` | notebook 全体、または `--doc <id>` で個別 document を削除（`--yes` で確認スキップ） |
| `index <notebook>` | 索引化（`--all` で全ファイル再構築） |
| `ask <notebook> <question>` | デバッグ用: notebook を指名して質問 |
| `consult <question>` | 司書がルーティングして notebook を選び回答 |
| `digest <notebook>` | 資料から学びノートを生成（`--doc-id` / `--force`） |
| `shelve <directory>` | ディレクトリから自動分類投入（`--dry-run` で計画のみ） |
| `ingest <paths...>` | 一括投入（new→add→index→[digest] のオーケストレーション。`--notebook` / `--auto-shelve` / `--digest` / `--yes`） |
| `emit-mcp` | claude / codex / gemini 向け MCP 設定ファイルを生成（`--host` / `--transport` / `--url` / `-o`。登録は行わない） |
| `setup` | 対話式で backend 初期設定（config.env）を生成（`--yes` / `--answers-file`） |
| `persona <notebook>` | notebook の専門家ペルソナを表示・設定（`--set` / `--clear`） |
| `doctor` | 環境のプリフライト診断（エンジンCLI / ollama / DB / corpus / config.env / fastembed キャッシュ）。1つでも失敗があれば exit code 1 |

使用例:

```bash
# 複数資料をまとめて投入して索引化まで済ませる
shelf ingest ~/Documents/papers/*.pdf --notebook tech-books

# 司書に質問（notebook はルーティングで自動選択）
shelf consult "分散システムの結果整合性の設計指針は？"

# 学びノートの生成
shelf digest tech-books
```

## リモート提供（Tailscale + streamable-http）

既定の stdio に代えて、streamable-http トランスポートで別マシンから利用できます。
想定する信頼境界は Tailscale の tailnet（VPN）です。

サーバ側:

```bash
shelf serve --http --host <tailscale-ip> --port 8765 --allowed-host <magicdns-name>:8765
```

DNS リバインディング保護は有効のまま、bind 先（`host:port` と `host`）のみが既定の許可
Host になります。Tailscale MagicDNS 名（例 `myhost.tailXXXX.ts.net`）でアクセスする場合は
`--allowed-host` での追加指定が必須です（指定しないと「Invalid Host header」で
initialize が弾かれます）。

`SHELF_HTTP_ENABLED` を使う環境で stdio 登録する場合は、裸の `shelf serve` が env に
すり替えられないよう `--stdio` を明示してください。

クライアント側（Claude Code の例）:

```bash
claude mcp add --transport http --scope user shelf http://<host>:8765/mcp
```

**タイムアウト契約**: `consult` は司書ルーティング 1 回 + 選択された notebook（最大
`SHELF_ROUTE_TOP_N`）への回答生成で構成されるため、サーバ側の回答予算は最大
`SHELF_ANSWER_TIMEOUT`（既定 300 秒）× (1 + `SHELF_ROUTE_TOP_N`) に達し得ます
（既定構成で 600 秒）。クライアント側の MCP ツールタイムアウトがこれを下回ると
長い consult が途中で切れるため、クライアント設定を引き上げるか、サーバ側で
`SHELF_ANSWER_TIMEOUT` を短縮して整合させてください。

**推奨設定例（多段ルーティング）**: 複数 notebook にまたがる質問への回答精度を
上げたい場合、`SHELF_ROUTE_TOP_N=2`（コード側の上限と同値）+
`SHELF_ROUTE_FALLBACK=all`（ルーティング失敗時も対象ゼロにせず全 notebook を
横断）の組み合わせを推奨します。この設定でも、`consult` の専門家呼び出しは
並行化済み（`ThreadPoolExecutor`）のため上記の一般式ほど壁時計は伸びません。
司書ルーティング1回（直列）+ 選択された最大2 notebook への回答生成（並行実行の
max）という2段構成に留まるため、構成値から算出される理論上の最悪壁時計はおおよそ
`SHELF_ANSWER_TIMEOUT × 2`（既定構成で600秒）です。

**輻輳時の挙動**: `ask`/`list_notebooks`/`consult` はいずれもバックエンド呼び出しを
anyio のワーカースレッドプール（既定上限 40 スレッド）へ逃がして実行するため、
1 クライアントの長時間 consult が他クライアントの呼び出しをブロックすることは
ありません。ただし同時実行数が上限 40 を超えると、超過分はスレッドの空きが出る
まで待たされます（キューイング）。また各呼び出しは `abandon_on_cancel=True` で
実行しているため、クライアント側がタイムアウト等でキャンセルしても、対応する
ワーカースレッドのスロットは配下の処理（バックエンド呼び出し）が自然完了するまで
解放されません。`consult` は選ばれた最大 `SHELF_ROUTE_TOP_N`（上限2）件の専門家
呼び出しを ThreadPoolExecutor で並行実行するため、`consult` 1 件あたり最大 2 並行の
サブプロセス/接続が同時に発生し得ます。このため最悪同時サブプロセス/接続数は
ワーカースレッド上限 40 の最大 2 倍（80）になり得ます。

**認証は現状ありません**。接続元の制限は VPN（tailnet）境界に委ねる設計です。
パブリックネットワークに露出するアドレスへの bind は非推奨です（詳細は
[SECURITY.md](SECURITY.md) の脅威モデルを参照）。

**初回起動の注意**: 埋め込みモデル（fastembed）が未キャッシュだと、`serve` は
リスナーを bind する前にモデル DL を行うため、DL 完了までポートが開かず
`/health` も応答しません。サービス登録の前に一度対話環境で `shelf index` /
`shelf ask` 等を実行してキャッシュを温めておくか、`FASTEMBED_CACHE_PATH` を
永続的な場所に設定してから登録してください。

## ダイジェスト生成（digest）

shelf は大規模資料（数百頁の書籍など）から学びノートを効率的に抽出するため、**map-reduce パイプライン**を採用しています。

- **Map 段階**: 資料の本文を `SHELF_DIGEST_MAP_WINDOW_CHARS`（既定8000字）ごとに分割し、section 境界を優先して調整したウィンドウを構成。各ウィンドウから最大 `SHELF_DIGEST_MAP_NOTES`（既定5）件の学びを LLM で抽出。大規模資料ではウィンドウ数分の LLM 呼び出しが発生します（例: 60万字の資料≈75回）。
- **Reduce 段階**: 全ウィンドウから集約した学びを文書全体で重複統合し、最大 `SHELF_DIGEST_MAX_NOTES`（既定20）件に厳選。タグ3～8個を付与（既存タグカタログから選別、NFKC/lower で正規化）。

各学び（study_note）は根拠チャンク（source_chunk_ids / section / page）に接地して保存されます。

**LLM 呼び出し最適化**: 大規模資料で多くの呼び出しが発生する場合、`SHELF_DIGEST_BACKEND` でローカル ollama など低コストなバックエンドへ逃がせます（未指定時は notebook の backend → SHELF_DEFAULT_BACKEND の順で解決）。

## 環境変数一覧

設定の優先順位は「プロセス環境変数 > config.env（`shelf setup` が生成） > ハードコード既定値」です（`shelf/config.py`）。

| 環境変数 | 既定値 | 説明 |
|---------|--------|------|
| `SHELF_CONFIG` | `~/.config/agent-shelf/config.env` | 永続設定ファイル（config.env）の場所 |
| `SHELF_DB_PATH` | `<repo>/.catalog/shelf.db` | SQLite ローカル DB パス |
| `SHELF_CORPUS_DIR` | `<repo>/corpus` | コーパス投入ディレクトリ |
| `SHELF_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 埋め込みモデル |
| `SHELF_MODEL_CACHE_DIR` | `~/.cache/fastembed` | 埋め込みモデルのキャッシュ置き場（$TMPDIR 依存を避けるため固定） |
| `SHELF_DEFAULT_BACKEND` | `codex` | デフォルト LLM バックエンド（codex/gemini/agy/ollama） |
| `SHELF_TOP_K` | `10` | 検索結果の上位 K 件 |
| `SHELF_ANSWER_TIMEOUT` | `300` | LLM 応答タイムアウト（秒） |
| `SHELF_DEEP_DIVE` | `false` | 深掘り検索有効化（true/1） |
| `SHELF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama デーモン接続先 |
| `SHELF_OLLAMA_MODEL` | `qwen3:8b` | Ollama で使うモデル |
| `SHELF_ROUTER_BACKEND` | `` | 司書（ルーティング推論）専用バックエンド（未指定時は SHELF_DEFAULT_BACKEND を使用） |
| `SHELF_ROUTE_TOP_N` | `1` | ルーティングで選択する notebook 数（コード側の上限 2 でクランプ） |
| `SHELF_ROUTE_FALLBACK` | `` | ルーティング失敗時の方針（`all` = 全 notebook、空 = 対象ゼロ） |
| `SHELF_DIGEST_MAX_NOTES` | `20` | 資料全体で保持する学びノート数（reduce 後の上限） |
| `SHELF_DIGEST_MAP_NOTES` | `5` | 1 ウィンドウあたりの抽出学びノート数（map フェーズ） |
| `SHELF_DIGEST_MAP_WINDOW_CHARS` | `8000` | ウィンドウサイズ（字数）。section 境界優先で調整 |
| `SHELF_DIGEST_BACKEND` | `` | Digest 専用 LLM バックエンド（未指定時は notebook の backend → SHELF_DEFAULT_BACKEND） |
| `SHELF_HYBRID_SEARCH` | `true` | ハイブリッド検索有効化（cosine + FTS5 BM25 RRF）。SQLite が FTS5 非対応の場合は自動劣化 |
| `SHELF_SHELVE_BACKEND` | `ollama` | 自動分類・新規 notebook 生成時のバックエンド |
| `SHELF_MAX_FILE_MB` | `300` | ローカルファイル投入（add・shelve）のサイズ上限（MB）。誤投入・暴走防止用で、URL 投入の20MB上限とは別 |
| `SHELF_EXTRACT_PY` | `<repo>/distill/extract.py` | 機微情報マスク規則の読み込み元（下記参照） |
| `SHELF_HTTP_ENABLED` | `false` | `shelf serve --http` を CLI フラグなしで有効化する（true/1） |
| `SHELF_HTTP_HOST` | `127.0.0.1` | `--http` 時の bind ホスト（`--host` 未指定時のみ使用） |
| `SHELF_HTTP_PORT` | `8765` | `--http` 時の bind ポート（`--port` 未指定時のみ使用） |
| `SHELF_ALLOWED_HOSTS` | `` | DNS リバインディング保護の追加許可 Host（カンマ区切り、`--allowed-host` 未指定時のみ使用） |
| `FASTEMBED_CACHE_PATH` | OS 一時ディレクトリ配下 `fastembed_cache` | fastembed（埋め込みモデル）のキャッシュ先。`shelf` 独自の変数ではなく fastembed 本体が参照する変数です。Windows をサービスとして運用する場合、既定の一時ディレクトリはクリーンアップやサービスアカウント別 temp の影響でモデル DL がやり直しになり得るため、永続パスを明示することを推奨します |

## アップグレード・マイグレーション（0.3.x → 0.4.0）

v0.4.0 ではダイジェスト生成パイプラインが単発 LLM 呼び出しから **map-reduce 方式**へ変更になり、DB スキーマが拡張されました。既存デプロイを v0.4.0 へ更新する場合は以下の手順を実施してください。

### 破壊的変更
- 環境変数 `SHELF_DIGEST_INPUT_MAX_CHARS` は廃止しました（新パイプラインでは `SHELF_DIGEST_MAP_WINDOW_CHARS` で制御）。
- 既存の旧世代学びノート（pipeline=1）は自動認識され、新パイプラインで全件再生成対象になります。

### 移行手順
1. アプリケーションをバージョン v0.4.0 にデプロイ
2. サーバを再起動（DB マイグレーション・FTS インデックス初期化が自動実行される）
3. 各 notebook ごとに `shelf digest <notebook>` を実行し、学びノートを再生成（既存の旧ノートは置き換わります）
4. `shelf ask` / `shelf consult` でスモーク確認（検索・回答が正常に動作することを確認）

### 環境変数の更新
- `SHELF_DIGEST_INPUT_MAX_CHARS` を環境変数から削除（ローカルテスト環境の場合）
- 大規模資料で LLM 呼び出し回数が多い場合、`SHELF_DIGEST_BACKEND=ollama` でローカル LLM へ逃がすことを推奨

## 機微情報マスクの正本

**独立導入性**：agent-shelf は単体で自己完結します（依存する他リポジトリなし）。agent-recall との併用は任意で、併用時のみ SHELF_EXTRACT_PY によるマスク正本共有が意味を持ちます。

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
