"""digests.py の純粋関数テスト。StudyNote/markdown fixture のみを使い、
外部 LLM・DB・subprocess・ネットワークには一切触れない。
"""
from __future__ import annotations

import json

from shelf.digests import (
    DIGEST_DEFAULT_MAX_NOTES,
    DIGEST_INPUT_MAX_CHARS,
    DIGEST_SCHEMA,
    build_digest_prompt,
    parse_digest,
)
from shelf.ports import StudyNote


def _walk_forbids_additional_properties(node: object) -> None:
    """スキーマ内の全 object ノードが additionalProperties: False を持つことを検証する
    （test_prompts.TestAnswerSchema と同じ検証 = codex --output-schema の厳格 JSON 要件）。"""
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, (
                f"object ノードに additionalProperties: false がありません: {node}"
            )
        for value in node.values():
            _walk_forbids_additional_properties(value)
    elif isinstance(node, list):
        for item in node:
            _walk_forbids_additional_properties(item)


class TestBuildDigestPrompt:
    def test_truncates_input_to_max_chars(self):
        markdown = "あ" * DIGEST_INPUT_MAX_CHARS + "この目印は含まれない"

        prompt = build_digest_prompt(markdown)

        assert "あ" * DIGEST_INPUT_MAX_CHARS in prompt
        assert "この目印は含まれない" not in prompt

    def test_respects_custom_max_chars(self):
        # config.DIGEST_INPUT_MAX_CHARS(env SHELF_DIGEST_INPUT_MAX_CHARS)を service.py
        # から配線するための additive パラメータ。既定値以外を渡した場合にも
        # 切詰めが実際にそのしきい値で行われることを検証する。
        markdown = "あ" * 100 + "この目印は含まれない"

        prompt = build_digest_prompt(markdown, max_chars=100)

        assert "あ" * 100 in prompt
        assert "この目印は含まれない" not in prompt

    def test_includes_json_format_instruction(self):
        prompt = build_digest_prompt("本文")

        assert "JSON" in prompt
        assert "notes" in prompt
        assert "text" in prompt

    def test_includes_no_hallucination_instruction(self):
        prompt = build_digest_prompt("本文")

        assert "推測で補わない" in prompt

    def test_includes_markdown_body(self):
        prompt = build_digest_prompt("量子力学の基礎について解説する。")

        assert "量子力学の基礎について解説する。" in prompt

    def test_omits_persona_section_when_none(self):
        prompt = build_digest_prompt("本文", persona=None)

        assert "あなたは" not in prompt

    def test_includes_persona_when_given(self):
        prompt = build_digest_prompt("本文", persona="量子力学の専門家")

        assert "量子力学の専門家" in prompt

    def test_omits_title_section_when_not_given(self):
        prompt_with = build_digest_prompt("本文", title="タイトル")
        prompt_without = build_digest_prompt("本文")

        assert "タイトル" not in prompt_without
        assert "タイトル" in prompt_with


class TestDigestSchema:
    def test_schema_declares_notes_array_of_text_and_span(self):
        assert DIGEST_SCHEMA["type"] == "object"
        assert DIGEST_SCHEMA["required"] == ["notes"]
        notes = DIGEST_SCHEMA["properties"]["notes"]
        assert notes["type"] == "array"
        item = notes["items"]
        assert item["properties"]["text"]["type"] == "string"
        assert item["properties"]["span"]["type"] == "string"
        assert item["required"] == ["text"]

    def test_all_object_nodes_forbid_additional_properties(self):
        _walk_forbids_additional_properties(DIGEST_SCHEMA)


class TestParseDigest:
    def test_parses_plain_json_with_notes(self):
        text = '{"notes": [{"text": "スピンは角運動量の一種", "span": "§2.1"}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="スピンは角運動量の一種", span="§2.1")]

    def test_parses_json_fenced_in_markdown_code_block(self):
        text = (
            "```json\n"
            '{"notes": [{"text": "学び本文", "span": "§1"}]}\n'
            "```"
        )

        result = parse_digest(text)

        assert result == [StudyNote(text="学び本文", span="§1")]

    def test_parses_json_with_leading_and_trailing_text(self):
        text = (
            "承知しました。以下が学びノートです。\n"
            '{"notes": [{"text": "学び本文", "span": "§1"}]}\n'
            "以上です。"
        )

        result = parse_digest(text)

        assert result == [StudyNote(text="学び本文", span="§1")]

    def test_degrades_gracefully_on_broken_json(self):
        text = "これはJSONではない壊れたテキスト {notes: 未閉じ"

        result = parse_digest(text)

        assert result == []

    def test_returns_empty_list_when_notes_missing(self):
        assert parse_digest("{}") == []

    def test_returns_empty_list_when_notes_not_list(self):
        assert parse_digest('{"notes": "not-a-list"}') == []

    def test_returns_empty_list_when_notes_is_empty(self):
        assert parse_digest('{"notes": []}') == []

    def test_parses_multiple_notes_in_order(self):
        text = (
            '{"notes": ['
            '{"text": "学び1", "span": "§1"}, '
            '{"text": "学び2", "span": "§2"}'
            "]}"
        )

        result = parse_digest(text)

        assert result == [
            StudyNote(text="学び1", span="§1"),
            StudyNote(text="学び2", span="§2"),
        ]

    def test_span_defaults_to_none_when_missing(self):
        text = '{"notes": [{"text": "学び本文"}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="学び本文", span=None)]

    def test_skips_notes_with_non_string_text(self):
        text = '{"notes": [{"text": 123, "span": "§1"}, {"text": "有効な学び"}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="有効な学び", span=None)]

    def test_skips_notes_with_empty_text_after_strip(self):
        text = '{"notes": [{"text": "   "}, {"text": "有効な学び"}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="有効な学び", span=None)]

    def test_strips_surrounding_whitespace_from_text(self):
        text = '{"notes": [{"text": "  学び本文  "}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="学び本文", span=None)]

    def test_span_becomes_none_when_not_a_string(self):
        text = '{"notes": [{"text": "学び本文", "span": 123}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="学び本文", span=None)]

    def test_skips_non_dict_items_in_notes_list(self):
        text = '{"notes": ["not-a-dict", {"text": "有効な学び"}]}'

        result = parse_digest(text)

        assert result == [StudyNote(text="有効な学び", span=None)]

    def test_clamps_to_default_max_notes(self):
        notes = [{"text": f"学び{i}"} for i in range(DIGEST_DEFAULT_MAX_NOTES + 3)]
        text = json.dumps({"notes": notes})

        result = parse_digest(text)

        assert len(result) == DIGEST_DEFAULT_MAX_NOTES
        assert result[0].text == "学び0"
        assert result[-1].text == f"学び{DIGEST_DEFAULT_MAX_NOTES - 1}"

    def test_clamps_to_custom_max_notes(self):
        notes = [{"text": f"学び{i}"} for i in range(5)]
        text = json.dumps({"notes": notes})

        result = parse_digest(text, max_notes=2)

        assert len(result) == 2
        assert [n.text for n in result] == ["学び0", "学び1"]

    def test_does_not_clamp_when_under_max_notes(self):
        text = json.dumps({"notes": [{"text": "唯一の学び"}]})

        result = parse_digest(text, max_notes=5)

        assert len(result) == 1
