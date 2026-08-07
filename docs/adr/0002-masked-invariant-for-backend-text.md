# ADR-0002: backend へ送る全テキストは mask 済みという不変条件の維持

## 状態

採択（2026-08-02、feat/routing-quality の title mask 修正で確立）

## 文脈

shelf は機微情報を含み得る資料を取り込み、LLM backend（クラウド CLI を含む）へ
検索結果やカタログを投入して回答を合成する。取込時マスキングが防御の要だが、
v0.5.0 開発中のレビューで `documents.title` が永続化時点から mask 未適用である
ことが発見された。従来 title は ingest 時の一回限りのプロンプトにしか現れず露出面が
狭かったため見過ごされていたが、司書ルーティングへの代表資料カタログ投影
（feat/routing-quality）により、毎 consult で backend へ恒常露出する経路になるところだった。

## 決定

「backend へ送出される全テキストは mask を通過済みである」を系の不変条件として明文化し、
title については **永続化時と投影時の双方で mask を適用する二重防御**とする。
新しい露出経路（プロンプトへ載せるデータの追加）を作るときは、載せるデータの
sanitize 履歴を取込時点まで遡って確認することを設計時の必須チェックとする。

## 検討した選択肢

- **投影時（プロンプト構築時）のみ mask**: 露出は防げるが、DB 内に未 mask データが
  残り続け、将来の新経路（エクスポート・別ツール連携）が同じ事故を再生産する。
  → 単独では棄却し、二重防御の一翼として採用。
- **永続化時のみ mask**: 新規データは安全になるが、修正適用前に取り込まれた既存行の
  未 mask title が投影経路からそのまま露出する。
  → 単独では棄却し、二重防御の一翼として採用。既存 DB の完全な浄化には再 add が必要
  （backport-0.5.0.md の移行注意に記載）。

## 帰結

- mask は「入口で一度」ではなく「入口と出口の双方」で保証され、どちらか一方の
  実装漏れ・既存データの取りこぼしが単独では事故にならない。
- 露出経路を追加する変更のレビュー観点として「載せるデータの sanitize 履歴確認」が
  確立した（trial-log 2026-08-02 の B7 レビュー知見）。
- 冗長な mask 適用の実行コストは title 程度の短文では無視できる。
- distill/extract.py 自体の規則欠陥（クォート付き複数語値の過少マスク）は本 ADR の
  範囲外で、agent-recall との同期方針決定待ち（backport-0.5.0.md 要ユーザー判断事項 (a)）。

## 追記（2026-08-07）

title 限定だった永続化時・投影時の二重防御を、notebook description・persona へ
拡張した。永続化時（`create_notebook`／shelve 新規 notebook 作成）と、投影・読み出し時
（`_build_catalog`／`ask`／`_consult_target`／`digest`）の計 6 箇所で mask を適用し、
title と同型の不変条件を満たす。description は title と異なり更新 API が無く notebook
再作成でのみ更新されるため、既存 DB 行の浄化は投影・読み出し時 mask の二重防御に
より恒久的に担保する（CHANGELOG.md [Unreleased] Security 参照）。

フレッシュレビューでこの初回修正の悉皆性が崩され、残存経路が2種類見つかった。
1つ目は `Shelver.plan()` の working_catalog（分類 LLM 応答由来の description を
次ファイルの分類プロンプトへ渡す経路）— スコープを service.py の6箇所に限定した
ため、shelver.py という別モジュールの露出経路が最初の悉皆確認から漏れていた
（fix/prompt-title-mask の「関数名の grep だけでは不十分」教訓と同型の見落とし方）。
2つ目は利用者向けの読み取り出力（MCP `list_notebooks` ツール・CLI `shelf persona`
表示）— これらは backend へのプロンプトではないが、AI エージェントの文脈へ直接
流れる／人間の目に触れるという点で同じ不変条件の対象とみなし、`_masked`／
`shelf.masking.mask` を適用した。「backend へ送出される全テキスト」という文言は
プロンプト構築点だけでなく、mask 未適用のまま外部へ渡る全ての出力（MCP ツール
戻り値・CLI 表示を含む）を指すと解釈を明確化する。

adversarial-verifier のレビューで、この2回の修正でもなお実証済みの穴が1件・
文書の過大宣言が残っていた。`shelf shelve --dry-run` の JSON 出力
（`created_notebooks[*].description`・`plan[*].reason`）と MCP `consult` 戻り値の
`routed[*].reason` が、それぞれ plan.created（shelver.py が永続化用に意図的に
保持する生 description）・分類/ルーティング LLM の自由記述をそのまま素通しして
いた。reason は description/persona/title と異なり「取込資料由来の DB 値」では
なく「LLM が都度生成する自由記述」だが、mask 正本（distill/extract.py）に既知の
残存制限がある以上、LLM 出力そのものに secret が混入している可能性を排除できない
（本 ADR が最初から採用している脅威モデルの帰結）。不変条件の対象を
「backend へ送出されるテキスト」から「backend へのプロンプト・DB 由来の投影値・
LLM が生成してクライアントへ返る自由記述フィールド」まで明示的に拡張した。

**スコープ外として残す既知事項**（詳細は SECURITY.md 既知の制限節）:
1. 修正適用前に永続化された `documents.description` の未 mask 既存行は、索引
   （index）時の要約チャンク化経由で backend へ流れうる。この経路には投影時
   二重防御が及んでいない。indexer は `documents.description` を書き換えない
   ため、`shelf add --desc` の明示指定、または `auto_summary=True` での
   再投入時のみ mask 済み値へ更新される（`shelf ingest` は `auto_summary=False`
   のためこの経路では更新されない）。
2. `ShelfService(shelver=...)` の直接注入は `_get_shelver()` による
   `mask=self._mask` の自動配線をバイパスする。現状この注入口はテスト専用で
   実運用の呼び出し箇所はない（死んだ注入口）。

## 追記2（2026-08-07・再検証 REJECT）

adversarial-verifier の再検証で、実装の穴1件（収束方向）と文書の過大宣言が
指摘された。

- **穴**: `_consult_target()` の `routed[*].subquery` が未 mask のまま残っていた。
  `subquery` はルーティング応答の同一 JSON から `reason` と一緒に取り出す兄弟
  フィールドだが、`reason` を直した際にこの兄弟フィールドの掃引を行わなかった
  （「同一データの全流出先を追跡する」という fix/prompt-title-mask 由来の教訓の
  3 度目の再発）。`subquery` はクライアント出力に加えて `_answer_with_expert`
  経由で専門家プロンプトへも投入されるため、`reason` より露出が広い。読み出し点
  1箇所（`_consult_target`）で mask した値をクライアント出力・専門家プロンプトの
  両方に使う形で修正した（digest の persona 修正と対称の設計）。
- **文書の過大宣言**: 前回の追記で「クライアントへ返る全ての LLM 生成フィールドは
  mask 済み」と書いたが、これは実装が満たす範囲を超えた過大な宣言だった（回答本文
  `answer`/`insights`/`citations` は mask していない）。不変条件の対象を
  「ルーティング/分類のメタ情報フィールド（`reason`/`subquery`/新規 notebook の
  `description`）」に明示的に絞り、回答本文は「索引時に入力チャンクが mask 済み
  であることを根拠とした対象外」として明記した。列挙で閉じる過小（前々回の反省）
  と、実装を超える過大宣言（今回の反省）の両方を避けるため、対象を「メタ情報
  フィールド」という性質で区切る書き方へ変更した。
- **事実誤りの訂正**: 「再 add または再 index で自然に mask 済み値へ更新される」
  という記述が虚偽だった。indexer は `documents.description` を書き換えない
  （upsert/update 呼び出しがない）ため、再 index では更新されない。正しくは
  「`shelf add --desc` 指定時、または `auto_summary=True` での再投入時のみ
  更新される（`shelf ingest` は `auto_summary=False` のため更新されない）」。
- **packaging 制約の開示**: masking 依存機能（`shelf persona` 表示を含む）は
  `distill/extract.py` の実在に依存するが、`pyproject.toml` の
  `packages = ["shelf"]` は `distill/` を wheel に含めない。リポジトリ直接
  checkout・`SHELF_EXTRACT_PY` 指定以外の配布形態では動作しない、という既存の
  系統的制約を SECURITY.md へ新規開示した。
- **検証者が誤検知として棄却した項目**: shelve notes の `raw_name`（フォールバック
  由来の notebook 名）は SECRET_RES 系パターン（`sk-`/`ghp_`/`AKIA` 等）と
  `validate_notebook_name` が許可する文字集合（英小文字・数字・-/_ のみ）が
  構造的に排他であるため、secret を含み得ないと判定され false positive として
  棄却された。
