"""司書のルーティング判断（prompt構成・パース・フォールバック）を担う純粋関数群。

外部 SDK・DB・subprocess・ネットワークを一切知らない。NotebookCard/RouteTarget/
RoutingDecision（ports.py）と str/dict/json 標準ライブラリのみを扱うことで、
Librarian（librarian.py・オーケストレーション層）を差し替えても本モジュールは
変更不要という境界を保つ（設計書 §3 依存方向 / §6 司書のルーティング仕様）。
"""
from __future__ import annotations

import json
from collections.abc import Sequence

from shelf.ports import NotebookCard, RouteTarget, RoutingDecision
from shelf.prompts import _extract_json_payload

# apply_fallback の score 降順クランプが設定値に関わらず超えない上限（設計書 §6-C 分岐4b）。
# 司書が誤って大量の notebook をルーティングし、専門家推論の fan-out が
# レイテンシ・コスト面で爆発する事故を構造的に防ぐための境界防御。
HARD_CAP_TOP_N = 2

# フォールバック方針として全 notebook 横断へ切り替える設定値
# （config.SHELF_ROUTE_FALLBACK と対応。既定はこれ以外＝保守的即答）。
FALLBACK_ALL = "all"

ROUTING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "notebook": {"type": "string"},
                    "score": {"type": "number"},
                    "subquery": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["notebook", "score", "subquery", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answerable", "targets"],
    "additionalProperties": False,
}

_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"answerable": true, "targets": [{"notebook": "...", "score": 0.9, '
    '"subquery": "...", "reason": "..."}]}'
)


def build_routing_prompt(question: str, catalog: Sequence[NotebookCard]) -> str:
    """質問文 + notebook カタログから司書ルーティング用プロンプトを組み立てる。"""
    instructions = [
        "あなたは資料室の司書です。以下の notebook 一覧から、質問に答えるために"
        "参照すべき notebook を選んでください。",
        "一覧に無い notebook 名を作り出さないでください。",
        "どの notebook でも回答できないと判断した場合は answerable を false にしてください。",
        _JSON_FORMAT_HINT,
    ]

    catalog_block = "\n\n".join(_format_card(card) for card in catalog)

    return "\n".join(instructions) + f"\n\n質問: {question}\n\nnotebook一覧:\n{catalog_block}"


def _format_card(card: NotebookCard) -> str:
    meta = [f"notebook: {card.name}", f"doc数: {card.doc_count}"]
    if card.description is not None:
        meta.append(f"概要: {card.description}")
    if card.persona is not None:
        meta.append(f"専門家像: {card.persona}")
    if card.tags:
        meta.append(f"タグ: {', '.join(card.tags)}")
    return "\n".join(meta)


def parse_routing(raw_text: str) -> RoutingDecision:
    """司書エンジンの生出力を ROUTING_SCHEMA として解釈する。

    prompts.parse_answer と同じ _extract_json_payload を再利用し、フェンス付き/
    前後ノイズ耐性を確保する（設計書 §6-B）。パース失敗は握り潰さず parse_ok=False
    で返し、apply_fallback の安全側フォールバックへ委ねる。answerable は必須項目
    として扱い、欠落・型不一致はパース失敗とする一方、targets 欠落は空リストとして
    許容する（parse_answer の citations 欠落許容と同じ寛容さの方針）。
    """
    payload = _extract_json_payload(raw_text)
    data = None
    if payload is not None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict) or not isinstance(data.get("answerable"), bool):
        return RoutingDecision(answerable=False, parse_ok=False, targets=[])

    return RoutingDecision(
        answerable=data["answerable"],
        parse_ok=True,
        targets=_parse_targets(data.get("targets")),
    )


def _parse_targets(raw: object) -> list[RouteTarget]:
    if not isinstance(raw, list):
        return []
    targets: list[RouteTarget] = []
    for item in raw:
        target = _parse_target_item(item)
        if target is not None:
            targets.append(target)
    return targets


def _parse_target_item(item: object) -> RouteTarget | None:
    if not isinstance(item, dict):
        return None
    notebook = item.get("notebook")
    score = item.get("score")
    subquery = item.get("subquery")
    reason = item.get("reason")
    if not isinstance(notebook, str) or not notebook:
        return None
    # bool は int のサブクラスだが JSON 上は真偽値であり score ではないため除外
    # （prompts._normalize_citation_ids と同じ方針）。
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    if not isinstance(subquery, str) or not isinstance(reason, str):
        return None
    return RouteTarget(notebook=notebook, score=float(score), subquery=subquery, reason=reason)


def apply_fallback(
    decision: RoutingDecision,
    catalog: Sequence[NotebookCard],
    question: str,
    top_n: int,
    fallback: str,
) -> list[RouteTarget]:
    """RoutingDecision + カタログ + 原質問 + 設定から最終的な実行対象を返す（設計書 §6-C）。

    分岐は以下の優先順位で評価する:
    1. カタログが空 → 専門家を呼ばない（対象ゼロ）。
    2. answerable=false → 専門家を呼ばない（レイテンシ保護の既定）。
    3. parse_ok=false または targets が空 → fallback 設定で選択
       （既定は保守的に対象ゼロ、fallback="all" のときのみカタログ全体へ横断）。
    4. targets が存在 → 幻覚 notebook 除去 → score 降順 top_n クランプ（hard cap 適用）
       → 重複除去 → subquery 空補完、の順で最終対象を組み立てる。

    question は §6-D の Librarian.route(question, catalog) が保持する原質問であり、
    分岐 4(d) の subquery 空補完に必要なため、本関数の入力として明示的に受け取る
    （設計書 §6-D の疑似コードは簡略表記のため引数を省略しているが、原質問なしには
    分岐 4(d) を実装できない）。
    """
    if not catalog:
        return []

    if not decision.answerable:
        return []

    if not decision.parse_ok or not decision.targets:
        return _fallback_across_catalog(catalog, question, top_n, fallback)

    return _clamp_targets(decision.targets, catalog, question, top_n)


def _fallback_across_catalog(
    catalog: Sequence[NotebookCard], question: str, top_n: int, fallback: str
) -> list[RouteTarget]:
    if fallback != FALLBACK_ALL:
        return []

    effective_top_n = _effective_top_n(top_n)
    # LLM のスコア付けが得られない失敗経路のため、カタログ順の先頭から hard cap 件を
    # 機械的に選ぶ（恣意的な優先付けをしない・設計書 §6-C 分岐3「後述の hard cap 内」）。
    return [
        RouteTarget(
            notebook=card.name,
            score=0.0,
            subquery=question,
            reason="ルーティング解析に失敗したため全notebookから機械的に選択",
        )
        for card in catalog[:effective_top_n]
    ]


def _clamp_targets(
    targets: Sequence[RouteTarget],
    catalog: Sequence[NotebookCard],
    question: str,
    top_n: int,
) -> list[RouteTarget]:
    known_names = {card.name for card in catalog}
    # (a) 幻覚 notebook 除去: カタログに実在する名前のみ残す
    # （structured 出力の幻覚が未知 notebook アクセスに化けるのを防ぐ境界防御）。
    existing = [t for t in targets if t.notebook in known_names]

    # (b) score 降順ソート + top_n クランプ（hard cap で上限を強制）。
    effective_top_n = _effective_top_n(top_n)
    ranked = sorted(existing, key=lambda t: t.score, reverse=True)[:effective_top_n]

    # (c) 同一 notebook の重複除去（先に出現した = より高スコア側を優先）。
    deduped: list[RouteTarget] = []
    seen: set[str] = set()
    for target in ranked:
        if target.notebook in seen:
            continue
        seen.add(target.notebook)
        deduped.append(target)

    # (d) subquery が空文字なら原質問へフォールバック。
    return [
        target if target.subquery else _with_fallback_subquery(target, question)
        for target in deduped
    ]


def _with_fallback_subquery(target: RouteTarget, question: str) -> RouteTarget:
    return RouteTarget(
        notebook=target.notebook, score=target.score, subquery=question, reason=target.reason
    )


def _effective_top_n(top_n: int) -> int:
    return max(0, min(top_n, HARD_CAP_TOP_N))
