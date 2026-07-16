"""AnswerBackend ポート定義と中立 DTO。

サブスク CLI エンジン（codex/gemini_cli/agy）の実装詳細をドメイン層から隠蔽するための
Protocol。RetrievedChunk/RawAnswer は本プロジェクト定義の中立 dataclass であり、
外部ライブラリ固有の型をドメイン層（service.py/prompts.py）に漏らさないための境界。
標準ライブラリのみに依存する（設計書 §6 の import ガード方針に整合）。

NotebookCard/RouteTarget/RoutingDecision/StudyNote は増分設計書
（docs/design-shelf-reference-service.md §6/§7）が定義する司書（routing.py/librarian.py）・
専門家（digests.py）向けの中立 DTO。routing.py/digests.py は store・sqlite3・fastembed 等の
外部依存を一切 import しない（§3 import ガード）ため、これらの DTO を経由してのみ
service.py とデータをやり取りする。

FileSummary/ClassificationDecision/ShelfAssignment/NewNotebookSpec/ShelvePlan は
第 2 の増分設計書（同 §13）が定義する自動分類投入（shelving.py/shelver.py）向けの
中立 DTO。shelving.py/shelver.py も同じ import ガード方針（外部依存ゼロ）に従うため、
これらの DTO を経由してのみ service.py とデータをやり取りする。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    doc_id: str
    source_path: str
    section: str | None
    page: int | None
    text: str
    # 'body'（本文抜粋・既定）| 'summary'（資料概要）| 'digest'（学びノート）。
    # 既定値を付けることで、kind を知らない既存呼び出し箇所（service._load_chunks 等）を
    # 壊さずに追加する（design §10 R1 の後方互換要件）。
    kind: str = "body"


@dataclass(frozen=True)
class RawAnswer:
    text: str
    ok: bool
    error: str | None  # 安全な要約のみ（コマンド全文・出力全文・認証情報は含めない）


class AnswerBackend(Protocol):
    name: str

    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer: ...


@dataclass(frozen=True)
class NotebookCard:
    """司書のルーティング判断に使う notebook の投影 DTO（設計書 §6-A）。

    service が store.list_notebooks() + persona から組み立てて Librarian.route() へ渡す。
    store 由来の行そのものではなく、routing に必要な最小情報だけを持つ
    （Librarian を store から切り離すための境界）。
    """

    name: str
    description: str | None
    persona: str | None
    doc_count: int


@dataclass(frozen=True)
class RouteTarget:
    """司書が選んだルーティング先 1 件（設計書 §6-B/§6-D）。

    ROUTING_SCHEMA の targets[] 要素、および apply_fallback 後の最終実行対象の
    両方をこの型で表す。
    """

    notebook: str
    score: float
    subquery: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    """routing.parse_routing() の生パース結果（設計書 §6-B）。

    apply_fallback（routing.py・純粋）がこれとカタログ・設定を入力に
    最終的な list[RouteTarget] を組み立てる。parse_ok=False はパース失敗を
    握り潰さず安全側フォールバックへ渡すための明示フラグ。
    """

    answerable: bool
    parse_ok: bool
    targets: list[RouteTarget] = field(default_factory=list)


@dataclass(frozen=True)
class StudyNote:
    """学びノート 1 件（設計書 §7-B）。digests.parse_digest() が DIGEST_SCHEMA
    ({"notes": [{"text": "str", "span": "str"}]}) から抽出する中立表現。

    store.study_notes テーブルの id/notebook/doc_id/seq/source_hash/model/created_at は
    service.digest() が書込み時に付与する（このモデルは LLM 出力の忠実な抽出に留める）。
    """

    text: str
    span: str | None = None


@dataclass(frozen=True)
class FileSummary:
    """`Shelver.plan` への入力 1 件（設計書 §13.6）。

    分類対象ファイル 1 件につき origin（resolve 済み絶対パス）と要約テキストの組。
    要約は service が既存 build_summary_prompt 経路で生成した既存成果物を再利用する
    （§13.1 決定 5・分類専用の別要約経路は作らない）。
    """

    origin: str
    summary: str


@dataclass(frozen=True)
class ClassificationDecision:
    """`shelving.parse_classification()` の生パース結果（設計書 §13.6）。

    CLASSIFY_SCHEMA の action/notebook/reason は必須だが description は必須に
    含めない（§13.4: `new` 以外では不要）ため、description のみ既定 None を持つ。
    dataclass は「デフォルト値を持たない引数の後にデフォルト値を持つ引数を置けない」
    制約があるため、description をフィールド末尾に置く（設計書列挙順とは並びが異なるが
    キーワード引数で構築するため実害はない）。parse_ok=False はパース失敗
    （不正 JSON・非 dict・enum 外・必須欠落）を握り潰さず、classify_step の
    安全側フォールバック（新規作成への再解釈）へ渡すための明示フラグ
    （RoutingDecision.parse_ok と同じ契機）。
    """

    action: str
    notebook: str
    reason: str
    parse_ok: bool
    description: str | None = None


@dataclass(frozen=True)
class ShelfAssignment:
    """分類が解決した 1 エントリ（設計書 §13.6）。

    ClassificationDecision（生パース結果）に対し、幻覚 notebook 名の再解釈・
    名前正規化・衝突連番（§13.4/§13.5）まで適用した後の確定形。dry-run 出力・
    適用時の投入先の両方がこの DTO を共有する。
    """

    origin: str
    notebook: str
    new_notebook: bool
    summary: str
    reason: str


@dataclass(frozen=True)
class NewNotebookSpec:
    """`shelve` が新規作成すべき notebook 1 件（設計書 §13.6）。

    backend は個別ファイルの分類結果ではなく config.SHELVE_BACKEND に倒される
    （§13.1 決定 6: 推論バックエンドは 1 つの config に集約）。
    """

    name: str
    description: str
    backend: str


@dataclass(frozen=True)
class ShelvePlan:
    """`Shelver.plan()` の集約結果（分類段・設計書 §13.6）。

    dry-run 出力・適用時の両方の元になる第一級データ構造（§13.1 決定 2）。
    4 フィールドとも mutable な list のため、他の集約 DTO（RoutingDecision.targets）と
    同じく field(default_factory=list) でインスタンス間の共有を断つ。
    """

    assignments: list[ShelfAssignment] = field(default_factory=list)
    created: list[NewNotebookSpec] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
