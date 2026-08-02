# 設計書: ハイブリッド QA MCP サーバ `shelf`（ローカル検索 + サブスク CLI 合成）

> **注記**: 本書は personal リポジトリ（非公開）の設計書を OSS 公開用にスクラブして移入したものである。移入元の履歴・環境固有情報は含まない。記述とコードが矛盾する場合はテストが正である。

- 対象ブランチ: `feature/shelf`
- 位置づけ: `recall/`（ローカル意味検索）の姉妹プロジェクト。**構造・運用感覚・テスト規約を recall に揃える**。
- 本書は設計のみ。実装コード・テストコードは含まない。

---

## 0. 目的とスコープ

### 目的
Claude Code から MCP 経由で「大量の資料・書籍コーパスに質問を投げ、**根拠（引用）付きの回答テキストだけ**を受け取る」委譲 QA レイヤを提供する。生の資料本文を Claude のコンテキストへ流し込まず、ローカル意味検索で top-k チャンク に絞り込み、サブスクリプション CLI エンジン（既定 codex、他 gemini・agy）で合成することで **Claude のトークン消費を最小化**する。NotebookLM の機能的代替であり、**「notebook ≒ コーパス」のメンタルモデル**を保つ。

### スコープ方針（recall と同じ哲学）
| する | しない（MVP 非スコープ） |
|---|---|
| Claude が明示的にツールを呼んだ時だけ動く（受動トークンコストゼロ） | フックによる自動注入 |
| ローカル意味検索（fastembed 384d）+ サブスク CLI エンジン（ハイブリッド RAG） | 単一クラウド依存・従量課金 LLM API |
| クエリ（`ask`）と notebook 一覧を MCP に公開 | 資料の投入（`add`）を MCP に公開（→ CLI 側・人間操作に限定。理由は §4-C） |
| ローカルファイル・URL の投入（スキャン PDF は MuPDF 内蔵 Tesseract で自動 OCR。既存テキスト層検出時は再 OCR をスキップし notes で明示） | 不安定な形式・OCR 制御用の新規 CLI フラグ・env（自動判定のみで YAGNI） |
| サブスク CLI（codex/gemini/agy）のプラガブル切替 | モデル固定 |

---

## 1. 決定事項と根拠（結論先行）

**File Search 案の破棄と新設計への移行（2026-07-07 ユーザー決定）。**
当初は Gemini File Search（Google のマネージド RAG）への薄いラッパを計画していたが、**ユーザー判断で破棄**された。理由は「クラウド送信は全面 OK だが従量課金の Gemini API は使わない」という方針のため。代わりに採用したのは**ハイブリッド RAG**：ローカル意味検索（recall 資産流用）で top-k チャンク に絞り込み、認可済みサブスク CLI エンジン（codex / gemini / agy）へ渡して合成。これにより API 課金を避けたまま、生データ転送を最小化できる。

**モジュール名は `shelf`。** MCP サーバ名・パッケージ名・console script をすべて `shelf` に統一する。採用理由: `recall`（5-6 文字の具体名詞/動詞）と対になる短い具体名詞。「1 つの本棚（＝この MCP サーバ）に複数の notebook（＝コーパス）が並ぶ」という比喩が、notebook のメンタルモデルと直結する。CLI が自然に読める（`shelf serve` / `shelf ls` / `shelf add`）。

**MCP に公開するツールは 2 つだけ**（`ask` と `list_notebooks`）。資料投入・notebook 作成・削除は **CLI（人間操作）** に置く。多ツール MCP がコンテキストを圧迫するというコミュニティの教訓と、recall が「クエリ専用 2 ツール（`memory_search`/`memory_get`）＋索引は CLI（`recall index`）」で切り分けている前例に厳密に従う。検索ツール（`search`）は追加しない。理由は「生チャンク露出は『トークン削減』という目的自体を崩す」（§4）。

**許可エンジンと課金モデル。** インストール済み CLI で認可済みもの（ChatGPT サブスク付き codex / OAuth 認可済み gemini・agy）のみを許可。ローカル LLM（llama.cpp 等）は将来の AnswerBackend 追加で対応予定（ハード制約: Apple M3·16GB では当面非推奨）。

**Ingest（投入）フロー。** notebook 名を `names.validate_notebook_name` で検証し存在確認を行い、パストラバーサル・無効名を防ぐ。PDF・Office・テキスト・URL を標準化 Markdown に変換し、corpus/<notebook>/<doc_id>.md に保存（元資料はコピーしない）。mask は **corpus 書き出し前に適用**し（ディスクに置くテキスト自体を保護。DEEP_DIVE でエンジンが原文を開いても未マスクテキストに触れない）、chunk 化時にも再適用する（正規表現マスクは冪等なので二重層は安全）。変換器は形式ごとに選定：
- **PDF**: pymupdf4llm（フォントサイズから見出し推定・`# ` 付き Markdown・`<!--page:N-->`マーカーで引用ページ直結）。markitdown 単独にしない理由は pdfminer ベースでフラット化され見出し構造が失われ、見出し優先チャンカーと相性が悪いため。
- **docx/xlsx/xls/pptx/html**: markitdown（extras 絞る: markitdown[docx,xlsx,xls,pptx]）。
- **md/txt/コード**: raw（バイナリ変換不要）。
- **URL**: http/https のみ許可。fetch は 20MB で読み取り打ち切り（容量・レイテンシ制御）。結果を markitdown で変換。
- **ファイル検証**: 投入ファイルは通常ファイル。形式は pdf/docx/xlsx/xls/pptx/md/txt/html のみ許可。違反は安全メッセージで拒否。
- **スキャン PDF**: pymupdf-layout 導入環境では `to_markdown` が既定で MuPDF 内蔵 Tesseract による OCR を自動実行する。ただし ABBYY/ScanSnap 等が作る透明テキスト層は Tesseract 形式と認識されず不要な再 OCR が発火するため、pymupdf でページテキストを事前サンプリングし、既存テキスト層を検出できた場合のみ `use_ocr=False` を明示して再 OCR を抑止し、その旨を notes で利用者に返す（`shelf/shelf/convert.py`）。テキスト層が無い純スキャン PDF は自動 OCR に委ねる。変換結果が 100 字未満の場合の fail-fast は、検出失敗時の最終フェイルセーフとして存置する。
- **ファイル origin の絶対パス正規化**: 単一ファイル `shelf add` でも、投入検証通過直後に `origin = str(Path(origin).resolve())` で絶対パスへ正規化・記録される。既存 DB に相対パス origin で登録済みのドキュメントがある場合、再 add で新 doc_id が割り当てられ旧行が重複して残る。必要に応じて `shelf rm <notebook> --doc <id>` で掃除可能。add_directory は既に resolve 済み絶対パスで記録しており、単一ファイルも同じ扱いに統一することで、相対/絶対パス表記揺れによる doc_id 分裂を防ぐ。

**Description（資料説明）の決定フロー。** `shelf add` の `--desc` / `--no-summary` で制御：
1. **`--desc` 明示・非空** → mask 適用のうえ `description_source='user'` で保存。これが最優先。
2. **`--desc` 未指定で `--no-summary` なし** → notebook の backend（既定 codex）で `build_summary_prompt`（入力は変換済み markdown 先頭 4000 字）+ `SUMMARY_SCHEMA` により自動生成。成功時は mask 適用のうえ `description_source='auto'` で保存。
3. **生成失敗（OK 偽 / パース失敗 / 例外）** → add を止めず。既存 description があれば維持、無ければ notes で「要約生成に失敗」を通知。
4. **`--no-summary` 指定** → backend を呼ばず、既存 description があれば維持する（消去しない）。description は検索対象化の基盤のため、「要約を要求しない」は「既存を保つ」と解釈する。
5. **ディレクトリ投入**: `--desc` は明示拒否（全ファイルに同一説明が付く誤メタデータの量産防止）。`--no-summary` は配下の全ファイルに伝播する。自動生成が有効な場合、codex をファイル数分呼ぶため遅い（1ファイル数十秒程度）。

**Corpus 管理と doc_id。** 元資料はコピーせず正規化 Markdown のみを corpus/<notebook>/<doc_id>.md に置く。doc_id = 元ファイル名 slug + sha256(`<notebook>:<origin>`) 先頭 8 桁。**notebook を含めて導出**するため、同一資料を複数 notebook に投入しても document が notebook 間で移動しない。これにより「エンジンに見せる面を『送ってよいと決めた変換済みテキスト』に限定」する統制を実現。

**AnswerBackend ポート。** API クライアントと subprocess 実行は 2 つのポートで隔離：`AnswerBackend` Protocol（name・answer メソッド）と `engines/runner.py`（subprocess 一本化・timeout+killpg）。backend は codex / gemini / agy / ollama として実装（`engines/{codex,gemini_cli,agy,ollama}.py` ファイル）、呼び出し方法（引数・スキーマ）は純粋関数で表現。これにより「ドメイン層はエンジン選択を一切知らない」設計を保証。

**クロスデバイス接続（`shelf serve --http`）。** MacBook から Tailscale VPN 経由で Windows 上の `shelf` に接続する運用のため、`shelf serve` に `--http`（既定は従来どおり stdio）・`--host`（既定 `127.0.0.1`）・`--port`（既定 `8765`）を追加した。streamable-http トランスポートで起動し、エンドポイントは mcp SDK 既定の `/mcp`。**bind は Tailscale ネットワーク内であることを前提とし、認証・アクセス制御は追加実装せず VPN 境界（Tailscale 自体の認可）に委ねる**（`0.0.0.0` 等の全ネットワーク公開は運用者の責任で行うこと）。

---

## 2. Ask フロー詳細と Grounding 判定

### Ask（質問）の実行フロー
1. **クエリ埋め込み**: 質問文を fastembed で 384 次元ベクトル化。
2. **ローカル検索**: SQLite から `cosine_topk(k=10, env: SHELF_TOP_K)`でチャンク取得。プロセス内行列キャッシュで不要な re-embedding を回避（meta generation カウンタで無効化）。
3. **プロンプト構成**: チャンクを `[S1] (source: <doc_id>.md, 節: §3.2, p.42)\n<本文>` 形式で番号付け。システムプロンプト: 「以下の抜粋のみを根拠に日本語で回答。各主張に [S番号] を付す。根拠が無ければ推測せず『資料からは分からない』と答え confident=false」。
4. **エンジン呼び出し**: AnswerBackend（既定 codex・notebook 単位で上書き可）へプロンプト+workdir+schema を渡す。
5. **出力パース**: 厳格 JSON `{answer, citations:[{s:int, ...}], confident:bool}` を期待。パース失敗時は生テキスト+grounded=false+warning で劣化返却（エラーで潰さない）。
6. **引用抽出**: citations[].s を S 番号に対応させ、source/section/page/quote（200 字切詰）を返却スキーマに埋める。

### Grounded 判定と Confident フラグ
- **grounded** = true iff confident == true ∧ citations.length ≥ 1 ∧ 全 S 番号が渡したチャンク範囲内（1 ≤ s ≤ len(chunks)。[S1] 起点の1始まり）。
- **confident**: エンジン出力の JSON フラグ。false = 推測が混じっているか不確実な回答を示す。
- claude はこのフラグで「資料に基づいているか」を判定し、不確実な回答（confident=false）に警戒。

### SHELF_DEEP_DIVE オプション（将来・既定無効）
環境変数 `SHELF_DEEP_DIVE=1` の場合、プロンプトに「引用元ファイルを開いて確認してよい。コマンド: cat <引用元パス>」を追加。codex の read-only サンドボックス内で実行されるため安全。レイテンシ増加を許容できる場合に利用。既定はチャンクのみ（レイテンシ優先）。

---

## 3. モジュール構成（recall と同型のレイヤリング）

```
shelf/
  pyproject.toml            # uv + hatchling, deps: mcp, fastembed, numpy, pymupdf4llm, markitdown[docx,xlsx,xls,pptx]
  shelf/
    __init__.py
    config.py               # env 上書き可能な設定（SHELF_DB_PATH・SHELF_ANSWER_TIMEOUT・SHELF_TOP_K 等）
    names.py                # [純粋] notebook 名検証・display_name 変換
    chunker.py              # [純粋・新規] 見出し階層と overlap を用いたテキスト分割・メタデータ(section/page)付与
    embedder.py             # recall 流用: fastembed ラッパ・`model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"`
    search.py               # recall 流用: cosine_topk・プロセス内行列キャッシュ・generation 無効化
    store.py                # recall 改修: catalog スキーマ・notebook/document/chunk CRUD
    prompts.py              # [純粋] ask プロンプト構成・[S番号] 付番
    convert.py              # 投入形式ごとの正規化: pymupdf4llm(PDF)・markitdown(Office/html)・raw(md/txt)・fetch(URL)
    indexer.py              # recall 流用: 増分索引・prune・状態管理
    ports.py                # AnswerBackend Protocol・RawAnswer/RetrievedChunk 中立 DTO
    engines/__init__.py      # 
    engines/runner.py        # subprocess 一本化: timeout+killpg・TemporaryDirectory・全エンジン共通
    engines/codex.py         # [実装] codex CLI(`codex exec`)呼び出し・--output-schema 強制
    engines/gemini_cli.py    # [実装] backend=gemini（gemini CLI呼び出し）・JSON フォールバック
    engines/agy.py           # [実装] agy CLI 呼び出し（JSON 出力なし、テキスト解析）
    service.py              # ユースケース: ask/list_notebooks/create_notebook/add_source/index。入口で notebook 名検証+存在確認
    server.py               # FastMCP・ask / list_notebooks の 2 ツール
    cli.py                  # serve / ls / new / add / rm / index / ask
  tests/
    fakes.py                # FakeAnswerBackend（canned 返却・呼び出し記録）
    test_*.py               # 各モジュール単体テスト（ネットワーク・実DB 不使用）
    test_boundaries.py      # import ガード: sqlite3→store のみ / subprocess→runner のみ / fastembed→embedder のみ / 変換lib→convert のみ
  .catalog/shelf.db         # メタデータ DB（git 管理外）
  corpus/<nb>/              # 正規化 Markdown ファイル群（git 管理外）
```

### 依存方向（内向き＝ドメインへ収束。DIP）
```
         cli.py ────┐                 server.py
           │        │                     │
           ▼        ▼                     ▼
    engines/*.py  catalog.py ──┐    service.py ──► ports.py(Protocol)
       (adapter)  (store.py)   │        │                 ▲
           │         │         │        ├──► prompts.py   │(純粋)
           ▼         ▼         ▼        ├──► chunker.py   │(純粋)
        runner  subprocess   sqlite3    ├──► convert.py   │(純粋)
                                        ├──► search.py    │(pure + embedder)
                                        ├──► indexer.py   │
                                        └──► store.py     │
                                            engines/* ──┘(Protocol を実装)
```
- **Import ガード**: `sqlite3` を import してよいのは `store.py` のみ。`subprocess` は `engines/runner.py` のみ。`fastembed` は `embedder.py` のみ。`pymupdf4llm`・`markitdown` は `convert.py` のみ。`test_boundaries.py` で自動強制。
- `service.py` / `prompts.py` / `chunker.py` / `search.py` は外部 SDK・DB・subprocess を一切知らない。ポートと純粋関数だけに依存。

---

## 4. MCP ツール仕様（最小・クエリ専用）

### A. `ask(notebook: str, question: str) -> dict`
指定 notebook のコーパスに質問し、**根拠付きの回答テキスト**を返す。§2 の ask フロー詳細に従う。

返却スキーマ（トークン効率と検証可能性の両立）:
```json
{
  "notebook": "physics-papers",
  "backend": "codex",
  "grounded": true,
  "answer": "<エンジンが合成した回答テキスト（[S番号]マーカー入り）>",
  "citations": [
    {"n": 1, "chunk_id": "<doc_id>#5>", "source": "feynman-lectures.md", "section": "§3.2",
     "page": 42, "quote": "<根拠チャンクの短い抜粋（既定 200 文字で切詰）>"}
  ],
  "warning": null
}
```
設計判断:
- **回答テキストを返す**のが本質（委譲 QA）。生の retrieved チャンク全文は返さない（それを返すとトークン削減という目的が崩れる）。
- **citations は軽量**: `chunk_id`（内部参照）＋ `source`（doc_id.md）＋ `section`（見出し）＋ `page`（PDF のみ・Optional）＋ **短い quote（切詰）**。Claude が全文を引かずに裏取りできる最小情報。section は通常の見出しのほか、`--desc` で明示指定した場合は「資料概要」、自動生成の場合は「資料概要（自動生成の要約）」となるため、引用元が人間入力か機械生成かを出典として透明に判別できる。
- **`grounded` フラグ**: 資料に裏打ちされているかを boolean で明示（§2「grounded 判定」参照）。
- **`backend`**: 実際に使用されたエンジン名（codex / gemini / agy）。ローテーション時のデバッグに有用。
- **`warning`**: パース失敗・エンジンエラーなど、成功だが警戒が必要な場合のテキスト（cf. エラー潰さない§1）。
- インライン `[S1]` マーカーは回答本文に埋め込まれている。grounding_supports のオフセット計算は将来（§10-3）。
- **`description`（ツール説明）は静的・最小**にする。「どの notebook に何が入っているか」を description に動的注入**しない**。理由: MCP のツール説明は常時ロードされるため、notebook が増えるほど常在トークンが膨らみ、機微な notebook 名が毎コンテキストに漏れる。discovery は明示的な `list_notebooks()` に委ね、「呼んだ時だけ払う」recall 哲学を保つ。

### B. `list_notebooks() -> list[dict]`
利用可能な notebook を列挙（discovery）。`ask` の前段。
```json
[{"notebook": "physics-papers", "description": "物理の論文50本", "backend": "codex",
  "sources": 50, "chunks": 1200}]
```
- `description`: catalog に人間が登録した文字列。
- `backend`: その notebook で使用されるエンジン名（既定は config で指定、notebook 単位で上書き可）。
- `sources`: 投入ドキュメント数。
- `chunks`: 索引済みチャンク数。

### C. なぜ `add_source` / `search` を MCP に公開しないか（重要な設計判断）
1. **`add_source` 非公開**: 投入（ファイル変換・索引化）は遅い副作用・課金対象・セキュリティ面が開く。人間の CLI 操作に限定し、Claude の自律判断で永続リソースを増やさない（§1 根拠 3）。ただし同一 Service を経由するので、将来インタラクティブ投入が要れば同じ境界の上に 3 つ目ツールを足すだけ（ドアは開ける / YAGNI）。
2. **`search` 非公開**: 生チャンクを MCP ツール経由で返すと「トークン削減」という目的自体を崩す。ask フロー内で top-k 検索は完結（クローズドボックス）。

→ 投入・管理は CLI（`shelf new/add/rm/index`）に置く。前例一致: recall も索引（`recall index`）は MCP に出さず CLI のみ。

---

## 5. データ管理（notebook と corpus）

### notebook 命名
- notebook 名は人間が付ける安全な識別子。`names.validate_notebook_name` で **`[a-z0-9_-]+`・長さ上限**に制限（純粋関数・境界で検証）。メタデータフィルタ注入を防ぐ。
- ローカルのみで参照。Gemini・他クラウド API を使う場合も、notebook 単位で backend（codex / gemini / agy）を指定可能（catalog の backend 列）。

### Corpus 配置と doc_id
- ディレクトリ構造: `corpus/<notebook>/<doc_id>.md`（git 管理外）。
- **doc_id** = 元ファイル名 slug + sha256(`<notebook>:<origin>`) 先頭 8 桁。**notebook を含めて導出**する（§1「Corpus 管理と doc_id」参照）ため、同一資料を複数 notebook に投入しても別々の doc_id が割り当てられ、notebook 単位で独立した document として管理される。再投入時の重複検出・更新に使用。
- **元資料はコピーしない**（corpus は正規化 Markdown のみ）。これにより「エンジンに見せる面を『送ってよいと決めた変換済みテキスト』に限定」する統制を実現。
- **各チャンク埋め込み** は SQLite chunks テーブルの embedding BLOB 列に保存。generation カウンタで無効化可能（プロセス内行列キャッシュを自動リセット）。

### ローカルメタデータ = SQLite（store.py）・マイグレーション戦略
JSON でなく SQLite を選ぶ理由: recall（SQLite）と整合、source が増えても素直にスケール・クエリ可能。配置 `shelf/.catalog/shelf.db`（git 管理外・env SHELF_DB_PATH で上書き可、recall の `.index/shelf.db` と同じ作法）。

description / description_source の 2 列追加は、Store 起動時（_migrate_documents_columns）に PRAGMA table_info で既存スキーマを検査し、不足列を ALTER TABLE で追補する冪等マイグレーション。既存 DB でも列なしで動作し（両列 NULL），新規投入時に徐々に設定される。

スキーマ（recall `store.py` のスタイルを踏襲）:
```sql
CREATE TABLE notebooks (
  name         TEXT PRIMARY KEY,          -- 検証済み人間名（[a-z0-9_-]+）
  description  TEXT,                       -- list_notebooks に出す
  backend      TEXT NOT NULL DEFAULT 'codex', -- 使用エンジン名（codex / gemini / agy）
  created_at   TEXT NOT NULL
);
CREATE TABLE documents (
  id            TEXT PRIMARY KEY,          -- doc_id（slug+sha8）
  notebook      TEXT NOT NULL REFERENCES notebooks(name),
  origin        TEXT NOT NULL,             -- 元ファイル名（パスやURL。重複検出用）
  origin_type   TEXT NOT NULL,             -- pdf | docx | md | url 等（投入形式）
  normalized_path TEXT NOT NULL,           -- corpus/<nb>/<doc_id>.md（内部参照）
  title         TEXT,                      -- 抽出タイトル（将来・UI用）
  converter     TEXT,                      -- pymupdf4llm | markitdown | raw （変換器記録）
  content_hash  TEXT,                      -- 内容チェックサム（変更検知）
  added_at      TEXT NOT NULL,
  fetched_at    TEXT,                      -- URL 投入時の fetch timestamp
  description   TEXT,                      -- 資料の説明/要約（--desc 明示 or codex 自動生成）
  description_source TEXT,                 -- 'user'(--desc明示) | 'auto'(codex自動生成) | NULL
  UNIQUE(notebook, origin)
);
CREATE TABLE chunks (
  id       TEXT PRIMARY KEY,                -- <notebook>/<doc_id>#<seq>
  notebook TEXT NOT NULL,
  doc_id   TEXT NOT NULL,
  source_path TEXT NOT NULL,               -- normalized_path（引用用）
  section  TEXT,                            -- 見出しテキスト or NULL。description から生成時は「資料概要」（--desc明示）or「資料概要（自動生成の要約）」
  page     INTEGER,                        -- PDF ページ番号 or NULL
  seq      INTEGER NOT NULL,               -- チャンク内シーケンス（本文 0 起点。description から生成されたチャンクは seq=-1）
  text     TEXT NOT NULL,
  embedding BLOB,                         -- numpy array（将来・プロセス内キャッシュ）
  dim      INTEGER,                       -- ベクトル次元（384）
  UNIQUE(notebook, doc_id, seq)
);
-- description の検索対象化: indexer が description 非空の document を seq=-1 の専用チャンク
-- (id=`{nb}/{doc_id}#-1`、source_path は本文と同じ、page=NULL) として自動生成・埋め込み。
-- section ラベルで出典透明性を確保（ask の citation に出た際に、自動生成か明示指定かを判別可）。
-- `shelf index` でも DB の description から再生成（backend 呼び出し不要）。
CREATE TABLE file_state (
  source_file TEXT PRIMARY KEY,
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  model       TEXT NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- schema_version / generation カウンタ
```

### CLI サブコマンド（recall 流儀）
```
shelf serve                                    # MCP サーバ(stdio)起動
shelf ls [notebook]                            # notebook 一覧 / 指定時は document 一覧
shelf new <notebook> [--desc TEXT] [--backend codex] # notebook 作成（catalog 登録）
shelf add <notebook> <path|dir>                # ファイル投入（形式判定→変換→corpus保存→索引化）。
      [--desc TEXT] [--no-summary]             # --desc: 資料の説明を明示指定（単一ファイル/URL のみ）
                                               # --no-summary: 説明の自動生成を行わない
                                               # ディレクトリ投入時は --desc は拒否、
                                               # --no-summary で auto_summary の方針を制御。
                                               # ディレクトリを渡すと配下を再帰走査し、対応形式
                                               # ファイルのみ一括投入する（隠しファイル/ディレクトリ・
                                               # symlink は除外。索引化は全件処理後に1回のみ）。
shelf rm  <notebook> [--doc-id ID]             # notebook 削除 or 特定 document 削除
shelf index [notebook] [--all]                 # 索引化（embedding 更新）。--all で全再構築
shelf ask <notebook> "<question>"              # デバッグ用 ask（service と同一）
```

---

## 6. 境界とテスト戦略（テストファースト設計）

### ポート: `AnswerBackend`（ports.py の Protocol）
サブスク CLI エンジンを抽象化し、ドメイン層が具体実装を知らない設計:
```python
@dataclass(frozen=True)
class RetrievedChunk:
    id: str; doc_id: str; source_path: str; section: str | None
    page: int | None; text: str

@dataclass(frozen=True)
class RawAnswer:
    text: str; ok: bool; error: str | None

class AnswerBackend(Protocol):
    name: str  # "codex" / "gemini" / "agy"
    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer: ...
```
- `RawAnswer` は **本プロジェクト定義の中立 dataclass**。`engines/*.py` が CLI 出力を詰め替え。
- `prompt` はシステムプロンプト+[S番号]付きチャンク。`workdir` は読み取り専用サンドボックス(corpus パス)。`schema` は JSON スキーマ（codex のみ使用、他は無視）。
- 戻り値 `error` は安全な要約のみ（コマンド全文・output 全文・認証情報を含めない）。

### Subprocess 一本化（engines/runner.py）
すべてのエンジン呼び出しを統一:
```python
def run_with_timeout(cmd: list, stdin: str, timeout: int, workdir: Path) -> (str, int):
    """subprocess.run(timeout=SHELF_ANSWER_TIMEOUT, start_new_session=True)
    + os.killpg で子プロセス残留防止 + TemporaryDirectory で一時ファイル管理."""
```
- **timeout**: env `SHELF_ANSWER_TIMEOUT`（既定 300 秒。codex の合成は 30〜90 秒かかるため余裕を持たせる）。超過時は TimeoutExpired → killpg して安全に終了。
- **start_new_session**: codex など子プロセスを張る cli を対象（プロセスグループ管理必須）。
- **一時ファイル**: TemporaryDirectory 内に出力ファイルを作成・読み込み・削除（外部ファイルシステム汚さない）。

### エンジン実装パターン（engines/{codex,gemini_cli,agy}.py）— backend codex / gemini / agy に対応
各エンジンは AnswerBackend Protocol を実装。構成:
1. **args 組立**: 純粋関数（test_engines で決定論テスト）。
2. **runner 呼び出し**: subprocess.run 統一・stdin でプロンプト渡す。
3. **出力パース**: JSON 期待・失敗時は RawAnswer(ok=False)。

### 各コンポーネントの単体テスト（ネットワーク・DB・プロセス不使用）
| 対象 | ダブル/手段 | テスト範囲 |
|---|---|---|
| `chunker`（純粋） | 手組みの text/metadata fixture | overlap / heading hierarchy / metadata 伝播 |
| `prompts`（純粋） | RetrievedChunk fixture | [S番号] 付番・指示文挿入 |
| `convert`（純粋・振り分けのみ） | 実変換はスモークのみ | 形式判定・fail-fast（100字未満） |
| `names`（純粋） | なし | 正当名の通過・不正名で例外 |
| `store`（境界） | `Store(":memory:")` | notebook/document/chunk CRUD・一意制約・generation カウンタ |
| `search`（pure + fake embedder） | recall 流用・FakeEmbedder | cosine_topk / キャッシュ無効化 |
| `service` | `Store(":memory:")` + `FakeAnswerBackend` | ask 経路・grounded 伝播・エラー処理 |
| `server` | 上記 service | 2 ツール登録のみ（ask/list_notebooks） |
| `cli.build_parser`（純粋） | なし | 各サブコマンド引数解釈 |
| `engines/*.{codex,gemini,agy}`(args 組立のみ) | pure function | CLI 引数の正確性 |
| `runner`（subprocess 軽量ラッパ） | /bin/echo / sleep スクリプト | timeout・killpg・exit code |

### `FakeAnswerBackend`（tests/fakes.py）
- 決定論的・ネットワーク不使用。canned `RawAnswer` を返す。呼び出し記録で副作用検証。

### 意図的に単体テストしないもの（recall と同じ割り切り）
- `engines/*.py` の実 CLI 呼び出し: スモークテストのみ（codex exec / gemini / agy の実表層は変わりやすい）。
- `cli.main` の実行配線: `build_parser` のみテスト、serve/add/ask は スモークで確認。

### Import ガード（test_boundaries.py 自動強制）
```python
import sys
def test_import_guards():
    # sqlite3 は store.py のみ
    assert "store" in [m for m in sys.modules if "sqlite3" in m]
    assert "service" not in [m for m in sys.modules if "sqlite3" in m]
    # subprocess は runner.py のみ
    # fastembed は embedder.py のみ
    # pymupdf4llm / markitdown は convert.py のみ
```


---

## 7. セキュリティ

- **API キー管理**: 従量課金 API は不使用（新設計）。代わりにサブスク CLI（認可済み codex / gemini / agy）のみ許可。各 CLI のトークンは既に ~/.config や ~/.kube に登録済み（認可フロー外）。
- **クラウド送信統制**: corpus 化したテキスト（正規化 Markdown）・質問・description（資料説明）のみが「クラウドエンジンへの送信対象」。生の投入ファイルは絶対に送らない。mask() 処理は corpus 書き出し前に適用（§1「ingest フロー」）し、chunk 化時にも再適用。description も mask 適用済みの状態で store に記録・backend へ送信される。mask→分割の順序厳守（recall 漏洩対策知見）。
- **パス検証**（`shelf add`）: 絶対パス解決・通常ファイルか・対応形式（pdf/docx/xlsx/xls/pptx/md/txt/html） を投入前に検証。不正は安全なメッセージで拒否。URL fetch は http/https のみ（スキーム制限）。
- **ディレクトリ投入の除外規則**（`shelf add` にディレクトリを渡した場合）: シンボリックリンク（ファイル/ディレクトリいずれも）判定は **resolve 前に行い**、非対応として skip する。その後、通常ファイルを `resolve()` で絶対パス正規化し記録。隠しファイル/ディレクトリ（相対パス構成要素が `.` 始まり）は記録すらせず除外、各ファイルは対応形式のみ対象。相対パスと絶対パスで同一ディレクトリを2回投入しても同一 doc_id に収束する（upsert 冪等）。
- **機微資料の注意明文化**: corpus に投入した資料は「サブスク CLI エンジン（codex は OpenAI・gemini は Google・agy は Gemini）へ送信・クラウド処理」。秘密・PII・機密資料を上げない運用注意を README の「する/しない」と併記（recall README の「/schedule に生ログ＝外部送信」注意と同じ位置づけ）。
- **エラー出力**: 内部詳細（スタックトレース・コマンド全文・出力全文・ファイルパス）を stdout に含めない。service.py で安全な要約に翻訳（ユーザーに見えるメッセージ化）。

---

## 8. タスク分割（TDD・実装順・done-criteria 付き）

各タスクは独立にビルド・テスト・revert 可能な最小単位。done-criteria はテストコマンド。担当エージェント表記は略（詳細は実装計画ファイル参照）。

> 共通制約: sqlite3 は `store.py` のみ / subprocess は `engines/runner.py` のみ / fastembed は `embedder.py` のみ / pymupdf4llm・markitdown は `convert.py` のみ / ネットワーク・実 DB・プロセス・時計に触れる単体テストを書かない / コミットは日本語・原子的。

| # | タスク | 依存 | done-criteria |
|---|---|---|---|
| T0 | **docs/design-shelf-mcp.md 全面改訂** ＋ **.gitignore（shelf エントリ追記）** | – | File Search 記述なし（破棄経緯のみ） |
| T1 | **scaffold**: pyproject.toml（mcp/fastembed/numpy/pymupdf4llm/markitdown[...]）＋ 空パッケージ＋ pyright 設定 | T0 | `uv run python -c "import shelf"` ・ `uv run pytest -q` collect 0 |
| T2 | **config.py**: env 設定（SHELF_DB_PATH・SHELF_ANSWER_TIMEOUT・SHELF_TOP_K・SHELF_DEEP_DIVE） | T1 | `uv run pytest tests/test_config.py` |
| T3 | **names.py（純粋）**: `validate_notebook_name` / `to_display_name` | T1 | `uv run pytest tests/test_names.py` |
| T4 | **chunker.py（純粋・新規）**: heading 階層・overlap・metadata(section/page)伝播 | T1 | `uv run pytest tests/test_chunker.py` |
| T5 | **embedder.py＋search.py**: recall 流用・fastembed 384d・cosine_topk・キャッシュ | T1 | `uv run pytest tests/test_embedder.py tests/test_search.py` |
| T6 | **store.py（recall 改修）**: スキーマ＋notebook/document/chunk CRUD・:memory: テスト | T1 | `uv run pytest tests/test_store.py` |
| T7 | **ports.py＋prompts.py＋convert.py＋fakes.py**: Protocol・prompt 構成・変換振り分け・Fake Backend | T1 | `uv run pytest tests/test_ports.py tests/test_prompts.py tests/test_fakes.py` |
| T8 | **indexer.py**: recall 流用・増分索引・metadata 更新 | T4,T5,T6 | `uv run pytest tests/test_indexer.py` |
| T9 | **service.py**（ask/list_notebooks/create/add/rm/index）＋ **test_boundaries.py** | T3,T5,T6,T7,T8 | `uv run pytest tests/test_service.py tests/test_boundaries.py` |
| T10 | **engines/runner.py＋engines/{codex,gemini_cli,agy}.py**: subprocess 統一・backend codex / gemini / agy の各実装 | T7 | `uv run pytest tests/test_runner.py tests/test_engines.py`（/bin/echo で決定論） |
| T11 | **server.py**: FastMCP・ask/list_notebooks 2 ツール | T9 | `uv run pytest tests/test_server.py` |
| T12 | **cli.py**: build_parser/dispatch（serve/ls/new/add/rm/index/ask） | T9,T11 | `uv run pytest tests/test_cli.py` |
| T13 | **README**: する/しない表・CLI 使い方・MCP 登録・クラウド送信注意 | T12 | docs レビュー |

### 検証ステップ（各タスク完了時）
- **T0 完了**: grep で File Search/GEMINI_API_KEY/FileSearchBackend が「破棄経緯」以外に残っていないか確認。
- **T1 完了**: `uv run python -c "import shelf"` 成功 / pytest collect 0 / ruff clean。
- **T9+T10**: import ガード（test_boundaries 自動検出）。
- **T11**: 公開ツール 2 つだけ（ask/list_notebooks）。
- **最終**: `uv run pytest -q && uv run ruff check .` 全緑。

---

## 9. トレードオフ記録（WHY）

- **File Search 案を破棄・ハイブリッド RAG へ移行**（従量課金 API 回避・トークン削減）: ローカル検索＋サブスク CLI で「送信量」と「課金」を最小化。ユーザー決定（2026-07-07）に従った設計転換（§1）。
- **MCP は 2 ツールのみ**（機能性 < コンテキスト圧迫回避・前例一致）: ingest・管理は CLI に閉じて、遅い副作用と誤送信面を人間操作に限定。MCP は「呼んだ時だけ払う」recall 哲学を保つ。ドア開ける（将来 3 つ目ツール追加可）/ 今は作らない（YAGNI）（§4-C）。
- **description に notebook 内容を動的注入しない**（利便 < 常在トークン抑制）: 1 往復（`list_notebooks`）を許容して常在コンテキストの肥大と機微名漏洩を避けた（§4-B）。
- **citations は quote 切詰**（完全性 < トークン効率）: 検証に足る最小抜粋のみ。全文が要るなら将来ツール（YAGNI）（§4-A）。
- **SQLite 採用**（JSON より重い < recall 整合・スケール・クエリ性）。
- **pymupdf4llm を PDF 最優先**（markitdown より重い < 見出し推定・ページ位置直結）: フラット化 vs 階層構造・チャンクメタデータの確実性（§1「ingest 選定根拠」）。

---

## 10. 未決事項

1. **Notebook 単位 DEEP_DIVE 列**: env `SHELF_DEEP_DIVE=1` で有効化し、実運用で需要を見る。catalog に backend 列に並ぶ deep_dive boolean 列を将来追加予定（今は env グローバル）。
2. **Whisper 転写（動画・音声）**: 将来タスク。MVP は転写 md の手動 `shelf add` で代替。
3. ~~**OCR（スキャン PDF）**~~: 解決済み。pymupdf-layout 導入環境では `to_markdown` の既定挙動として MuPDF 内蔵 Tesseract による自動 OCR が働く。透明テキスト層を事前検出できた場合は `use_ocr=False` + notes で再 OCR をスキップする（`shelf/shelf/convert.py`）。100 字未満の fail-fast は検出失敗時の最終フェイルセーフとして存置。
4. ~~**ローカル LLM バックエンド**~~: 解決済み。`engines/ollama.py` を追加し、Ollama `/api/chat` 経由のローカル LLM（RTX 4060 8GB 実機・既定モデル `qwen3:8b`）を呼び出す `AnswerBackend` 実装とした。リクエストボディ組立は純粋関数 `build_payload`（`schema` 指定時は Ollama の structured outputs 用 `format` キーへ渡して厳格 JSON 出力を強制、qwen3 系の thinking モードが JSON 出力と干渉しないよう `think: false` を常時付与）。HTTP は標準ライブラリ `urllib.request` のみを使用し新規ランタイム依存は追加しない。接続先は `SHELF_OLLAMA_URL`（既定 `http://127.0.0.1:11434`）・`SHELF_OLLAMA_MODEL`（既定 `qwen3:8b`）で env 上書き可能。`shelf new --backend ollama` で notebook 単位に選択できる。
   - **レイテンシ改善（2026-07-19）**: 実測でコールド consult 24.4s / ウォーム 9.1s、主因は Ollama 既定5分アンロードにより実運用（consult間隔は通常5分超）でほぼ毎回モデル再ロード(~15s)を踏むことと判明。(a) `build_payload` に `keep_alive` を追加（既定 `SHELF_OLLAMA_KEEP_ALIVE=60m`、空文字列で Ollama 既定へ委ねる脱出ハッチ）、(b) `engines/ollama.preload(url, model, keep_alive, timeout)` を新設し `shelf serve` 起動時（司書/既定バックエンド、または notebook 個別の backend 列のいずれかが ollama の場合）に `threading.Thread(daemon=True)` で1発非同期発火してモデルを先んじてロードする（fail-soft・Ollama 不在でも serve は正常起動）、(c) 司書ルーティングプロンプト（`routing.build_routing_prompt`）を「指示→カタログ→質問」の順へ並び替え、可変部（質問）を固定部（指示+カタログ）より後ろに置くことで Ollama の KV プレフィックスキャッシュを効かせる、の3点で対処した。
5. **codex --output-schema 実挙動**: T10 着手時に codex 0.142.3+ で導入版 CLI を再検証（互換性・スキーマ強制 vs フォールバック）。
6. **notebook 単位エンジン切替**: catalog backend 列に実装・運用要件次第（複数 API キー管理が必須になる）。
