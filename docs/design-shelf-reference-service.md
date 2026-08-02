# 設計書: `shelf` 2層レファレンスサービス（司書ルーティング + 専門家ペルソナ/学びノート）

> **注記**: 本書は personal リポジトリ（非公開）の設計書を OSS 公開用にスクラブして移入したものである。移入元の履歴・環境固有情報は含まない。記述とコードが矛盾する場合はテストが正である。

- 対象ブランチ: `feature/shelf-remote-local-llm`
- 位置づけ: `docs/design-shelf-mcp.md`（以下「基盤設計書」）の**上に載る増分設計**。基盤設計書のレイヤリング（§3）・import ガード（§6）・「MCP 最小ツール」哲学（§4）を一切崩さず、その seam の上に 2 層 LLM 構成を追加する。
- 前提とする在庫（本書では設計せず「完成する」ものとして扱う）: 別エージェントが実装中の `engines/ollama.py`（Ollama `/api/chat` バックエンド・`AnswerBackend` Protocol 実装・qwen3:8b）と `cli.py` の `serve --http`（streamable-http トランスポート）。本書はこれらの現物に衝突する編集を提案しない。
- 本書は設計のみ。実装コード・テストコードは含まない。

---

## 0. 目的とスコープ

### 目的
Windows バックエンド（RTX 4060 8GB・qwen3:8b 単一常駐）を「**司書（router）+ 複数の専門家（expert）**」の 2 層に構成し、Mac クライアントから Tailscale VPN 越しに `shelf` MCP を叩くと、**notebook を指定しなくても**「どの資料に問うべきか」を司書が仕分け、当該専門家が「**抜粋（citations）と学び（insights）を分離して**」返すレファレンス体験を実現する。ハードウェア制約（fine-tuning 不可・複数モデル並行ロード不可・モデル切替 30 秒）を所与として、「専門家」を**同一ベースモデル + notebook 固有ペルソナ + 当該 RAG + 事前生成した学びノート**として実装する。

### スコープ方針（基盤設計書と同じ哲学を継承）
| する | しない（本増分の非スコープ） |
|---|---|
| 司書の入口ツール `consult(question)` を 1 つだけ追加（notebook 指定不要） | 生チャンクを返す `search` ツール（基盤 §4-C の判断を維持） |
| 専門家 = ペルソナ（system prompt）+ 学びノート（事前消化）で表現 | notebook ごとの fine-tuning・複数モデル常駐（ハード制約で不可） |
| 学びノート生成は明示コマンド `shelf digest`（人間操作・高コスト側作用を隔離） | `add` 時の自動学びノート生成（既定オフ。単一 GPU 直列を interactive 経路から外す） |
| 既存 `ask` は互換維持しつつ出力に `insights` を追加（additive） | `ask` の入出力を破壊する変更 |
| Mac クライアント資産と Windows バックエンド資産をディレクトリ分離 | サーバ内認証（境界は Tailscale VPN に委ねる） |

### 基盤設計書との整合（重要）
基盤設計書 §4 は「MCP に公開するツールは `ask` / `list_notebooks` の 2 つだけ」と決めた。本書はここに `consult` を**1 つだけ**足して 3 ツールにする。これは §4 の哲学の**破棄ではなく限定的拡張**である。根拠は §5-D に明記する（要旨: 「ルーティングはクライアント側では合成できない新capability」であり、`search`（生チャンク露出）とは性質が異なる）。

---

## 1. 決定事項と根拠（結論先行）

**結論: 2 層は「新モジュール 3 つ（すべてドメイン層 = 外部依存ゼロ）+ 既存 seam の再利用」で実現でき、新しい外部依存・新しい import ガード対象を 1 つも増やさない。** 司書も専門家も、推論呼び出しはすべて既存の `AnswerBackend` ポート（`engines/ollama.py` が実装）を通す。したがってドメイン層は「ローカルモデルか・司書か・専門家か」を一切知らないまま、`FakeAnswerBackend` だけで全経路を単体テストできる。

決定事項の要旨:

1. **司書 = `routing.py`（純粋）+ `librarian.py`（オーケストレーション）。** ルーティングの「判断ロジック」（プロンプト構成・structured 出力パース・フォールバック・top-N クランプ）を純粋関数 `routing.py` に隔離し、LLM 呼び出しの配線だけを `Librarian` クラス（`librarian.py`）に置く。`Librarian` は `AnswerBackend` ポートと `routing.py`（純粋）だけに依存し、store・corpus・sqlite・subprocess を一切触らない → **ドメイン層**に属し、`FakeAnswerBackend` で単体テスト可能。カタログ（notebook 一覧）は**データとして受け取る**（store には触れない）ことで循環依存を断つ。

2. **専門家 = ペルソナ（`notebooks.persona` 列）+ 学びノート（`study_notes` テーブル → `chunks.kind='digest'` として索引）+ `ask` の出力分離。** 「専門家」という重量級クラスは作らない。実体は (a) 純粋なプロンプト拡張（`prompts.py` に persona 注入・`insights` 分離）、(b) 純粋な学びノート生成（`digests.py`）、(c) store スキーマ拡張、(d) `service.py` / `indexer.py` のオーケストレーションに分散する。これが「単一責務 = 単一の変更理由」に最も忠実な分解になる。

3. **学びノートは既存の「description → seq=-1 サマリチャンク」パターンの richer な一般化。** 基盤設計書の description 自動要約（`_resolve_description` → indexer が seq=-1 チャンク化）が、まさに「モデルが事前消化した要点を索引対象に入れる」種になっている。学びノートはこれを「1 資料あたり複数の構造化された学び」に拡張し、`chunks.kind='digest'` として embedding・検索・prune・citation の既存機構をそのまま再利用する。

4. **`consult` の返却は「回答 + 抜粋（citations）+ 学び（insights）+ ルーティング透明性（routed）」。** 生の retrieved チャンク全文は返さない（基盤 §4-A のトークン削減原則を維持）。「抜粋 vs 学び」の分離は**検索レベルの `chunks.kind` で駆動**する（body → S 番号 = citations、digest → L 番号 = insights）。両者ともモデルが実際に参照した retrieved 内容に接地し、幻覚した「学び」を返さない。

5. **学びノート生成タイミングは専用コマンド `shelf digest`。** `add`（変換 + body 埋め込み・比較的軽い）/ `index`（再埋め込み・軽い）/ `digest`（LLM 合成・数十秒/資料・重い）の 3 コスト級を明確に分離する。単一 GPU 直列の高コスト推論を interactive な `add` 経路から外し、人間が計算予算を投じる時点を明示的に制御する（基盤 §4-C「遅い側作用・課金対象は CLI = 人間操作に限定」の踏襲）。

6. **セットアップ資産は `setup/mac/`（クライアント）と `setup/windows/`（バックエンド）に分離。** `shelf/` 本体は両者共有コードのまま。Mac は「remote streamable-http を指す MCP 登録」だけ、Windows は「Ollama 導入 + qwen3:8b pull + `shelf serve --http` 常駐化 + 電源/スリープ抑止」。認証境界は Tailscale VPN（サーバはループバック/Tailscale インターフェースにのみ bind）。

7. **`consult` は 3 番目の MCP ツールとして追加し、`ask` 互換は壊さない。** `ask` の出力に `insights` キーを additive に足す（キー追加は破壊的でない）。ペルソナ未設定 notebook の `ask` は従来と byte 単位で同じプロンプトになる（互換保証）。

---

## 2. 2層アーキテクチャ全体像

```
                    Mac クライアント (Claude Code)
                            │  MCP: consult / ask / list_notebooks
                            │  (streamable-http, Tailscale 越し)
        ════════════════════╪════════════════════ Tailscale VPN 境界（認証はここ）
                            ▼
                    Windows バックエンド: shelf serve --http
                            │
                       server.py (MCPServer 3 ツール)
                            │
                       ShelfService (ユースケース束ね)
             ┌──────────────┼───────────────────────────┐
             ▼              ▼                            ▼
    [層1 司書]        [層2 専門家]                 [既存 ask パイプライン]
    Librarian         persona + 学びノート          embed→search→prompt→backend
      │                  │                            │
      ├─ routing.py      ├─ digests.py (純粋)         ├─ prompts.py (persona/insights 拡張)
      │  (純粋)          ├─ store: persona/study_notes ├─ search.py / chunker.py
      ▼                  ▼                            ▼
    AnswerBackend ポート ◄──────────────────────────── AnswerBackend ポート
      │  （司書も専門家も同じポートを通る）
      ▼
    engines/ollama.py  → qwen3:8b（単一常駐・司書と専門家で共有・直列実行）
```

**1 回の `consult(question)` の流れ:**
1. `ShelfService.consult` が store から**カタログ**（notebook 名・description・persona・doc 数）を組み立てる。
2. `Librarian.route(question, catalog)` が qwen3:8b へ structured 呼び出し → 対象 notebook（top-N）・サブクエリ・理由・`answerable` を得る（`routing.py` がパース + フォールバック適用）。
3. `answerable=false` または対象ゼロ → 専門家推論を**呼ばずに**「資料からは分からない」を即返（高コスト推論の節約 = レイテンシ保護の既定）。
4. 対象 notebook ごとに**直列**で専門家 ask（`_answer_with_expert(notebook, subquery, persona)`）を実行。
5. 各専門家の（回答・citations・insights・grounded・透明性）を集約して返す。

**レイテンシ予算の明示（未決事項 §12-1 と対）:** 司書 1 回 + 専門家 N 回がすべて**同一モデルの直列推論**。総レイテンシ ≒ routing(数秒) + N×expert(数十秒)。既定 `top_n=1` はこの予算を守るための選択。

---

## 3. モジュール構成と依存方向

### 追加・変更するファイル（基盤 §3 のツリーへの差分のみ）

```
shelf/shelf/
  routing.py      [新規・純粋・ドメイン層] 司書の判断: build_routing_prompt / ROUTING_SCHEMA
                                          / parse_routing / apply_fallback / RoutingDecision
  librarian.py    [新規・オーケストレーション・ドメイン層] Librarian クラス。AnswerBackend ポート
                                          + routing.py だけに依存。route(question, catalog)
  digests.py      [新規・純粋・ドメイン層] 学びノート生成: build_digest_prompt / DIGEST_SCHEMA
                                          / parse_digest / StudyNote
  ports.py        [拡張] 中立 DTO 追加: NotebookCard / RouteTarget / RoutingDecision(再輸出) /
                                          StudyNote。RetrievedChunk に kind フィールド追加
  prompts.py      [拡張・純粋] build_ask_prompt に persona/学びノート(L番号)を注入。
                                          ANSWER_SCHEMA/parse_answer に insights([{l}]) 追加
  store.py        [拡張・境界] notebooks.persona 列 / study_notes テーブル / chunks.kind 列。
                                          冪等マイグレーション（既存 _migrate 方式）
  indexer.py      [拡張] study_notes を kind='digest' チャンクとして索引化。summary に kind='summary'
  service.py      [拡張] consult() / digest() / set_persona() を追加。ask() に insights/persona。
                                          Librarian を注入（既定は backend_factory から構築）
  server.py       [拡張] consult ツールを追加（3 ツール目）
  cli.py          [拡張] consult / digest / persona サブコマンド + router 配線
  config.py       [拡張] SHELF_ROUTER_BACKEND / SHELF_ROUTE_TOP_N / SHELF_DIGEST_MAX_NOTES 等
shelf/tests/
  test_routing.py / test_librarian.py / test_digests.py  [新規]
  test_prompts.py / test_store.py / test_indexer.py / test_service.py /
  test_server.py / test_cli.py / test_boundaries.py / fakes.py  [拡張]
```

### 依存方向（内向き = ドメインへ収束・DIP。基盤 §3 の図に追記）

```
   server.py ──► service.py ──► Librarian ──► routing.py (純粋)
                    │              │
                    │              └────────► AnswerBackend ポート ◄── engines/ollama.py
                    ├──► prompts.py (純粋: persona/insights)
                    ├──► digests.py (純粋)
                    ├──► indexer.py ──► chunker.py / store.py / embedder ポート
                    └──► store.py (sqlite3 境界: persona / study_notes / chunks.kind)
```

- **`Librarian` はドメイン層**。依存は `AnswerBackend`（ポート）と `routing.py`（純粋）と `ports.py` の DTO のみ。store・corpus・sqlite・subprocess・fastembed を import しない。→ `FakeAnswerBackend` だけで `route()` を単体テストできる。
- **カタログは service が組み立てて Librarian に渡す**（`Librarian` は store を知らない）。これが循環（service→librarian→store→…）を防ぎ、Librarian を「純粋に近い」テスト対象に保つ最重要の境界判断。
- **`consult` の集約は service の責務**。`Librarian.route` は「どこに問うか」だけを返し、専門家 ask の実行・集約は service が行う（層1 と層2 の責務分離）。

### import ガードへの影響（基盤 §6 / `test_boundaries.py`）
- **新規外部依存はゼロ。** `routing.py` / `librarian.py` / `digests.py` は `json`（stdlib）+ `ports.py` の DTO だけを使う。ゆえに `_RESTRICTED_TO_OWNER` に**新エントリを追加しない**。
- `test_boundaries.py` の `_DOMAIN_LAYER_FILES` に **`routing.py` / `librarian.py` / `digests.py` を追加**する。これで「この 3 ファイルが sqlite3/subprocess/fastembed/… を import したら即失敗」を自動強制でき、司書・専門家ロジックのテスト可能性を静的に守る。
- `test_domain_layer_files_are_present`（typo で検証集合が空になる保険）の期待集合も 3 ファイル分更新する。

---

## 4. データモデル（persona・学びノート・chunks.kind）

### 4-A. スキーマ拡張（すべて冪等マイグレーション = 既存 `_migrate_documents_columns` 方式）

```sql
-- notebooks: 専門家ペルソナ（system prompt）。NULL = ペルソナなし（ask は従来挙動）。
ALTER TABLE notebooks ADD COLUMN persona TEXT;   -- mask 適用済みで保存（§7-A）

-- chunks: チャンク種別。'body'（本文抜粋）| 'summary'（資料概要=既存 seq=-1）| 'digest'（学びノート）。
ALTER TABLE chunks ADD COLUMN kind TEXT NOT NULL DEFAULT 'body';

-- study_notes: 学びノートの source-of-truth（再 index で LLM 再呼び出し不要にするため DB に持つ）。
CREATE TABLE IF NOT EXISTS study_notes (
  id          TEXT PRIMARY KEY,          -- {notebook}/{doc_id}#d{n}
  notebook    TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,          -- doc 内の学び連番（0 起点）
  text        TEXT NOT NULL,             -- 学び本文（mask 適用済み）
  source_span TEXT,                      -- 由来（節・ページ範囲等）任意
  source_hash TEXT,                      -- 生成時点の正規化 md ハッシュ（陳腐化検出）
  model       TEXT,                      -- 生成に使ったモデル名（例 qwen3:8b）
  created_at  TEXT NOT NULL,
  UNIQUE(notebook, doc_id, seq)
);
```

**マイグレーション注意（`chunks.kind` の既存行）:** `ADD COLUMN ... DEFAULT 'body'` により既存の全チャンク（本文も既存 seq=-1 サマリも）が一旦 `'body'` になる。サマリチャンクの正しい `kind='summary'` は**次回 `shelf index` で indexer が seq=-1 を書き直す時**に付く。移行期間の誤ラベルは、読取り時に `seq==SUMMARY_SEQ` を最終フォールバックとして併用することで実害を消す（indexer / service 両方で seq による判別を保険に残す）。

### 4-B. 学びノートの格納と索引（既存 description パターンの一般化）

- **source-of-truth = `study_notes` テーブル**（`shelf digest` が LLM 合成して書く）。
- **検索対象化 = indexer が `study_notes` を読み、`kind='digest'` チャンクとして embedding して `chunks` に upsert する**（description → seq=-1 サマリチャンクと同型。ただし複数）。digest チャンクの `seq` は本文（≥0）・サマリ（-1）と衝突しない予約負域（`≤ -2`）を使い、`source_path` は本文と同一にする（`delete_by_source_file` / `prune_missing` / `rm --doc` の既存掃除が学びノートにも自然に及ぶ = 別経路の掃除を増やさない）。
- **mask は `study_notes` 書込み時点で適用済み**（§7）。indexer は description と同様、digest テキストに二重 mask をかけない。

### 4-C. 生成タイミングと再生成条件（コスト管理）

| コマンド | 何をするか | コスト | LLM 呼び出し |
|---|---|---|---|
| `shelf add` | 変換 → mask → 本文 embedding → 索引。（description 自動要約は既存の best-effort のまま） | 中 | description 要約時のみ（既存挙動） |
| `shelf index` | 既存チャンク + `study_notes` の再 embedding | 小 | **なし** |
| `shelf digest <nb> [--doc ID] [--force]` | 資料を専門家モデルで消化 → `study_notes` 書込み | **大（数十秒/資料・直列）** | **あり（専門家ペルソナで）** |

- **既定は `add` で学びノートを生成しない。** 高コスト直列推論を interactive 経路から外す。`shelf add --digest` で明示チェイン可（既定オフ・ドアは開ける/YAGNI）。
- **陳腐化検出:** `study_notes.source_hash` が現在の正規化 md ハッシュと異なれば stale → 再生成対象。`--force` は無条件再生成。**前提**: `documents.content_hash`（現状 NULL のまま）を ingest 時に「正規化 md のハッシュ」で埋めるようにする（§12-3 で明示。digest 陳腐化判定の土台）。
- **直列・進捗:** `digest` は 1 資料ずつ直列に処理し進捗を出す。失敗（ok=False/parse 失敗/例外）は 1 資料で全体を止めず、その資料をスキップして errors に記録（`add_directory` と同じ流儀）。

---

## 5. MCP ツール仕様（consult 追加・ask 拡張）

### 5-A. `consult(question: str) -> dict`（司書の入口・notebook 指定不要）

司書がルーティングし、選ばれた専門家が抜粋 + 学びを返す。返却スキーマ:

```json
{
  "question": "<原質問>",
  "answered": true,
  "routed": [
    {
      "notebook": "quantum-mechanics",
      "reason": "<司書がこの notebook を選んだ理由>",
      "subquery": "<司書が生成した検索用サブクエリ>",
      "score": 0.9,
      "backend": "ollama",
      "persona": "量子力学の専門家",
      "grounded": true,
      "answer": "<専門家が合成した回答（[S番号]/[L番号] マーカー入り）>",
      "citations": [
        {"n": 1, "chunk_id": "...", "source": "sakurai.md", "section": "§2.1", "page": 40,
         "quote": "<本文抜粋・200字切詰>"}
      ],
      "insights": [
        {"l": 1, "note_id": "quantum-mechanics/sakurai-ab12cd34#d0", "source": "sakurai.md",
         "text": "<事前消化した学び・切詰>"}
      ]
    }
  ],
  "warning": null
}
```

**設計判断（トークン効率 = 基盤 §4-A と同粒度）:**
- **生の retrieved チャンク全文は返さない**（基盤の目的を維持）。回答テキスト + 軽量 citations + 軽量 insights のみ。
- **抜粋（citations）と学び（insights）の分離は `chunks.kind` で駆動**。retrieved の body/summary → `[S#]` = citations、digest → `[L#]` = insights。insights も**実際に retrieved された学びノートに接地**（幻覚した学びを返さない）。
- **透明性（要件）:** `routed[]` に「どの notebook / どの専門家（persona）/ なぜ（reason）」を出す = 「経由した notebook/専門家の透明性」。
- **`answered` フラグ:** 司書が該当なしと判断した場合 `false` + `routed: []` + 説明文。専門家推論を呼ばずに返る。backend 呼び出し失敗時は説明文（`warning`）で区別する（`RouteOutcome.router_error` を診断として表面化・§6-D）。
- **`top_n=1` 既定で応答はコンパクト・レイテンシ有界**。`routed` は配列形なので N>1 にスキーマ変更なしで拡張可（ドア開ける/YAGNI）。
- **ツール `description` は静的・最小**。notebook 内容を動的注入しない（基盤 §4-B: 常在トークン肥大・機微名漏洩の回避）。discovery は `list_notebooks` に委ねる。

### 5-B. `ask(notebook, question)` の拡張（互換維持）

- 出力に **`insights` キーを追加**（retrieved digest チャンクから構成）。キー追加は additive で非破壊。
- notebook に persona があれば専門家プロンプトに注入。**persona が NULL なら従来と同一プロンプト**（互換保証・既存 `test_prompts` が緑のまま）。
- `ask` と `consult` は内部で共通コア `_answer_with_expert(notebook, question, persona)` を共有（DRY・両者を同じダブルでテスト）。

### 5-C. `ANSWER_SCHEMA` / `parse_answer` の拡張（純粋）
```json
{"answer": "str", "citations": [{"s": 1}], "insights": [{"l": 1}], "confident": true}
```
- `insights` は **required に含めない**（省略時 `[]`）。他バックエンド（codex/gemini/agy）や学びノート未整備 notebook でも従来通り検証が通り、`ask` 互換が保たれる。
- `s` は body/summary 抜粋番号、`l` は digest 学びノート番号。service が `s`→citations・`l`→insights に対応付ける（既存 `_build_citations` と対称な `_build_insights`）。

### 5-D. なぜ `consult` は追加してよく `search` は依然ダメか（§4-C の再確認）
- **`consult` を足す理由:** ルーティング（質問 → どの資料か）はクライアント側で `list_notebooks` + `ask` から合成できない**新しい capability**である。クライアントに合成させると、全 notebook のメタを常時コンテキストへ載せて自前ルーティングすることになり、まさに「呼んだ時だけ払う」哲学を壊す。司書を**サーバ側の 1 ツール**に閉じ込めるのが最小コスト。
- **`search` を依然足さない理由:** 生チャンク露出は「トークン削減」という目的そのものを崩す（基盤 §4-C）。`consult`/`ask` は回答 + 軽量引用のクローズドボックスを保つ。

---

## 6. 司書のルーティング仕様（`routing.py` 純粋 + `Librarian`）

### 6-A. 入力
- `question: str`（原質問）
- `catalog: list[NotebookCard]` — `NotebookCard(name, description, persona, doc_count)`。service が `store.list_notebooks()` + persona から組み立てる**投影 DTO**（routing 用の最小情報。store 型を Librarian に漏らさない）。

`build_routing_prompt` はプロンプトを「指示ブロック → notebook一覧（カタログ） → 質問」の順で組み立てる（2026-07-19 レイテンシ改善）。固定部（指示+カタログ）を先頭の安定プレフィックスにすることで Ollama の KV プレフィックスキャッシュを効かせ、可変部（質問）は末尾に置く（指示追従の観点でも定石）。

### 6-B. 出力 structured schema（`ROUTING_SCHEMA`）
```json
{
  "answerable": true,
  "targets": [
    {"notebook": "quantum-mechanics", "score": 0.9,
     "subquery": "スピン角運動量の交換関係", "reason": "スピンは量子力学ノートの主題"}
  ]
}
```
- `parse_routing(raw_text) -> RoutingDecision(answerable, targets, parse_ok)`。基盤 `parse_answer` と同じ `_extract_json_payload` を再利用（フェンス付き/前後ノイズ耐性）。パース失敗は潰さず `parse_ok=False`。

### 6-C. フォールバック方針（`apply_fallback` — 純粋・最重要のテスト対象）
純粋関数として `RoutingDecision` + カタログ + 設定（top_n・許可 notebook 集合）を入力に、最終的な実行対象リストを返す。分岐:

1. **カタログ空** → 対象ゼロ・`answered=false`「利用可能な notebook がありません」。専門家を呼ばない。
2. **`answerable=false`** → 対象ゼロ・`answered=false`「資料からは分からない」。**専門家推論を呼ばない**（レイテンシ保護の既定）。
3. **`parse_ok=false` または targets 空だが answerable** → 設定で選択（既定 = 保守的に「資料からは分からない」即答。`SHELF_ROUTE_FALLBACK=all` の時のみ全 notebook 横断へ、ただし後述の hard cap 内）。
4. **targets が存在** → (a) カタログに実在する notebook 名のみ残す（存在しない名は司書の幻覚として捨てる = 境界防御）、(b) `score` 降順で `SHELF_ROUTE_TOP_N`（既定 1・hard cap 2）にクランプ、(c) 同一 notebook の重複除去、(d) `subquery` 空なら原 `question` にフォールバック。

**ポイント:** 「司書が返した notebook 名を検証してからしか使わない」ことで、structured 出力の幻覚がパストラバーサルや未知 notebook アクセスに化けるのを防ぐ（`validate_notebook_name` + カタログ実在チェックの二重境界）。この分岐は全て純粋関数で、手組み `RoutingDecision` fixture だけで網羅テストできる。

### 6-D. `Librarian` クラス（`librarian.py`・オーケストレーション）
```
Librarian(backend: AnswerBackend, *, top_n: int, fallback: str)
  route(question, catalog) -> RouteOutcome:  # targets: list[RouteTarget], router_error: str | None
      prompt = routing.build_routing_prompt(question, catalog)
      raw    = backend.answer(prompt, workdir=..., schema=routing.ROUTING_SCHEMA)
      if not raw.ok: decision, router_error = RoutingDecision(answerable=?, parse_ok=False), raw.error  # 安全側 + 診断
      else:          decision, router_error = routing.parse_routing(raw.text), None
      targets = routing.apply_fallback(decision, catalog, top_n, fallback)
      return RouteOutcome(targets=targets, router_error=router_error)
```
- `backend` は注入（既定は service が `backend_factory(config.ROUTER_BACKEND or default_backend)` で構築）。テストは `FakeAnswerBackend(canned=routing_json)` を渡すだけ。
- `workdir` は routing では読取り専用サンドボックスとして corpus ルート等を渡す（Ollama は無視、codex 系のみ利用）。

---

## 7. 専門家（ペルソナ + 学びノート生成 = `shelf digest`）

### 7-A. ペルソナ（`notebooks.persona`）
- `shelf new <nb> --persona TEXT` または `shelf persona <nb> <TEXT>`（`service.set_persona`）で設定。
- **mask を storage 時に適用**する。理由: persona は system prompt として backend へ送られ、backend が codex/gemini/agy の場合クラウドに出る。「backend へ送る全テキストは mask 済み」という基盤 §7 の統制不変条件を persona にも適用する（冪等なので無害・一貫性優先）。
- `build_ask_prompt(question, chunks, deep_dive, persona=None)`: persona 非 None のとき指示文の冒頭に「あなたは <persona> である」を注入。**None のとき文字列は従来と完全一致**（互換）。

### 7-B. 学びノート生成（`digests.py` 純粋 + `service.digest`）
- `build_digest_prompt(markdown, *, persona, title)`: 「この資料の要点と、そこから得られる学び（洞察）を N 件、各 M 字以内で。本文にない内容を推測で補わない」。入力は先頭 N 字に切詰（基盤 `SUMMARY_INPUT_MAX_CHARS` と同思想）。
- `DIGEST_SCHEMA`: `{"notes": [{"text": "str", "span": "str"}]}`。`parse_digest(text) -> list[StudyNote]`（不正/空/上限超過を安全に処理。上限 `SHELF_DIGEST_MAX_NOTES`）。
- `service.digest(notebook, doc_id=None, force=False)`: 対象 doc を選び（doc_id 未指定なら notebook 全 doc）、専門家ペルソナで `backend.answer(build_digest_prompt(...), schema=DIGEST_SCHEMA)` → `parse_digest` → mask → `store.replace_study_notes(...)`。生成後は当該 notebook を `index` して digest チャンクを検索対象へ。stale 判定は `source_hash`。

### 7-C. 「血肉化」の実体（要件の言葉との対応）
- **事前消化された学びノートの索引化** = §4-B（`study_notes` → `kind='digest'` チャンク → 検索対象）。
- **ask 応答での抜粋（citations）+ 学び（insights）の分離返却** = §5-A/§5-C（`kind` 駆動の S/L 分離）。
- 専門家は「同一 qwen3:8b + persona + RAG + 学びノート」。モデル切替・fine-tuning はしない（ハード制約の所与を honor）。

---

## 8. リポジトリのセットアップ資産分離（Mac クライアント / Windows バックエンド）

### 8-A. 提案ディレクトリ構成
```
setup/
  mac/        # クライアント（司書へ問い合わせる側。Ollama も shelf serve も動かさない）
    README.md                 # 前提（Tailscale 接続・Windows バックエンド稼働）と手順
    register-shelf-mcp.sh     # shelf MCP を streamable-http で Windows バックエンドへ向けて登録
    env.example               # SHELF_REMOTE_URL=http://<win-tailscale-host>:<port>/mcp 等
  windows/    # バックエンド（Ollama + shelf serve --http 常駐）
    README.md
    install-ollama.ps1        # Ollama 導入（winget/公式）
    pull-models.ps1           # qwen3:8b を pull（単一常駐モデル）
    serve-shelf.ps1           # shelf serve --http 起動ラッパ（bind は Tailscale/loopback）
    register-task.ps1         # タスクスケジューラで serve-shelf.ps1 を常駐化（ログオン時/再起動時）
    power-settings.ps1        # スリープ抑止・高パフォーマンス電源プラン・GPU 常時稼働
    env.example               # SHELF_OLLAMA_URL / SHELF_DEFAULT_BACKEND=ollama / SHELF_ROUTER_BACKEND=ollama / SHELF_HTTP_HOST/PORT
```

### 8-B. 既存 `bootstrap/` との責務分界
- `bootstrap/`（`bootstrap.sh`/`bootstrap.ps1`）は**汎用 Claude Code 環境**（repo clone・`~/.claude`・superpowers・MCP 登録の総合ブートストラップ）を担い続ける。
- `setup/{mac,windows}/` は**この 2 層レファレンスサービス固有のロール provisioning**（Ollama・モデル・常駐化・電源・remote MCP 向き先）に限定する。両者は重複せず、`setup/*` は `bootstrap` を前提に「その後に叩く役割別スクリプト」。
- **`shelf/` 本体は共有コード**。Mac も Windows も同じ `shelf` パッケージ。Mac は `shelf serve` を動かさず MCP クライアント登録のみ、Windows が `shelf serve --http` を常駐。

### 8-C. Mac クライアントの MCP 登録（remote streamable-http）
- 現状 `bootstrap/manifest.json` の shelf エントリは stdio（`uv run --directory .../shelf shelf serve`）。Mac クライアントでは**これを remote URL エントリ**（streamable-http トランスポート・`http://<win-tailscale-host>:<port>/mcp`）に差し替える。
- **本書は `manifest.json` を編集しない**（http トランスポートは在庫作業に属する）。intended shape のみ規定: Mac は URL 参照、Windows は serve 側。実装時に http 在庫の I/F と突き合わせる。

### 8-D. 認証境界（Tailscale）
- サーバ内認証は実装しない（所与）。代わりに `shelf serve --http` は **Tailscale インターフェース / loopback にのみ bind**（`0.0.0.0` 公開しない）。`SHELF_HTTP_HOST` の既定を公開アドレスにしない設計上の防御を `serve-shelf.ps1` / 在庫の serve 実装で担保する（§12-6 で在庫側と要確認）。
- README に「corpus と学びノートは Ollama（ローカル）で処理されるが、notebook backend を codex/gemini/agy にした場合はクラウド送信になる」旨を明記（基盤 §7 の運用注意を継承）。

---

## 9. 境界とテスト戦略（テストファースト設計）

### 9-A. 各コンポーネントの単体テスト（ネットワーク・実 DB・プロセス・時計 不使用）
| 対象 | ダブル/手段 | テスト範囲 |
|---|---|---|
| `routing`（純粋） | 手組み `RoutingDecision`/`NotebookCard` fixture | prompt 整形・parse（正常/不正/部分 JSON）・apply_fallback 全分岐（空カタログ・answerable=false・幻覚 notebook 除去・top-N クランプ・重複除去・subquery 空補完） |
| `librarian` | `FakeAnswerBackend(canned=routing_json)` | route の配線・backend 失敗時の安全側フォールバック・schema 引き渡し |
| `digests`（純粋） | 手組み markdown fixture | prompt 切詰・parse_digest（正常/空/上限超過/不正）・StudyNote 抽出 |
| `prompts`（純粋・拡張） | RetrievedChunk fixture（kind 付き） | persona 注入（None で従来一致）・S/L 二系統付番・insights 指示・parse_answer の insights |
| `store`（境界・拡張） | `Store(":memory:")` | persona CRUD・study_notes CRUD/UNIQUE・chunks.kind・**マイグレーション冪等性**（旧 DB を開いて列追加が壊さない） |
| `indexer`（拡張） | `Store(":memory:")` + `FakeEmbedder` | study_notes→kind='digest' チャンク化・summary の kind='summary'・prune が digest にも及ぶ |
| `service.consult` | `Store(":memory:")` + `FakeAnswerBackend([routing_json, answer_json])` + 実 or Fake Librarian | route→専門家直列 fan-out→集約・answerable=false 短絡・透明性(routed)・insights 分離 |
| `service.digest` | `Store(":memory:")` + `FakeAnswerBackend(digest_json)` | 生成→study_notes 書込み・source_hash 陳腐化・force 再生成・失敗スキップ |
| `service.ask`（拡張） | 既存 + persona 有/無 | persona 注入をキャプチャ prompt で検証・insights 出力・**persona なしで従来出力と一致** |
| `server`（拡張） | 上記 service | 3 ツール登録（ask/list_notebooks/**consult**） |
| `cli`（拡張） | build_parser のみ | consult/digest/persona サブコマンド引数解釈 |

### 9-B. `FakeAnswerBackend` の再利用（既存で足りる）
既存 `FakeAnswerBackend` は **canned に list を渡すと呼び出し順に消費**する（`tests/fakes.py`）。`consult` は「routing 呼び出し → 専門家 answer 呼び出し」の順で 1 つの backend を使うため、`FakeAnswerBackend([routing_json, answer_json])` でそのままテストできる。**拡張は原則不要**。呼び出し順が非決定的なテストが将来必要になった場合のみ、schema 同一性でルーティングする薄いヘルパを追加する（YAGNI 判断: 今は作らない）。`FakeLibrarian`（route が固定 targets を返す）は consult の集約ロジックを routing から切り離してテストしたい時に追加。

### 9-C. import ガード（`test_boundaries.py` 拡張）
- `_DOMAIN_LAYER_FILES` に `routing.py` / `librarian.py` / `digests.py` を追加 → 3 ファイルの外部依存 import を静的に禁止。
- `_RESTRICTED_TO_OWNER` は**変更なし**（新外部依存ゼロ）。
- `test_domain_layer_files_are_present` の期待集合を更新。

### 9-D. 意図的に単体テストしないもの（基盤と同じ割り切り）
- `engines/ollama.py` の実 HTTP 呼び出し・`serve --http` の実配線（在庫・スモークのみ）。
- `setup/*` スクリプト（shellcheck / PSScriptAnalyzer の静的検査 + doctor スモークのみ）。
- `cli.main` の実行配線（build_parser のみ単体・実行はスモーク）。

---

## 10. タスク分割（TDD・実装順・done-criteria 付き）

各タスクは独立にビルド・テスト・revert 可能な最小単位。done-criteria はテストコマンド。**共通制約:** sqlite3 は `store.py` のみ / subprocess は `engines/runner.py` のみ / fastembed は `embedder.py` のみ / `routing.py`・`librarian.py`・`digests.py` は外部依存を一切 import しない / ネットワーク・実 DB・プロセス・時計に触れる単体テストを書かない / コミットは日本語・原子的。**在庫（`engines/ollama.py`・`serve --http`）に衝突する編集をしない。**

| # | タスク | 依存 | 並行 | 推奨エージェント | done-criteria |
|---|---|---|---|---|---|
| R0 | **config.py 拡張**: `SHELF_ROUTER_BACKEND`/`SHELF_ROUTE_TOP_N`(既定1)/`SHELF_ROUTE_FALLBACK`/`SHELF_DIGEST_MAX_NOTES`/`SHELF_DIGEST_INPUT_MAX_CHARS` | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_config.py` |
| R1 | **ports.py 拡張**: `NotebookCard`/`RouteTarget`/`RoutingDecision`/`StudyNote` DTO・`RetrievedChunk` に `kind: str='body'` 追加 | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_ports.py` |
| R2 | **store.py 拡張**: `notebooks.persona`・`study_notes` テーブル・`chunks.kind`・冪等マイグレーション・CRUD（persona/study_notes/kind）・`content_hash` 埋め | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_store.py` |
| R3 | **routing.py（純粋・新規）**: `build_routing_prompt`/`ROUTING_SCHEMA`/`parse_routing`/`apply_fallback` | R1 | ✓ | tdd-strict-coder | `uv run pytest tests/test_routing.py` |
| R4 | **digests.py（純粋・新規）**: `build_digest_prompt`/`DIGEST_SCHEMA`/`parse_digest`/`StudyNote` 抽出 | R1 | ✓ | tdd-strict-coder | `uv run pytest tests/test_digests.py` |
| R5 | **prompts.py 拡張**: `build_ask_prompt` に persona/L番号・`ANSWER_SCHEMA`/`parse_answer` に insights | R1 | ✓ | tdd-strict-coder | `uv run pytest tests/test_prompts.py`（persona=None で従来一致を含む） |
| R6 | **librarian.py（新規）**: `Librarian.route`（backend 注入・fallback 配線） | R1,R3 | | tdd-strict-coder | `uv run pytest tests/test_librarian.py`（FakeAnswerBackend） |
| R7 | **indexer.py 拡張**: study_notes→kind='digest' チャンク化・summary の kind='summary'・prune 波及 | R2 | | tdd-strict-coder | `uv run pytest tests/test_indexer.py` |
| R8 | **service.py 拡張**: `consult`/`digest`/`set_persona`・`ask` の insights/persona・`_answer_with_expert` 抽出・Librarian 注入 + **test_boundaries.py 拡張** | R2,R5,R6,R7 | | tdd-strict-coder | `uv run pytest tests/test_service.py tests/test_boundaries.py` |
| R9 | **server.py 拡張**: `consult` ツール追加（3 ツール） | R8 | | implementation-coder | `uv run pytest tests/test_server.py` |
| R10 | **cli.py 拡張**: `consult`/`digest`/`persona` サブコマンド + router 配線 | R8,R9 | | implementation-coder | `uv run pytest tests/test_cli.py` |
| R11 | **setup/mac・setup/windows 資産**: スクリプト雛形 + README（Ollama/pull/常駐化/電源/remote MCP/Tailscale bind） | – | ✓ | implementation-coder | shellcheck / PSScriptAnalyzer 静的検査・doctor スモーク・docs レビュー |
| R12 | **README/基盤ドキュメント追補**: consult/digest/persona の使い方・2 層説明・Tailscale 送信注意 | R10 | | implementation-coder | docs レビュー |

**並行可能セット（メインが同一メッセージで並列ディスパッチ可）:** {R0, R1, R2, R11}。R1 完了後に {R3, R4, R5}。以降 R6→R7→R8→R9→R10→R12 は順序依存。

### 各タスク完了時の検証ステップ（メインが `test-runner` 等で回す）
- **R1**: `RetrievedChunk` の `kind` デフォルトで既存 `_load_chunks` 呼び出しが壊れないこと（後方互換）。
- **R2**: 旧スキーマ DB（persona/kind/study_notes なし）を開いて `_migrate` が例外なく列/表を追補し、既存行が読めること（マイグレーション冪等性）。
- **R3**: `apply_fallback` の全分岐（空カタログ・answerable=false・幻覚 notebook 除去・top-N クランプ・重複・subquery 空補完）が純粋テストで網羅。
- **R5**: persona=None で `build_ask_prompt` の出力が既存 golden と完全一致（`ask` 互換の証明）。
- **R6/R8**: import ガード（`test_boundaries` が routing/librarian/digests の外部依存 import を検出）。
- **R9**: 公開ツールがちょうど 3 つ（ask/list_notebooks/consult）。
- **最終**: `uv run pytest -q && uv run ruff check .` 全緑。

---

## 11. トレードオフ記録（WHY）

- **司書を `routing.py`（純粋）+ `Librarian`（配線）に 2 分割**（モジュール数増 < テスト可能性・SRP）: ルーティングの「判断」（幻覚除去・フォールバック・クランプ）は最もバグりやすい所なので純粋関数に隔離し、LLM 呼び出しの配線と分離。カタログをデータで渡すことで Librarian を store から切り離し `FakeAnswerBackend` だけでテスト可能にした。`Librarian` を service のメソッドに畳む案は却下（層1 の判断ロジックが層2 fan-out と混ざり、独立テストの seam を失う）。
- **学びノートを `chunks.kind='digest'` に相乗り**（若干の非正規化 < embedding/検索/prune/citation 機構の全面再利用）: `study_notes` を source-of-truth に、検索用コピーを chunks に持つ二層構造。description→seq=-1 の既存パターンと同型で、新しい検索・掃除経路を増やさない。
- **抜粋/学びの分離を検索レベル（kind）で駆動**（合成時の自由記述 < 接地性）: insights を「モデルが実際に retrieved した学びノート」に接地し、幻覚した学びを返さない。S/L の 2 系統番号で透明に区別。
- **学びノート生成を専用 `shelf digest`**（手数増 < 計算予算の明示制御）: 単一 GPU 直列の数十秒推論を interactive `add` から外し、add/index/digest の 3 コスト級を分離。`--digest` チェインはドアだけ開ける。
- **`answerable=false` で専門家推論を短絡**（false-negative リスク < 30 秒級推論の節約）: 既定はレイテンシ保護。`SHELF_ROUTE_FALLBACK=all` で全横断へ切替可能（hard cap 内）。
- **`consult` を 3 番目のツールとして追加**（基盤「2 ツール」からの逸脱 < ルーティングという新 capability）: §5-D の通り、クライアント合成は哲学を壊す。`search`（生チャンク）とは性質が異なるため公開してよい。
- **persona も mask 適用して保存**（人手 config への mask は過剰にも見える < 「backend 送信テキストは全て mask」不変条件の一貫性）: codex/gemini/agy 時のクラウド送信を含めて統制を単純化。
- **`top_n=1` 既定**（recall 幅 < レイテンシ予算・単一 GPU 直列）: 配列スキーマで N>1 拡張のドアは開ける。
- **セットアップ資産を `setup/{mac,windows}` に分離しつつ `shelf/` は共有**（ディレクトリ重複懸念 < ロール別 provisioning の明確化）: `bootstrap/` は汎用環境、`setup/*` は 2 層サービス固有と責務分界。

---

## 12. 未決事項

1. **司書のレイテンシ対効果**: 司書 1 回 + 専門家 N 回が同一モデルの直列推論。routing の数秒が「ユーザーに notebook を選ばせる」より本当に得か、実運用で計測。損なら `consult` を薄くして `ask` 主体へ寄せる余地。
2. **学びノートの粒度**: 1 資料あたりのノート数・粒度（資料単位 vs 節単位）。まず資料単位・小 N（`SHELF_DIGEST_MAX_NOTES`）で開始し反復。
3. **`content_hash` の充填**: 現状 `upsert_document` は content_hash を NULL のまま。digest 陳腐化判定のため ingest 時に「正規化 md のハッシュ」で埋める必要。実装オーナー（service ingest か indexer か）を R2/R8 着手時に確定。
4. **retrieval のクエリ**: 専門家 ask で司書の `subquery` を検索に使い、原 `question` を合成指示に使う既定でよいか。両方を投げる/融合する案も含め実測で確定。
5. **`FakeAnswerBackend` の多段呼び出し**: 順序消費（既存 list モード）で当面十分。schema キー方式は必要になってから（YAGNI）。
6. **`serve --http` の bind とルート**: Tailscale/loopback bind・`/mcp` パス・`SHELF_HTTP_HOST` 既定の非公開化は在庫（別エージェント）実装の I/F と要突き合わせ。Mac 側 MCP 登録（`manifest.json` の URL エントリ化）もその確定後。
7. **ペルソナの多言語/長さ**: system prompt としての persona の長さ上限・言語。過長 persona がコンテキストを圧迫しないよう上限を設けるか要検討。
8. **司書と専門家でモデル切替をしない前提の再確認**: 所与（単一常駐・切替 30 秒回避）に従い両層とも qwen3:8b。将来 8GB に収まる別モデルを司書専用にする案はハード制約再評価後（§10 の R0 config でエンジン名は分離済み = ドアは開けてある）。

---

## 13. 自動分類投入（`shelf shelve`）

- 位置づけ: 基盤設計書（§4-C「投入は人間 CLI に限定」・§5 命名検証・`add_directory` のスキャン規則）と本増分設計書（§6 の「純粋判断 `routing.py` + 薄い配線 `Librarian`」パターン・§4-C コスト 3 級分離）の**上に載る第 2 の増分**。既存節・既存挙動は一切変更せず、確立済みの seam を再利用して「ディレクトリ一括投入時の自動解釈・分類」を追加する。
- 本節は設計のみ。実装コード・テストコードは含まない。

### 13.0 目的とスコープ

ディレクトリごと投入された雑多な資料を、モデルが 1 ファイルずつ解釈して既存 notebook へ割り当てる／適切な新 notebook を提案・作成することで、人手の仕分けなしにコーパスを構築できるようにする。

| する | しない（本増分の非スコープ） |
|---|---|
| CLI 専用 `shelf shelve <dir>`（人間操作。投入系を MCP に出さない基盤 §4-C を維持） | MCP ツール追加（`consult`/`ask`/`list_notebooks` の 3 つのまま） |
| **増分分類**: (現カタログ + 当該ファイル要約) → 1 ファイルずつ structured 判断・新規はその場でカタログ反映 | 全ファイル要約を 1 プロンプトで一括分類（qwen3:8b 実効ctx 小のため不採用） |
| `--dry-run` で分類計画のみ JSON 出力（永続副作用ゼロ） | 分類の対話的確認 UI（v1 は計画提示 → 適用の 2 モードのみ） |
| 要約は既存 description 自動生成パス（`build_summary_prompt`）を再利用し分類にも使う（変換 1 回） | 分類専用の別要約経路（二重変換・二重要約を作らない） |
| 新 notebook は決定的な名前正規化・衝突フォールバックで作成（persona 自動設定なし・digest 自動実行なし） | 新 notebook への persona/学びノート自動付与（digest 同様、後から人間が付与） |

### 13.1 決定事項と根拠（結論先行）

**結論: `shelve` は「新純粋モジュール `shelving.py`（外部依存ゼロ）+ 薄い配線クラス `Shelver`（`routing.py`/`Librarian` と同型）+ service の 2 フェーズ配線（計画→適用）」で実現でき、新しい外部依存・新しい import ガード対象を 1 つも増やさない。** 最もバグりやすい 3 つの論理——(a) 増分カタログのスレッディング、(b) 幻覚 notebook 名の除去、(c) 名前の正規化・衝突連番——をすべて純粋関数に隔離し、`FakeAnswerBackend` と手組み DTO fixture だけで全分岐を単体テストできる。既存の `add_directory` スキャン規則・`_ingest_file` 永続化・`index_notebook`・description 自動要約・mask 順序をそのまま再利用する。

決定事項の要旨:

1. **分類は司書と同型の 2 分割。** 判断ロジック（プロンプト構成・structured パース・幻覚除去・名前フォールバック・増分カタログ更新）を純粋関数 `shelving.py` に、LLM 呼び出しの配線を `Shelver` クラス（`shelver.py`）に置く。`Shelver` は `AnswerBackend` ポートと `shelving.py`（純粋）だけに依存し、store・sqlite・subprocess を一切知らない → `FakeAnswerBackend` だけで `plan()` の全経路を単体テストできる（§6 `Librarian` と厳密に同型）。

2. **2 フェーズ（計画 → 適用）で `--dry-run` を自然に表現する。** フェーズ1（走査 → 変換 → 要約 → 分類）で `ShelvePlan` を組み立て、フェーズ2（適用・`--dry-run` 時は省略）で新 notebook 作成 + 投入 + 索引を行う。**`--dry-run` = フェーズ1 の打ち切り**。計画を第一級のデータ構造にすることで、dry-run と適用が同じ分類結果を共有し、計画そのものを純粋テストできる。

3. **「変換 1 回」を守るため `_ingest_file` を permute せず extract する。** 既存 `_ingest_file` は内部で変換 + 要約 + 永続化を全部やるが、`shelve` はフェーズ1 で既に変換・要約済みなので、適用で再変換・再要約させると二重コストになる（PDF/OCR は特に重い）。`_ingest_file` から**永続化半分**（doc_id 採番 → corpus 書き出し → `upsert_document`）を純粋な副作用として `_persist_converted` に extract-method し、`add_source`/`add_directory` と `shelve` の両方が共有する。これは振る舞い保存リファクタで、既存テストが回帰ガードになる。

4. **冪等性は「いずれかの notebook に投入済みの origin はスキップ」で担保する。** `doc_id` が notebook を含む（基盤 §1）ため、再実行で LLM が別 notebook へ再分類すると同一 origin が複数 notebook に重複投入され得る。これを断つため、変換前に `store.find_documents_by_origin(origin)`（新規 read メソッド）で全 notebook 横断の既投入を検査し、既投入なら変換・要約・分類のコストを払わずスキップする。

5. **コスト 3 級分離を維持。** `shelve` は add 相当（変換 + body 索引）+ 分類推論までで止め、**digest は自動実行しない**。出力 notes で `shelf digest` を案内する。要約は既存 best-effort パスの再利用のみ（新規の高コスト経路を増やさない）。

6. **推論バックエンドは 1 つの config に集約。** 要約・分類の推論、および作成される新 notebook の `backend` 列をすべて `config.SHELVE_BACKEND`（env `SHELF_SHELVE_BACKEND`・既定 `"ollama"`）に倒す。既定 `codex`（クラウド）ではなくローカル `ollama`（qwen3:8b）を既定にするのが所与（§2 の実効 ctx 小・課金回避）。persona は自動設定しない。

### 13.2 全体フロー（2 フェーズ）とレイテンシ

```
                      shelf shelve <dir> [--dry-run]   (CLI 専用・MCP 非公開)
                                │
                         ShelfService.shelve
   ┌────────────────── フェーズ1: 計画 ───────────────────┐   ┌── フェーズ2: 適用 (dry-run 時は省略) ──┐
   │ 1. scan   (add_directory と同一規則)                  │   │ 6. 新 notebook 作成                     │
   │ 2. 冪等スキップ  find_documents_by_origin              │   │    (backend=SHELVE_BACKEND, persona=None)│
   │ 3. convert (1回) → mask → markdown 保持               │   │ 7. _persist_converted (再変換しない)     │
   │ 4. summarize (既存 build_summary_prompt・1回)         │   │    description=要約, source='auto'       │
   │ 5. classify  Shelver.plan(要約群, カタログ)           │   │ 8. index_notebook (影響 notebook 毎に1回)│
   │    → ShelvePlan(assignments, created, skipped, errors)│   │ 9. notes: digest 推奨を案内             │
   └──────────────────────────────────────────────────────┘   └──────────────────────────────────────┘
                                │
              AnswerBackend ポート (= engines/ollama.py, qwen3:8b 単一常駐・直列)
```

- **`--dry-run` の「副作用ゼロ」の厳密な定義**: 永続的変異（notebook 作成・corpus ファイル・DB 行・索引）を一切行わないこと。計画を出すには分類が必要で、分類には要約が、要約には変換が必要なので、**フェーズ1 の推論コスト（ファイルあたり 要約1 + 分類1）は dry-run でも発生する**が、これらは非変異である。この定義を CLI ヘルプ・README にも明記する（「plan preview は無料ではない」誤解の予防）。
- **レイテンシ予算**: ファイルあたり = 変換（テキスト即時／PDF+OCR は数〜数十秒）+ 要約（qwen3:8b・数〜数十秒）+ 分類（小プロンプト小出力・数秒）。単一 GPU 直列のため N ファイルで概ね `N×(要約+分類)`。50 ファイルなら十数〜数十分規模の**バッチ操作**（人間が起動して離席する `digest` と同格）。

### 13.3 モジュール構成と依存方向

#### 追加・変更するファイル（差分のみ）

```
shelf/shelf/
  shelving.py     [新規・純粋・ドメイン層] 分類の判断: build_classification_prompt / CLASSIFY_SCHEMA
                                          / parse_classification / classify_step（増分カタログ更新・
                                          幻覚除去・名前フォールバック適用）/ StepResult
  shelver.py      [新規・オーケストレーション・ドメイン層] Shelver クラス。AnswerBackend ポート
                                          + shelving.py だけに依存。plan(summaries, catalog) -> ShelvePlan
  names.py        [拡張・純粋] normalize_notebook_name（不正名→決定的正規化）/
                                          assign_unique_name（衝突→連番）を public 追加
  ports.py        [拡張] 中立 DTO 追加: FileSummary / ClassificationDecision / ShelfAssignment /
                                          NewNotebookSpec / ShelvePlan
  store.py        [拡張・境界] find_documents_by_origin(origin) 追加（冪等スキップ検査用の read）
  service.py      [拡張] shelve() 追加 + _ingest_file から _persist_converted を extract-method。
                                          Shelver を注入（既定は backend_factory から構築）
  config.py       [拡張] SHELVE_BACKEND（env SHELF_SHELVE_BACKEND・既定 "ollama"）
  cli.py          [拡張] shelve サブコマンド（<dir> 位置引数 + --dry-run）+ 進捗コールバック配線
shelf/tests/
  test_shelving.py / test_shelver.py  [新規]
  test_names.py / test_ports.py / test_store.py / test_service.py / test_config.py /
  test_cli.py / test_boundaries.py / fakes.py  [拡張]
```

> **命名の注意**: `shelving.py`（純粋判断）と `shelver.py`（配線クラス `Shelver`）は 1 文字差で紛らわしい。役割で覚える——**shelving = 何を決めるか（pure）、shelver = どう呼ぶか（wiring）**。`routing.py`/`librarian.py` が別語なのと違い同語派生だが、責務分界は同一。

- **`server.py` は変更しない**（決定 1・MCP ツールを増やさない）。これはトレードオフ記録（§13.11）にも明記する。

#### 依存方向（内向き = ドメインへ収束・DIP。§3 の図に追記）

```
   cli.py ──► service.py ──► Shelver ──► shelving.py (純粋) ──► names.py (純粋)
                  │             │
                  │             └────────► AnswerBackend ポート ◄── engines/ollama.py
                  ├──► shelving.py (計画のシリアライズ補助・純粋)
                  ├──► convert.py (純粋・変換振り分け) / prompts.build_summary_prompt (純粋)
                  ├──► index_notebook ──► chunker / store / embedder
                  └──► store.py (sqlite3 境界: find_documents_by_origin / create_notebook / upsert)
```

- **`Shelver` はドメイン層**。依存は `AnswerBackend`（ポート）・`shelving.py`（純粋）・`ports.py` の DTO のみ。カタログは `list[NotebookCard]`（§6-A の既存 DTO を再利用）として service から渡され、`Shelver` は store を知らない（§6 `Librarian` と同一の循環遮断境界）。
- **要約は service の責務、分類は Shelver の責務**。`Shelver.plan` は「どこへ入れるか」だけを決め、走査・変換・要約・適用（副作用）は service が行う（層の責務分離）。

#### import ガードへの影響（§9-C / `test_boundaries.py`）

- **新規外部依存はゼロ。** `shelving.py` は `json`（stdlib）+ `ports.py` の DTO + `names.py`（純粋）だけ、`shelver.py` は `ports.py` + `shelving.py` + `pathlib` だけを使う。`_RESTRICTED_TO_OWNER` に**新エントリを追加しない**。
- `_DOMAIN_LAYER_FILES` に **`shelving.py` / `shelver.py` を追加**し、両ファイルが sqlite3/subprocess/fastembed/… を import したら即失敗させる。`test_domain_layer_files_are_present` の期待集合も 2 ファイル分更新する。

### 13.4 分類プロンプトと structured schema

#### `CLASSIFY_SCHEMA`（1 ファイル 1 判断・§6-B `ROUTING_SCHEMA` と同粒度）
```json
{
  "type": "object",
  "properties": {
    "action":      {"type": "string", "enum": ["assign", "new"]},
    "notebook":    {"type": "string"},
    "description": {"type": "string"},
    "reason":      {"type": "string"}
  },
  "required": ["action", "notebook", "reason"],
  "additionalProperties": false
}
```
- `action="assign"`: `notebook` = 既存 notebook 名。
- `action="new"`: `notebook` = 新 notebook 名の提案、`description` = その notebook の説明。
- `description` は required に含めない（`new` 以外では不要・`StudyNote.span` と同じ寛容さ）。

#### `build_classification_prompt(summary, catalog)`（純粋）の判断基準
- 「あなたは資料室の司書です。以下の notebook 一覧と新資料の要約を読み、この資料をどの notebook に入れるべきか判断してください。」
- 「既存 notebook のいずれかが主題に合致するなら `action=assign`・`notebook` にその名前を入れてください。」
- 「どれにも合致しないなら `action=new`・`notebook` に簡潔な新名（英小文字・数字・`-`/`_` のみ）・`description` に説明を入れてください。」
- 「一覧に無い notebook 名へ `assign` しないでください。」（幻覚のプロンプト側抑止。純粋 resolver 側でも二重防御。）
- 厳格 JSON ヒント（`routing.py` の `_JSON_FORMAT_HINT` と同形）。
- **カタログ表現**: `NotebookCard` を `routing._format_card` と同型に整形（name・doc数・概要=description）。**新規作成した this-run notebook も提案 description 付きで即カタログに載る**ので、後続ファイルはそれを見て `assign` を選べる（増分の核心）。

#### 幻覚 notebook 名の扱い（純粋 `classify_step` で二重防御）
- `assign` 先が working カタログに**実在**する → assign。
- `assign` 先が**実在しない**（幻覚）→ その名前を「新規作成の提案名」と再解釈し、`new` と同じ正規化・衝突経路へ流す。これで「存在しない notebook への assign」は決して発生せず、かつファイルを失わない（`SHELF_SHELVE_ON_HALLUCINATION=skip` で保守的にスキップへ切替可能——ドアは開けるが v1 の既定は create）。
- `new` 名 → `names.normalize_notebook_name` で正規化 → `names.assign_unique_name` で衝突連番（§13.5）。

### 13.5 名前フォールバックと衝突解決（`names.py` 純粋関数）

`shelving.classify_step` は名前決定を `names.py` の 2 つの純粋関数へ委譲する（notebook 名の同一性規則は既に `names.py` が所有——SRP）。

- `normalize_notebook_name(raw) -> str`: 小文字化 → `[a-z0-9_-]` 以外を `-` へ圧縮 → 前後 `-` 除去 → 64 字切詰 → 空なら既定 stem `"notebook"`。**構成上必ず `validate_notebook_name` を通る**値を返す（`_slugify` と同思想の一般化・`_slugify` は private のため再利用せず notebook 名専用の public 関数を新設）。
- `assign_unique_name(base, taken) -> str`: `base ∉ taken` ならそのまま。衝突時は `base-2`, `base-3`, … と 64 字上限内で連番付与し、最初に空いた名を返す。`taken` = working カタログ名（pre-run ∪ this-run 作成分）。

**衝突ポリシー（推奨・決定的）:**
- `assign` で実在名 → 既存へ投入（新 notebook なし）。
- `new`（または幻覚 assign の再解釈）で正規化名が working カタログと衝突 → **連番で別 notebook を作る**（モデルがカタログに当該名を見た上で `new` を選んだ = 明示的に別バケツを要求したと解釈。既存ユーザ notebook へ黙って混ぜない保守側）。
- **this-run 内の重複は増分カタログが吸収する**: ファイル1 が `physics` を作れば、ファイル2 のプロンプトには `physics` が載るので、行儀の良いモデルは `assign physics` を選び 1 つに集約される。ファイル2 が敢えて `new physics` を返す病的ケースのみ `physics-2` になる（決定的・保守的なセーフティネット）。

### 13.6 計画データ構造・dry-run 出力・適用時返却スキーマ

#### 純粋 DTO（`ports.py`）
```
FileSummary(origin: str, summary: str)                                   # Shelver への入力（分類用テキスト）
ClassificationDecision(action, notebook, description, reason, parse_ok)  # parse_classification の生結果
ShelfAssignment(origin, notebook, new_notebook: bool, summary, reason)   # 解決済み 1 エントリ
NewNotebookSpec(name, description, backend)                              # 作成すべき notebook
ShelvePlan(assignments: list[ShelfAssignment], created: list[NewNotebookSpec],
           skipped: list[dict], errors: list[dict])                     # Shelver.plan の集約結果（分類段）
```
（markdown を保持する中間表現 `ConvertedFile(origin, markdown, title, converter, notes, summary)` は corpus 書き出しにしか使わず**ドメイン境界を跨がない**ので `ports.py` ではなく service.py ローカルに置く。）

#### `--dry-run` 出力スキーマ（永続副作用ゼロ）
```json
{
  "directory": "/abs/dir", "dry_run": true,
  "plan": [
    {"origin": "/abs/a.pdf", "notebook": "quantum-mechanics", "new_notebook": false, "summary": "...", "reason": "..."},
    {"origin": "/abs/b.md",  "notebook": "cooking-recipes",   "new_notebook": true,  "summary": "...", "reason": "..."}
  ],
  "created_notebooks": [{"notebook": "cooking-recipes", "description": "料理レシピ集", "backend": "ollama"}],
  "skipped": [
    {"origin": "/abs/c.png", "reason": "未対応の形式です"},
    {"origin": "/abs/d.pdf", "reason": "既に notebook 'physics' に投入済みです"}
  ],
  "errors": [{"origin": "/abs/e.pdf", "error": "テキストを抽出できませんでした"}]
}
```

#### 適用時返却スキーマ（`add_directory` と語彙を揃える）
```json
{
  "directory": "/abs/dir", "dry_run": false,
  "added": [{"doc_id": "a-1a2b3c4d", "origin": "/abs/a.pdf", "notebook": "quantum-mechanics"}],
  "created_notebooks": ["cooking-recipes"],
  "skipped": [...], "errors": [...],
  "chunks_written": 123,
  "notes": ["学びノートは自動生成されません。`shelf digest <notebook>` の実行を検討してください。"]
}
```

**skipped と errors の区別（`add_directory` の流儀に整合）:**
- `skipped`（ポリシー除外）: 隠しファイル・symlink・未対応形式・**既投入 origin**。
- `errors`（運用失敗・継続）: 変換失敗・読取不可（OSError）・**分類のバックエンド/パース失敗**（要約失敗は劣化——後述——でありエラーにしない）。

### 13.7 冪等性・再実行安全性

- **既投入 origin スキップ**: フェーズ1 の変換前に `store.find_documents_by_origin(origin)`（新規 `SELECT id, notebook FROM documents WHERE origin = ?`）を引き、非空なら「既に notebook '<name>' に投入済み」で skipped。これにより (a) 再分類ドリフト（同一 origin が実行毎に別 notebook へ）と (b) notebook 跨ぎの重複投入を構造的に防ぐ。変換・要約・分類のコストも払わない。
- **origin 正規化の一致**: `add_directory` と同一に `root = Path(dir_path).resolve()` → 各 `origin = str(path)`（resolve 済み絶対パス）。スキップ検査も同じ resolve 済み絶対パス文字列で引くので、格納値と照合値が一致する（新たな正規化規則を導入しない）。
- **再実行の帰結**: 適用済みディレクトリの再 `shelve` → 全件 skipped → 計画空 → no-op（冪等）。部分失敗した実行の再 `shelve` → 成功分は skip・失敗分のみ再試行（resumable）。意図的な再分類は `shelf rm` 後の再実行（将来 `--force` のドアは開けるが v1 非対象）。
- **補足**: `documents.origin` に索引が無いため全表走査だが個人スケールでは無害。規模が問題化したら `CREATE INDEX idx_documents_origin ON documents(origin)` を追加（門は開けておく）。

### 13.8 コスト・レイテンシ見積りと進捗表示

- **推論回数**: ファイルあたり 要約 1 + 分類 1（いずれも `SHELVE_BACKEND`=ollama・qwen3:8b・直列）。要約と分類を 1 プロンプトに融合すれば半減できるが、**要約は既存 description 生成物として再利用する所与（決定 5）**のため分離を維持する（責務分離・成果物再利用 > 推論回数最小化）。
- **要約失敗時の劣化**: `build_summary_prompt` は best-effort。失敗時は分類用テキストを決定的フォールバック（title + markdown 先頭 ~500 字）に落とし、**stored description は None**（既存 best-effort と同じ・source も None）。分類はフォールバックテキストで継続し、ファイルを失わない。
- **進捗表示**: 数十分規模のバッチのため live 進捗が UX 上有効。ただし service に `print` を置くと境界が漏れテストしづらい → **`progress: Callable[[dict], None] | None = None` を注入**（既定 None = 無音）。service はファイル毎に `progress({"index":i, "total":n, "origin":..., "phase":"classify"})` を呼び、CLI が stderr へ出す callback を渡し、テストは記録用 fake か None を渡す（進捗を port 化してテスト可能に保つ）。v1 は callback 無し（末尾に結果 JSON を出すだけ）でも安全（冪等・resumable なので「固まったか」の不安は再実行で解消）——callback は推奨 seam として設計に織り込むが優先度は低。

### 13.9 境界とテスト戦略（ネットワーク・実 DB・プロセス・時計 不使用）

| 対象 | ダブル/手段 | テスト範囲 |
|---|---|---|
| `shelving`（純粋） | 手組み `FileSummary`/`NotebookCard`/`ClassificationDecision` fixture | prompt 整形（カタログ block・幻覚抑止文・JSON ヒント）・`parse_classification`（正常 assign/new・欠落・不正 JSON・enum 外・非 dict→parse_ok=False）・`classify_step` 全分岐（assign 実在／assign 幻覚→create／new 正常／new 不正名→正規化／new 衝突→連番／this-run 重複／description の created spec への伝播／working カタログ成長） |
| `names`（純粋・拡張） | なし | `normalize_notebook_name`（不正字→`-`・空→`notebook`・64 字切詰）・`assign_unique_name`（無衝突／1 回衝突→`-2`／多重→`-3`／上限内切詰） |
| `shelver` | `FakeAnswerBackend([classify_json...])` | plan の配線・**増分スレッディング（file2 の prompt に file1 の新 notebook が載ることを `backend.calls` の prompt キャプチャで証明）**・backend 失敗/parse 失敗→当該ファイルのみ skipped/errors・集約（assignments + created） |
| `service.shelve` dry-run | `Store(":memory:")` + `FakeEmbedder` + `FakeAnswerBackend` + `FakeConverter` | **永続副作用ゼロ**（`list_notebooks` 不変・corpus 空・documents 0 件）・plan JSON 形状 |
| `service.shelve` apply | 同上 | 新 notebook 作成（backend=ollama・persona=None）・documents に description=要約/source='auto'・影響 notebook 毎 index 1 回・chunks_written>0・added/created/skipped/errors |
| `service.shelve` 冪等 | 同上・2 回適用 | 2 回目は全 skipped（"既に投入済み"）・documents 増えず・notebook 重複せず |
| `service.shelve` 既投入他notebook | `Store(:memory:)` に origin X を notebook A で事前 seed | X を含む dir を shelve → X は skipped（再分類されない） |
| `service.shelve` スキャン規則 | tmp ディレクトリ fixture | 隠し/symlink/未対応 → skipped（`add_directory` テストの fixture 流用） |
| `service.shelve` 変換 1 回 | `FakeConverter` の呼び出し記録 | 投入ファイル毎に `convert_file` はちょうど 1 回・description == 要約・digest 未実行（study_notes 0 件）・notes に digest 案内 |
| `_persist_converted`（extract） | 既存 `test_service.py` | `add_source`/`add_directory` の既存テストが緑のまま（振る舞い保存の回帰ガード） |
| `store.find_documents_by_origin` | `Store(":memory:")` | 全 notebook 横断ヒット・未投入で空・複数 notebook に同 origin |
| `config` | monkeypatch.setenv + reload | `SHELVE_BACKEND` の env 上書き・既定 "ollama" |
| `cli.build_parser`（純粋） | なし | `shelve <dir> --dry-run` の引数解釈 |
| `test_boundaries` | AST 静的走査 | `shelving.py`/`shelver.py` の外部依存 import ゼロ・`_DOMAIN_LAYER_FILES` へ追加 |

`FakeAnswerBackend` は既存のまま（canned list を呼び出し順に消費）。`shelve` の推論順は「ファイル毎に 要約 → 分類」だが、要約は service、分類は `Shelver` が別 backend インスタンスを使う設計にすれば順序衝突を避けられる（テストは要約用・分類用に別 `FakeAnswerBackend` を渡す）。**Fake の拡張は不要**（§9-B と同じ YAGNI 判断）。

### 13.10 タスク分割（TDD・実装順・done-criteria）

各タスクは独立にビルド・テスト・revert 可能な最小単位。done-criteria はテストコマンド。**共通制約:** sqlite3 は `store.py` のみ / `shelving.py`・`shelver.py` は外部依存を一切 import しない / ネットワーク・実 DB・プロセス・時計に触れる単体テストを書かない / `server.py` を変更しない（MCP ツールを増やさない）/ コミットは日本語・原子的。

| # | タスク | 依存 | 並行 | 推奨エージェント | done-criteria |
|---|---|---|---|---|---|
| V0 | **config.py 拡張**: `SHELVE_BACKEND`（env `SHELF_SHELVE_BACKEND`・既定 "ollama"） | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_config.py` |
| V1 | **ports.py 拡張**: `FileSummary`/`ClassificationDecision`/`ShelfAssignment`/`NewNotebookSpec`/`ShelvePlan` DTO | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_ports.py` |
| V2 | **names.py 拡張（純粋）**: `normalize_notebook_name` / `assign_unique_name` | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_names.py` |
| V3 | **store.py 拡張（境界）**: `find_documents_by_origin(origin)` | – | ✓ | tdd-strict-coder | `uv run pytest tests/test_store.py` |
| V4 | **service リファクタ**: `_ingest_file` から `_persist_converted` を extract-method（振る舞い保存） | – | ✓ | tdd-strict-coder | 既存 `uv run pytest tests/test_service.py` が緑 |
| V5 | **shelving.py（純粋・新規）**: `build_classification_prompt`/`CLASSIFY_SCHEMA`/`parse_classification`/`classify_step` | V1,V2 | | tdd-strict-coder | `uv run pytest tests/test_shelving.py` |
| V6 | **shelver.py（新規・配線）**: `Shelver.plan`（backend 注入・増分カタログスレッディング・失敗継続） | V1,V5 | | tdd-strict-coder | `uv run pytest tests/test_shelver.py`（FakeAnswerBackend・prompt キャプチャ） |
| V7 | **service.shelve 拡張** + **test_boundaries.py 拡張**: scan→convert→summarize→classify→(dry-run/apply)→index・冪等スキップ・progress callback・Shelver 注入 | V1,V2,V3,V4,V5,V6 | | tdd-strict-coder | `uv run pytest tests/test_service.py tests/test_boundaries.py` |
| V8 | **cli.py 拡張**: `shelve <dir> --dry-run` サブコマンド + stderr 進捗 callback 配線 | V7 | | implementation-coder | `uv run pytest tests/test_cli.py` |
| V9 | **README 追補**: `shelve` の使い方・`--dry-run` の非無料性・冪等性・digest 推奨 | V8 | | implementation-coder | docs レビュー |

**並行可能セット（メインが同一メッセージで並列ディスパッチ可）:** {V0, V1, V2, V3, V4}。V1・V2 完了後に V5。以降 V5→V6→V7→V8→V9 は順序依存。

#### 各タスク完了時の検証ステップ（メインが `test-runner` 等で回す）
- **V2**: `normalize_notebook_name` の出力が必ず `validate_notebook_name` を通ること（純粋テストで往復検証）。
- **V4**: `add_source`/`add_directory` の既存テストが**一切変わらず**緑（extract-method が振る舞いを変えていない証明）。境界: `_persist_converted` は変換・要約をしない（責務が永続化のみに絞られている）。
- **V5**: `classify_step` の全分岐が手組み `ClassificationDecision` fixture のみで網羅（backend 不使用でテストできる = 境界が正しい証拠）。
- **V6**: import ガード（`test_boundaries` が `shelving.py`/`shelver.py` の外部依存 import を検出）。増分スレッディングが prompt キャプチャで証明されていること。
- **V7**: dry-run が永続副作用ゼロ（notebook/document/corpus/index いずれも不変）・冪等再実行が全 skip・変換が投入ファイル毎 1 回。
- **V8**: 公開 MCP ツールが依然ちょうど 3 つ（`server.py` 無変更の確認）。
- **最終**: `uv run pytest -q && uv run ruff check .` 全緑。

### 13.11 トレードオフ記録（WHY）

- **分類を `shelving.py`（純粋）+ `Shelver`（配線）に 2 分割**（モジュール数増 < テスト可能性・SRP）: 増分カタログスレッディング・幻覚除去・名前衝突が最もバグりやすい。純粋関数に隔離し `FakeAnswerBackend` + 手組み fixture で全分岐を固定。§6 の `routing.py`/`Librarian` と同型なので学習コストゼロ。
- **2 フェーズ（計画→適用）で `--dry-run` を表現**（全 markdown をメモリ保持 < 計画の第一級データ化・dry-run の自然な打ち切り・変換 1 回の担保）: 個人スケール（数十ファイル・ローカル推論が支配的）ではメモリは無視できる。超大規模ならストリーミング変種のドアは開ける。
- **`_persist_converted` extract-method で既存 `_ingest_file` を再利用**（既存コードへの介入リスク < DRY・doc_id/corpus/upsert の単一真実源・変換 1 回）: 重複コピーは doc_id 生成則の分岐という将来事故源。振る舞い保存リファクタとして既存テストを回帰ガードにする。
- **既投入 origin を全 notebook 横断でスキップ**（再分類の柔軟性 < 冪等性・重複防止）: LLM 非決定性による notebook 跨ぎ重複を構造的に断つ。意図的再分類は `rm` 後の再実行（`--force` は将来）。
- **幻覚 assign を「新規作成の再解釈」に倒す**（notebook 増殖リスク < ファイルを失わない・存在しない notebook への assign を構造的にゼロ化）: 保守的スキップは `SHELF_SHELVE_ON_HALLUCINATION=skip` でドアを開ける。
- **要約と分類を分離した 2 推論/ファイル**（推論回数 < 要約成果物の再利用・責務分離）: 決定 5（既存 description パス再利用）に従う。融合は可能だがドアは閉じる。
- **digest を自動実行しない・persona 自動設定なし**（利便 < コスト 3 級分離・人間の計算予算制御）: `shelve` は add 相当 + 分類まで。notes で `shelf digest` を案内（§4-C の踏襲）。
- **`SHELVE_BACKEND` 既定 ollama（≠ 全体既定 codex）**（設定の一貫性 < ローカル推論・課金回避・実効 ctx 小の所与）: 分類のような多数回・低単価推論をクラウドに出さない。作成 notebook の backend も同値に倒す。
- **`server.py` 無変更（MCP ツールを増やさない）**（自律投入の利便 < 基盤 §4-C「投入は人間 CLI に限定」）: 遅い副作用・永続リソース増を人間操作に閉じる原則を維持。`consult`/`ask`/`list_notebooks` の 3 ツールのまま。

### 13.12 未決事項

1. **`--dry-run` の推論コスト明示**: dry-run でも 要約+分類の推論が走る（非変異だが有料＝時間）ことを CLI ヘルプ/README でどこまで強調するか。誤解が多ければ「N ファイルで概算 M 分」の見積り表示を検討。
2. **分類粒度と要約の質**: qwen3:8b の要約が主題判定に十分かは実測依存。不足なら分類プロンプトに title/先頭見出しを追加する余地（§13.8 の劣化フォールバックと同経路）。
3. **`documents.origin` 索引**: 冪等スキップの全表走査が規模で問題化したら索引追加（§13.7 補足）。実装オーナーは V3 着手時に規模想定を確認。
4. **進捗 callback の要否**: v1 は末尾 JSON のみでも冪等・resumable ゆえ安全。実運用で「固まった不安」が実害なら V8 で stderr callback を有効化（seam は設計済み）。
5. **`SHELF_SHELVE_ON_HALLUCINATION` の実装可否**: 既定 create で回るなら skip モードは YAGNI。幻覚由来の notebook 増殖が実測で問題化したら追加。
6. **意図的再分類 `--force`**: 既投入 origin の再分類・移動需要が出たら `rm`+再実行より軽い経路として検討（現状は門を開けるのみ）。

### 13.13 実機検証の記録と既知の特性（2026-07-11・qwen3:8b）

料理レシピ2件+Git解説1件・既存 notebook 1件（無関係な主題）での実機 dry-run/適用/冪等再実行/ask の全経路を検証済み。その過程での対処と既知特性:

- **温度0固定（対処済み）**: 既定サンプリングでは実行ごとに分類が揺れ、「主題は合致しない」という理由文と `action=assign` が自己矛盾する出力を観測。`engines/ollama.py` の `build_payload` で `options.temperature=0` を常時付与し決定論化した（根拠付きQAでも資料忠実性に働くため全経路で固定）。
- **命名粒度は 8B モデルの既知特性（記録のみ）**: 「カテゴリ粒度で命名せよ」という明示指示を2段階で強化しても、最初の資料から `curry-recipe` のような個別資料粒度の name/description が生成されがち。同種の後続資料の assign 自体は正しく機能する（カテゴリ近接判定は働く）ため、実害は「棚の名前が狭い」に留まる。**推奨運用: 主要カテゴリの notebook を良い description 付きで事前作成してから shelve を流す**と、assign 経路が支配的になり品質が安定する。プロンプト反復の費用対効果が尽きたための記録であり、より大きいモデル（gemma3:12b 等）への切替時に再評価する。
