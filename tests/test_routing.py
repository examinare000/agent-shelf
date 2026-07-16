"""routing.py の純粋関数テスト（司書のルーティング判断）。

NotebookCard/RouteTarget/RoutingDecision（ports.py）の手組み fixture のみを使い、
外部 CLI・ネットワーク・DB には一切触れない（設計書 §6/§9-A）。
apply_fallback は設計書 §6-C の全分岐（空カタログ・answerable=false・幻覚 notebook
除去・top-N クランプ・重複除去・subquery 空補完）を網羅する。
"""
from __future__ import annotations

from shelf.ports import NotebookCard, RouteTarget, RoutingDecision
from shelf.routing import (
    HARD_CAP_TOP_N,
    ROUTING_SCHEMA,
    apply_fallback,
    build_routing_prompt,
    parse_routing,
)


def _card(**overrides) -> NotebookCard:
    base = dict(
        name="quantum-mechanics",
        description="量子力学の教材集",
        persona="量子力学の専門家",
        doc_count=3,
    )
    base.update(overrides)
    return NotebookCard(**base)


def _target(**overrides) -> RouteTarget:
    base = dict(
        notebook="quantum-mechanics",
        score=0.9,
        subquery="スピン角運動量の交換関係",
        reason="スピンは量子力学ノートの主題",
    )
    base.update(overrides)
    return RouteTarget(**base)


class TestBuildRoutingPrompt:
    def test_includes_question_text(self):
        prompt = build_routing_prompt("スピンとは何ですか", [_card()])

        assert "スピンとは何ですか" in prompt

    def test_includes_notebook_name_and_doc_count(self):
        card = _card(name="quantum-mechanics", doc_count=5)

        prompt = build_routing_prompt("質問", [card])

        assert "quantum-mechanics" in prompt
        assert "5" in prompt

    def test_includes_description_when_present(self):
        card = _card(description="量子力学の教材集")

        prompt = build_routing_prompt("質問", [card])

        assert "量子力学の教材集" in prompt

    def test_omits_description_when_none(self):
        card = _card(description=None)

        prompt = build_routing_prompt("質問", [card])

        assert "概要:" not in prompt

    def test_includes_persona_when_present(self):
        card = _card(persona="量子力学の専門家")

        prompt = build_routing_prompt("質問", [card])

        assert "量子力学の専門家" in prompt

    def test_omits_persona_when_none(self):
        card = _card(persona=None)

        prompt = build_routing_prompt("質問", [card])

        assert "専門家像:" not in prompt

    def test_includes_multiple_notebooks(self):
        cards = [_card(name="quantum-mechanics"), _card(name="statistical-mechanics")]

        prompt = build_routing_prompt("質問", cards)

        assert "quantum-mechanics" in prompt
        assert "statistical-mechanics" in prompt

    def test_includes_json_format_instruction(self):
        prompt = build_routing_prompt("質問", [_card()])

        assert "JSON" in prompt
        assert "answerable" in prompt
        assert "targets" in prompt

    def test_instructs_not_to_invent_notebook_names(self):
        prompt = build_routing_prompt("質問", [_card()])

        assert "一覧に無い" in prompt


class TestRoutingSchema:
    def test_declares_answerable_and_targets(self):
        assert ROUTING_SCHEMA["type"] == "object"
        props = ROUTING_SCHEMA["properties"]
        assert props["answerable"]["type"] == "boolean"
        assert props["targets"]["type"] == "array"
        assert ROUTING_SCHEMA["required"] == ["answerable", "targets"]

    def test_target_items_declare_required_fields(self):
        item_schema = ROUTING_SCHEMA["properties"]["targets"]["items"]

        assert item_schema["required"] == ["notebook", "score", "subquery", "reason"]
        assert item_schema["properties"]["notebook"]["type"] == "string"
        assert item_schema["properties"]["score"]["type"] == "number"
        assert item_schema["properties"]["subquery"]["type"] == "string"
        assert item_schema["properties"]["reason"]["type"] == "string"

    def test_all_object_nodes_forbid_additional_properties(self):
        """codex --output-schema は全 object ノードに additionalProperties:false を要求する
        （prompts.py の ANSWER_SCHEMA と同様の制約。test_prompts.py の踏襲）。"""

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, (
                        f"object ノードに additionalProperties: false がありません: {node}"
                    )
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(ROUTING_SCHEMA)


class TestParseRouting:
    def test_parses_plain_json(self):
        text = (
            '{"answerable": true, "targets": [{"notebook": "quantum-mechanics", '
            '"score": 0.9, "subquery": "スピン", "reason": "主題が一致"}]}'
        )

        decision = parse_routing(text)

        assert decision.parse_ok is True
        assert decision.answerable is True
        assert decision.targets == [
            RouteTarget(
                notebook="quantum-mechanics", score=0.9, subquery="スピン", reason="主題が一致"
            )
        ]

    def test_parses_json_fenced_in_markdown_code_block(self):
        text = (
            "```json\n"
            '{"answerable": false, "targets": []}\n'
            "```"
        )

        decision = parse_routing(text)

        assert decision.parse_ok is True
        assert decision.answerable is False
        assert decision.targets == []

    def test_parses_json_with_leading_and_trailing_text(self):
        text = (
            "承知しました。\n"
            '{"answerable": true, "targets": []}\n'
            "以上です。"
        )

        decision = parse_routing(text)

        assert decision.parse_ok is True
        assert decision.answerable is True

    def test_degrades_gracefully_on_broken_json(self):
        text = "これはJSONではない壊れたテキスト {answerable: 未閉じ"

        decision = parse_routing(text)

        assert decision.parse_ok is False
        assert decision.answerable is False
        assert decision.targets == []

    def test_missing_answerable_field_is_parse_failure(self):
        text = '{"targets": []}'

        decision = parse_routing(text)

        assert decision.parse_ok is False

    def test_missing_targets_field_yields_empty_list(self):
        text = '{"answerable": true}'

        decision = parse_routing(text)

        assert decision.parse_ok is True
        assert decision.targets == []

    def test_drops_target_items_missing_required_fields(self):
        text = (
            '{"answerable": true, "targets": ['
            '{"notebook": "nb"}, '
            '{"notebook": "nb2", "score": 0.5, "subquery": "q", "reason": "r"}'
            "]}"
        )

        decision = parse_routing(text)

        assert decision.parse_ok is True
        assert decision.targets == [
            RouteTarget(notebook="nb2", score=0.5, subquery="q", reason="r")
        ]

    def test_drops_target_items_with_non_string_notebook(self):
        text = (
            '{"answerable": true, "targets": ['
            '{"notebook": 123, "score": 0.5, "subquery": "q", "reason": "r"}'
            "]}"
        )

        decision = parse_routing(text)

        assert decision.targets == []


class TestApplyFallbackEmptyCatalog:
    def test_empty_catalog_returns_no_targets_even_if_answerable_with_targets(self):
        decision = RoutingDecision(
            answerable=True, parse_ok=True, targets=[_target()]
        )

        result = apply_fallback(decision, [], "質問", top_n=1, fallback="conservative")

        assert result == []


class TestApplyFallbackNotAnswerable:
    def test_not_answerable_returns_no_targets(self):
        decision = RoutingDecision(answerable=False, parse_ok=True, targets=[_target()])

        result = apply_fallback(
            decision, [_card()], "質問", top_n=1, fallback="conservative"
        )

        assert result == []

    def test_not_answerable_returns_no_targets_even_with_all_fallback(self):
        """専門家推論のレイテンシ保護（設計書 §6-C 分岐2）: answerable=false は
        fallback=all でも専門家を呼ばない。"""
        decision = RoutingDecision(answerable=False, parse_ok=True, targets=[])

        result = apply_fallback(decision, [_card()], "質問", top_n=1, fallback="all")

        assert result == []


class TestApplyFallbackParseFailureOrEmptyTargets:
    def test_parse_failure_with_conservative_fallback_returns_no_targets(self):
        decision = RoutingDecision(answerable=True, parse_ok=False, targets=[])

        result = apply_fallback(
            decision, [_card()], "質問", top_n=1, fallback="conservative"
        )

        assert result == []

    def test_answerable_with_empty_targets_and_conservative_fallback_returns_no_targets(self):
        decision = RoutingDecision(answerable=True, parse_ok=True, targets=[])

        result = apply_fallback(
            decision, [_card()], "質問", top_n=1, fallback="conservative"
        )

        assert result == []

    def test_parse_failure_with_all_fallback_routes_to_catalog_notebooks(self):
        cards = [_card(name="nb-a"), _card(name="nb-b")]
        decision = RoutingDecision(answerable=True, parse_ok=False, targets=[])

        result = apply_fallback(decision, cards, "元の質問", top_n=2, fallback="all")

        assert [t.notebook for t in result] == ["nb-a", "nb-b"]
        assert all(t.subquery == "元の質問" for t in result)

    def test_all_fallback_is_clamped_to_hard_cap_even_with_larger_catalog(self):
        cards = [_card(name=f"nb-{i}") for i in range(5)]
        decision = RoutingDecision(answerable=True, parse_ok=False, targets=[])

        result = apply_fallback(decision, cards, "質問", top_n=5, fallback="all")

        assert len(result) == HARD_CAP_TOP_N

    def test_empty_targets_with_all_fallback_also_routes_to_catalog(self):
        cards = [_card(name="nb-a")]
        decision = RoutingDecision(answerable=True, parse_ok=True, targets=[])

        result = apply_fallback(decision, cards, "質問", top_n=1, fallback="all")

        assert [t.notebook for t in result] == ["nb-a"]


class TestApplyFallbackWithTargets:
    def test_filters_out_hallucinated_notebook_names(self):
        cards = [_card(name="real-notebook")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[
                _target(notebook="real-notebook", score=0.9),
                _target(notebook="hallucinated-notebook", score=0.99),
            ],
        )

        result = apply_fallback(decision, cards, "質問", top_n=2, fallback="conservative")

        assert [t.notebook for t in result] == ["real-notebook"]

    def test_all_targets_hallucinated_yields_empty_result(self):
        cards = [_card(name="real-notebook")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[_target(notebook="hallucinated-notebook", score=0.9)],
        )

        result = apply_fallback(decision, cards, "質問", top_n=1, fallback="conservative")

        assert result == []

    def test_clamps_to_default_top_n_and_sorts_by_score_descending(self):
        cards = [_card(name="nb-a"), _card(name="nb-b")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[
                _target(notebook="nb-a", score=0.5),
                _target(notebook="nb-b", score=0.9),
            ],
        )

        result = apply_fallback(decision, cards, "質問", top_n=1, fallback="conservative")

        assert [t.notebook for t in result] == ["nb-b"]

    def test_top_n_larger_than_hard_cap_is_clamped_to_hard_cap(self):
        cards = [_card(name=f"nb-{i}") for i in range(5)]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[
                _target(notebook=f"nb-{i}", score=float(i)) for i in range(5)
            ],
        )

        result = apply_fallback(decision, cards, "質問", top_n=100, fallback="conservative")

        assert len(result) == HARD_CAP_TOP_N
        # score 降順: nb-4(4.0), nb-3(3.0) が残る。
        assert [t.notebook for t in result] == ["nb-4", "nb-3"]

    def test_deduplicates_same_notebook_keeping_highest_ranked(self):
        cards = [_card(name="nb-a")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[
                _target(notebook="nb-a", score=0.9, subquery="q1"),
                _target(notebook="nb-a", score=0.9, subquery="q2"),
            ],
        )

        result = apply_fallback(decision, cards, "質問", top_n=2, fallback="conservative")

        assert len(result) == 1
        assert result[0].subquery == "q1"

    def test_empty_subquery_falls_back_to_original_question(self):
        cards = [_card(name="nb-a")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[_target(notebook="nb-a", subquery="")],
        )

        result = apply_fallback(decision, cards, "元の質問", top_n=1, fallback="conservative")

        assert result[0].subquery == "元の質問"

    def test_non_empty_subquery_is_kept_as_is(self):
        cards = [_card(name="nb-a")]
        decision = RoutingDecision(
            answerable=True,
            parse_ok=True,
            targets=[_target(notebook="nb-a", subquery="スピンについて")],
        )

        result = apply_fallback(decision, cards, "元の質問", top_n=1, fallback="conservative")

        assert result[0].subquery == "スピンについて"
