"""自動分類投入（`shelf shelve`）の分類判断を担う純粋関数群（設計書 §13）。

外部 SDK・DB・subprocess・ネットワークを一切知らない。`json`（stdlib）+ `ports.py` の DTO
+ `names.py`（純粋）だけに依存することで、Shelver（shelver.py・オーケストレーション層）を
差し替えても本モジュールは変更不要という境界を保つ（設計書 §3 依存方向 / §13.3 import
ガード）。`routing.py` は `prompts._extract_json_payload` を共有再利用しているが、
shelving.py の許容依存には prompts.py が含まれない（設計書 §13.3 が列挙する
「json + ports.py + names.py だけ」を厳守するため）ので、同等のロジックを
`_extract_json_payload` としてこのモジュール内に閉じて複製する。

classify_step は「幻覚 notebook 名の除去」「名前正規化・衝突連番」を二重防御として
一手に引き受ける（設計書 §13.4/§13.5）。呼び出し側（Shelver）は working カタログ
（pre-run ∪ this-run 作成分）を毎回渡すことで、増分カタログの成長をこの純粋関数の
外側でスレッディングする（classify_step 自体は catalog を書き換えない・ステートレス）。
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from shelf.names import assign_unique_name, normalize_notebook_name
from shelf.ports import (
    ClassificationDecision,
    FileSummary,
    NewNotebookSpec,
    NotebookCard,
    ShelfAssignment,
)

# パース失敗時に落ちる決定的な既定 reason。生の壊れた JSON 断片やモデルの余計な
# 発話をそのまま assignment.reason へ漏らさないための固定文言（設計書 §13.4 の
# 「二重防御」思想を parse 失敗経路にも適用する）。
_PARSE_FAILURE_REASON = "分類結果を解析できなかったため新規作成として扱います"

# CLASSIFY_SCHEMA の action 列挙値（routing.py の RouteTarget と同様、enum 外を
# parse 失敗として弾くための単一の真実源）。
_VALID_ACTIONS = ("assign", "new")

CLASSIFY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["assign", "new"]},
        "notebook": {"type": "string"},
        "description": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "notebook", "reason"],
    "additionalProperties": False,
}

_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"action": "assign", "notebook": "...", "description": "...", "reason": "..."}'
)


def build_classification_prompt(summary: FileSummary, catalog: Sequence[NotebookCard]) -> str:
    """ファイル要約 + notebook カタログから分類判断用プロンプトを組み立てる（設計書 §13.4）。"""
    instructions = [
        "あなたは資料室の司書です。以下の notebook 一覧と新資料の要約を読み、"
        "この資料をどの notebook に入れるべきか判断してください。",
        "既存 notebook のいずれかが主題に合致するなら action=assign・notebook に"
        "その名前を入れてください。",
        "assign は、資料の主題が既存 notebook の説明（概要）と明確に合致する場合"
        "のみ選んでください。合致するか迷う場合や説明と一致しない場合は、無関係な"
        "notebook に投入すると検索品質が損なわれるため、必ず action=new を選んで"
        "ください（notebook が増えること自体は失敗ではありません）。",
        "どれにも合致しないなら action=new・notebook に簡潔な新名"
        "（英小文字・数字・-/_ のみ）・description に説明を入れてください。",
        "action=new を選ぶ場合は、その notebook が今後扱う主題を1〜2文で説明する"
        "description を必ず入れてください（description が空だと後続の分類判断で"
        "参照できなくなります）。",
        "notebook は同種の資料を複数まとめて収める棚です。name と description は、"
        "この資料 1 件だけの内容ではなく、資料が属する主題カテゴリの粒度で付けて"
        "ください（例: 個別レシピ名ではなく『料理レシピ』、特定ツールの一機能では"
        "なくそのツール全般のように、上位カテゴリの粒度でまとめてください）。",
        "description は『この notebook が扱う主題』を1〜2文で表す説明にしてくだ"
        "さい。『この資料は…』のような個別資料の要約文にしないでください。",
        "一覧に無い notebook 名へ assign しないでください。",
        _JSON_FORMAT_HINT,
    ]

    catalog_block = "\n\n".join(_format_card(card) for card in catalog)

    return (
        "\n".join(instructions)
        + f"\n\n資料: {summary.origin}\n要約: {summary.summary}"
        + f"\n\nnotebook一覧:\n{catalog_block}"
    )


def _format_card(card: NotebookCard) -> str:
    meta = [f"notebook: {card.name}", f"doc数: {card.doc_count}"]
    if card.description is not None:
        meta.append(f"概要: {card.description}")
    return "\n".join(meta)


def parse_classification(text: str) -> ClassificationDecision:
    """分類推論の生出力テキストを CLASSIFY_SCHEMA として解釈する（設計書 §13.4/§13.6）。

    `parse_routing`（routing.py）と同じ寛容さの方針: フェンス付き/前後ノイズ耐性を
    確保しつつ、必須項目（action・notebook・reason）の欠落・型不一致・enum 外・
    非 dict はすべて parse_ok=False とし、classify_step の安全側フォールバック
    （新規作成への再解釈）に委ねる。description は必須に含めないため、欠落・
    非文字列はいずれも None として許容する（§13.4: `new` 以外では不要）。
    """
    payload = _extract_json_payload(text)
    data = None
    if payload is not None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict):
        return _parse_failure()

    action = data.get("action")
    notebook = data.get("notebook")
    reason = data.get("reason")

    if action not in _VALID_ACTIONS:
        return _parse_failure()
    if not isinstance(notebook, str) or not notebook:
        return _parse_failure()
    if not isinstance(reason, str):
        return _parse_failure()

    description = data.get("description")
    if not isinstance(description, str):
        description = None

    return ClassificationDecision(
        action=action, notebook=notebook, reason=reason, parse_ok=True, description=description
    )


def _parse_failure() -> ClassificationDecision:
    return ClassificationDecision(action="", notebook="", reason="", parse_ok=False, description=None)


def _extract_json_payload(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


@dataclass(frozen=True)
class StepResult:
    """`classify_step` の 1 ファイル分の結果（設計書 §13.3）。

    assignment は常に確定する（幻覚・パース失敗も新規作成への再解釈で必ず解決する
    ため、ファイルを失う経路が存在しない）。new_notebook はこのステップで新規
    notebook 作成が必要になった場合のみ非 None になり、呼び出し側（Shelver）は
    これを `plan.created` に積み、working カタログへ NotebookCard として追記する
    ことで増分カタログを成長させる（classify_step 自身は catalog を書き換えない）。
    """

    assignment: ShelfAssignment
    new_notebook: NewNotebookSpec | None = None


def _resolve_new_notebook_description(decision: ClassificationDecision, summary: FileSummary) -> str:
    """新規作成 notebook の description を決定する（実機ログで観測された空 description 実害の対処）。

    司書ルーティング（routing.py）は notebook description のみを判断材料にするため、
    空 description の notebook は事実上ルーティング不能になる。モデルが description を
    返さなかった・空白のみを返した場合は、当該ファイルの要約（FileSummary.summary）を
    代替として採用する（劣化許容: summary も空ならそのまま空文字列を返す）。
    action=new・幻覚 assign 再解釈・parse 失敗のすべての経路がこの関数を経由するため、
    3 経路個別の対処を書かずに一箇所で解決する。
    """
    candidate = decision.description if (decision.parse_ok and decision.description) else ""
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return summary.summary if summary.summary and summary.summary.strip() else ""


def classify_step(
    decision: ClassificationDecision,
    summary: FileSummary,
    catalog: Sequence[NotebookCard],
    backend: str,
) -> StepResult:
    """1 ファイル分の分類判断を確定形（ShelfAssignment）へ変換する（設計書 §13.4/§13.5）。

    catalog は呼び出し側が this-run 作成分まで反映した working カタログである前提。
    実在 notebook への assign 判定・新規名の衝突判定の両方をこの単一の catalog を
    基準に行う（§13.5「working カタログ = pre-run ∪ this-run 作成分」）。

    3 つの安全側フォールバック経路（assign 幻覚・action=new・parse 失敗）はすべて
    「名前を正規化して新規作成として扱う」という同じ処理に収束するため、if 分岐は
    「実在 notebook への正当な assign か否か」の 1 箇所のみで足りる。
    """
    known_names = {card.name for card in catalog}

    if decision.parse_ok and decision.action == "assign" and decision.notebook in known_names:
        assignment = ShelfAssignment(
            origin=summary.origin,
            notebook=decision.notebook,
            new_notebook=False,
            summary=summary.summary,
            reason=decision.reason,
        )
        return StepResult(assignment=assignment)

    # ここに来るのは (a) assign 先が実在しない幻覚、(b) 正当な action=new、
    # (c) parse_ok=False のいずれか。(a)/(b) はモデルが提案した名前を正規化し、
    # (c) は手がかりが無いため既定名（"" → normalize で "notebook"）に倒す。
    raw_name = decision.notebook if decision.parse_ok else ""
    normalized = normalize_notebook_name(raw_name)
    unique_name = assign_unique_name(normalized, known_names)

    description = _resolve_new_notebook_description(decision, summary)
    reason = decision.reason if decision.parse_ok else _PARSE_FAILURE_REASON

    new_notebook = NewNotebookSpec(name=unique_name, description=description, backend=backend)
    assignment = ShelfAssignment(
        origin=summary.origin,
        notebook=unique_name,
        new_notebook=True,
        summary=summary.summary,
        reason=reason,
    )
    return StepResult(assignment=assignment, new_notebook=new_notebook)
