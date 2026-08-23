# backport-0.5.0: personal リポジトリへの還流ガイド

OSS agent-shelf v0.5.0（v0.4.3 → 0.5.0、73 コミット / 18 機能ブランチ）を
private リポジトリ `personalized-claude/shelf/`（Windows 実運用中）へ逆移植するためのガイド。

## 前提

- **採番**: OSS と personal は独立採番。personal の現行は 0.5.0 のため、本還流の適用結果は **personal 0.6.0** とすることを推奨（要ユーザー判断、後述）。
- **レイアウト**: personal は `personalized-claude/shelf/` 直下に `shelf/`（パッケージ）・`tests/`・`pyproject.toml` を持ち、OSS ルートと相対レイアウトが一致する。パッチ適用は `personalized-claude/shelf/` を cwd として `git apply`、または `git -C` 相当で行える。
- **patch の切り出し**: 各論理単位は squash マージのない `--no-ff` マージなので、`git diff <merge>^1..<merge>` が単位全体の差分、`git log <merge>^1..<merge>^2` が構成コミットになる。コミット単位で移植する場合は `git format-patch -N <hash>` を使う。
- **personal 側の乖離に注意**: personal 0.5.0 は OSS 0.4.3 と完全一致ではない（独自履歴を持つ）。各単位の適用前に対象ファイルを diff し、機械適用が失敗する場合はコミットメッセージの意図（WHY）に沿って手で移植する。
- **本ガイドは OSS 側の文書**。personal リポジトリへの変更は還流作業のセッションで別途行う（本セッションでは personal は読み取りのみ）。

## 適用順序と論理単位

セッションでのマージ順に準拠。依存関係（例: store-thread-safety → async-mcp-tools、http-env-config → doctor）があるため順序を守ること。
各単位の「検証」は personal 側で `uv run pytest -q <対象>` を想定（テストファイルも同じ単位で移植する）。

| # | ブランチ / マージ | コミット | 一行説明 |
|---|---|---|---|
| 1 | fix/persona-lazy-service (1372696) | eb84327 | persona 表示専用パスの実 embedder 構築を遅延しテストハングを解消 |
| 2 | docs/readme-accuracy (c192afe) | bb08206, 54c17cf | README 実装一致 + SECURITY.md 脅威モデル節 |
| 3 | fix/runner-windows-exec (13303e6) | 32cc98b, aa79fc8 | Windows .cmd 起動失敗と timeout 時の孫プロセス孤児化を解消 |
| 4 | feat/epub-support (4c1d1ac) | 769b2f1, 09f2d0b, 099178f | EPUB/FB2/XPS のページマーカーなしリフロー変換 |
| 5 | fix/store-wal (2435586) | d0180dd, 0a52a03, 736c345 | WAL 化 + FTS ラッチ自己修復リトライ |
| 6 | test/masking-rules (d1431da) | 000cb19 | マスク規則 5 正規表現の仕様固定テスト |
| 7 | docs/import-design-docs (655a675) | 25fe492 | 設計書 2 本の移入 — **personal が原本のためスキップ** |
| 8 | fix/small-remote-fixes (d9ef25a) | 880b2f0, caea11a, 29646d0, ff6ad7c | emit-mcp httpUrl・0.0.0.0 警告・Windows 予約デバイス名 |
| 9 | ci/windows-matrix (171b100) | ccc141d, 099aed4, 33662e4 | Windows CI matrix + テストの POSIX 依存排除 |
| 10 | feat/local-file-size-limit (1be8b40) | 7f94281, d9d47d2, 5cd4039, f34b6c9 | SHELF_MAX_FILE_MB（既定 300MB）のサイズ上限 |
| 11 | feat/store-thread-safety (094f9d0) | 6a66220, cbedafd | Store スレッド安全化 + ShelfService 遅延構築レース解消 |
| 12 | feat/http-env-config (3e33851) | 7a80f15, e2f5f49, d38755e | HTTP リスナー設定の env 対応 + --stdio 明示フラグ |
| 13 | feat/content-hash-dedup (04fc3b2) | b8c5cf4, e04ace9, bfc4fe3 | content_hash 記録 + notebook 横断の内容重複検出 |
| 14 | feat/async-mcp-tools (63d3cc9) | 9fbfd89, fd70990, 30558c5, 25942fa | MCP 3 ツールの async 化（要 #11） |
| 15 | chore/small-debts (49e3ebb) | 2873c23, 0d9da3f, 23bac49, a58949e, f300c11, eaf9be7 | setup 粒度入力・note_id 正規化・SKILL.md・e2e/dispatch テスト |
| 16 | feat/parallel-consult (585f803) | 1978572, 9da4b08, 8f6d5d3 | consult expert 呼び出しの並行化（要 #11） |
| 17 | feat/doctor-health (28b3f99) | 12265e6, 040b278, 58c2754, b4827e8 | doctor プリフライト診断 + /health（要 #12） |
| 18 | feat/routing-quality (e452fc4) | cc78b8e, 0a45de3, a249cd2, 7ca9515, 70e8ade, d65878d | 代表資料投影・warning 分離・リマップ可視化・**title mask 修正（Security）** |

### 各単位の適用メモ（競合しやすい箇所・検証）

1. **fix/persona-lazy-service** — 対象: `shelf/cli.py`（persona 分岐）。競合: personal の cli.py は emit-mcp/setup サブコマンドが無いぶん行番号がずれるが、persona 分岐自体は同型のはず。検証: `uv run pytest -q tests/test_cli.py`
2. **docs/readme-accuracy** — 対象: `README.md`, `SECURITY.md`。personal の README は Windows デプロイ前提の記述があるため**機械適用せず内容を手で反映**。SECURITY.md の脅威モデル節（tailnet 信頼境界）はむしろ personal の運用実態そのもので価値が高い。検証: なし（docs）
3. **fix/runner-windows-exec** — 対象: `shelf/engines/runner.py`。競合: personal 0.4.1 相当の Windows timeout フォールバック（`hasattr(os, "killpg")` + `proc.kill()`）と重なる領域。本修正は `taskkill /T /F` へ置き換えるため、旧フォールバックとの整合を取ること。検証: `uv run pytest -q tests/test_runner.py`
4. **feat/epub-support** — 対象: `shelf/convert.py`。競合: 少（`pick_converter` への経路追加）。検証: `uv run pytest -q tests/test_convert.py`
5. **fix/store-wal** — 対象: `shelf/store.py`（`__init__` の PRAGMA 順、`keyword_topk`）。競合: personal の `_init_fts` / FTS 自己修復（0.4.1〜0.4.3 相当）との差分に注意。**既存 DB は新コードで開くだけで WAL 化される**（下記「運用注意」）。検証: `uv run pytest -q tests/test_store.py tests/test_search.py`
6. **test/masking-rules** — 対象: `tests/test_masking.py`。personal の masking.py / distill/extract.py の規則が OSS と同一か確認してから移植（テストは現行挙動の仕様固定なので、規則が違えば期待値も変わる）。検証: `uv run pytest -q tests/test_masking.py`
7. **docs/import-design-docs** — **スキップ**。personal/docs が原本。ただし OSS 側で発見した宙吊り参照（tests/test_embedder.py の「§14」、shelf/setup.py の「タスク仕様 §1-3/§1-4」）は personal 側文書の改訂候補として申し送り。
8. **fix/small-remote-fixes** — 880b2f0（emit_mcp.py）は **personal に emit_mcp.py が無いためスキップ**。caea11a（cli.py の 0.0.0.0 警告）と 29646d0（names.py の予約名）は適用。**移行注意**: 予約デバイス名の notebook が既存なら適用後ロックアウトされる（CHANGELOG 0.5.0 Migration Notes 参照）。検証: `uv run pytest -q tests/test_names.py tests/test_cli.py`
9. **ci/windows-matrix** — 33662e4（.github/workflows/ci.yml）は personal に CI が無ければスキップ。**ccc141d と 099aed4（テストの POSIX 依存排除）は適用推奨** — personal の本番は Windows であり、Windows 上で `pytest` を回すために必要。検証: Windows 機で `uv run pytest -q`
10. **feat/local-file-size-limit** — 対象: `shelf/config.py`, `shelf/service.py`, `shelf/cli.py`。競合: config.py の env 定義集約部。検証: `uv run pytest -q tests/test_config.py tests/test_service.py`
11. **feat/store-thread-safety** — 対象: `shelf/store.py`（RLock）, `shelf/service.py`（遅延構築）。競合: #5 と同じく store.py。RLock は「公開メソッド全体」粒度・load_vectors の行列構築はロック外、という設計判断を崩さないこと。検証: `uv run pytest -q tests/test_store.py tests/test_service.py`
12. **feat/http-env-config** — 対象: `shelf/config.py`, `shelf/cli.py`（`resolve_serve_settings`）。競合: cli.py の serve 定義。env 名は `SHELF_HTTP_ENABLED/HOST/PORT` + `SHELF_ALLOWED_HOSTS`（serve-shelf.ps1 の既存変数名と互換）。検証: `uv run pytest -q tests/test_cli.py`
13. **feat/content-hash-dedup** — 対象: `shelf/service.py`（`_ingest_file`）, `shelf/store.py`。検証: `uv run pytest -q tests/test_service.py tests/test_store.py`
14. **feat/async-mcp-tools** — 対象: `shelf/server.py`, `pyproject.toml`（anyio>=4.1 直接依存）。**#11 が前提**（sync Store のままでは並行アクセスが不正）。検証: `uv run pytest -q tests/test_server.py`
15. **chore/small-debts** — 2873c23（setup.py）と 23bac49（SKILL.md）は **personal に shelf/setup.py が無ければスキップ**（SKILL.md は distill/extract 用のため、personalized-claude 側 distill/ への配置を別途検討）。0d9da3f（note_id 正規化）は適用 — **外部クライアント影響**: `insights[].note_id` の意味が変わる（生値は新設 `chunk_id` へ）。a58949e/f300c11（e2e/dispatch テスト）は適用。検証: `uv run pytest -q tests/test_e2e.py tests/test_cli.py tests/test_service.py`
16. **feat/parallel-consult** — 対象: `shelf/service.py`（consult）。**#11 が前提**（共有 embedder の Lock 直列化）。検証: `uv run pytest -q tests/test_service.py`
17. **feat/doctor-health** — 対象: `shelf/doctor.py`（新規）, `shelf/cli.py`, `shelf/server.py`。**#12 が前提**（doctor は serve 設定の解決を参照）。/health は認証外のため status/version のみ返す設計を維持。検証: `uv run pytest -q tests/test_doctor.py tests/test_server.py`
18. **feat/routing-quality** — 対象: `shelf/service.py`, `shelf/librarian.py`, `shelf/store.py`, `shelf/shelver.py`, `shelf/masking.py` 周辺。**cc78b8e（title の永続化時+投影時 mask）は Security 修正であり最優先で適用**。**移行注意**: 適用前に取り込んだ既存資料の title は未 mask のまま DB に残る — 投影時 mask で露出は防がれるが、DB 内の未 mask title を消すには再 add が必要。検証: `uv run pytest -q tests/test_service.py tests/test_librarian.py tests/test_shelver.py`

## personal 固有の注意

- **OSS 独自モジュールのスキップ**: `shelf/emit_mcp.py`・`shelf/setup.py`（および `tests/test_emit_mcp.py`・`tests/test_setup.py`）は personal に存在しない OSS 独自資産。これらへ触るコミット（880b2f0, 2873c23, 23bac49 の一部）はスキップまたは選択適用する。逆に `shelf/doctor.py` は本還流で personal へ新規追加される。
- **serve-shelf.ps1 の簡素化提案**: `setup/windows/serve-shelf.ps1` は現在、env（SHELF_HTTP_HOST/PORT/ALLOWED_HOSTS）を読み取って `--http --host --port --allowed-host` の CLI 引数列を自前で組み立てている。#12（http-env-config）適用後は shelf 本体が同じ env を直接解決するため、ps1 側の引数組み立て（`$serveArgs` 構築と AllowedHosts の分解ループ）は `SHELF_HTTP_ENABLED=1` を設定して `shelf serve` を裸で呼ぶだけに縮約できる。ps1 に残す価値があるのは Windows 固有の起動レース対策（`Wait-ForBindAddress`・Tailscale MagicDNS 導出・有界リトライループ）のみで、設定翻訳層は削除して二重管理を解消することを推奨。
- **既存 DB の WAL 化とバックアップ**: #5 適用後、既存 DB は新コードで開くだけで自動的に WAL 化される（`-wal`/`-shm` サイドカーが増える）。適用前に `VACUUM INTO 'backup.db'` で整合バックアップを取ること。DB を OneDrive 等のクラウド同期下に置いている場合は WAL が機能せずフェイルソフトする点も確認。
- **設計書はスキップ**: #7 のとおり personal/docs が原本。OSS 側はスクラブ済みコピー。

## 要ユーザー判断事項

還流作業に着手する前に、以下 3 点の判断が必要。

- **(a) distill/extract.py のクォート値マスク修正**: 汎用 password/secret/token regex の値キャプチャの過少マスク欠陥（`password: "hunter 2 with spaces"` のようなクォート付き複数語の値で先頭 1 トークンのみマスク）は **PR #15（fix/mask-quoted-values）で修正済み**です。extract.py は agent-recall と共有の正本のため、personal 側への還流時に同じ修正を extract.py へ同期する必要があります（agent-recall 側との同期は別途課題）。
- **(b) personal のバージョン採番**: 本ガイドは personal 0.6.0 を推奨（OSS 0.5.0 と番号が衝突し由来が曖昧になるのを避ける）。personal の CHANGELOG に「OSS 0.5.0 からの還流」と明記すること。
- **(c) 実 Windows 機での稼働確認手順**: 全単位適用後、(1) Windows 機で `shelf doctor` を実行し環境診断が green であること、(2) `serve-shelf.ps1`（または `SHELF_HTTP_ENABLED=1` で `shelf serve --http`）でサーブ起動、(3) Mac 側から `http://avalon.tail18a7d0.ts.net:8765/health` の疎通と MCP クライアント経由の `ask`/`consult` 実行、の順で確認する。手順の詳細化と実施タイミングはユーザー判断。

## 全体検証

全単位適用後、personal 側で:

```
uv run pytest -q       # 全件 green
uv run ruff check      # green
shelf doctor           # Windows 実機で診断 green
```
