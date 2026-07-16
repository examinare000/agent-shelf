"""prompts.py の純粋関数テスト。RetrievedChunk fixture のみを使い、
外部 CLI・ネットワークには一切触れない。
"""
from __future__ import annotations

from shelf.ports import RetrievedChunk
from shelf.prompts import (
    ANSWER_SCHEMA,
    SUMMARY_INPUT_MAX_CHARS,
    SUMMARY_SCHEMA,
    ParsedAnswer,
    build_ask_prompt,
    build_summary_prompt,
    parse_answer,
    parse_summary,
)


def _chunk(**overrides) -> RetrievedChunk:
    base = dict(
        id="nb/doc#0",
        doc_id="doc",
        source_path="feynman-lectures.md",
        section="§3.2",
        page=42,
        text="本文サンプル",
    )
    base.update(overrides)
    return RetrievedChunk(**base)


class TestBuildAskPrompt:
    def test_numbers_chunks_sequentially_starting_at_one(self):
        chunks = [_chunk(text="一つ目"), _chunk(text="二つ目"), _chunk(text="三つ目")]

        prompt = build_ask_prompt("質問です", chunks)

        assert "[S1]" in prompt
        assert "[S2]" in prompt
        assert "[S3]" in prompt
        assert prompt.index("[S1]") < prompt.index("[S2]") < prompt.index("[S3]")

    def test_formats_source_section_and_page(self):
        chunk = _chunk(source_path="feynman-lectures.md", section="§3.2", page=42, text="本文")

        prompt = build_ask_prompt("質問", [chunk])

        assert "source: feynman-lectures.md" in prompt
        assert "節: §3.2" in prompt
        assert "p.42" in prompt
        assert "本文" in prompt

    def test_omits_section_when_none(self):
        chunk = _chunk(section=None, page=42)

        prompt = build_ask_prompt("質問", [chunk])

        assert "節:" not in prompt
        assert "p.42" in prompt

    def test_omits_page_when_none(self):
        chunk = _chunk(section="§1", page=None)

        prompt = build_ask_prompt("質問", [chunk])

        assert "p." not in prompt
        assert "節: §1" in prompt

    def test_omits_both_when_section_and_page_are_none(self):
        chunk = _chunk(section=None, page=None, source_path="notes.md")

        prompt = build_ask_prompt("質問", [chunk])

        assert "節:" not in prompt
        assert "p." not in prompt
        assert "source: notes.md" in prompt

    def test_includes_question_text(self):
        prompt = build_ask_prompt("宇宙の年齢は？", [_chunk()])

        assert "宇宙の年齢は？" in prompt

    def test_deep_dive_false_by_default_omits_instruction(self):
        prompt = build_ask_prompt("質問", [_chunk()])

        assert "作業ディレクトリ" not in prompt

    def test_deep_dive_true_adds_instruction(self):
        prompt = build_ask_prompt("質問", [_chunk()], deep_dive=True)

        assert "作業ディレクトリ" in prompt

    def test_includes_grounding_instruction_and_json_format(self):
        prompt = build_ask_prompt("質問", [_chunk()])

        assert "資料からは分からない" in prompt
        assert "confident" in prompt
        assert "citations" in prompt


class TestBuildAskPromptCompatibility:
    """R5 の最重要検証点: persona=None かつ digest チャンクなしなら既存実装と
    byte 単位で同一プロンプトになること（ask 互換の証明・設計書 §10 R5）。

    golden 文字列は拡張前の build_ask_prompt を実際に実行して採取したもの
    （既存 test_numbers_chunks_sequentially_starting_at_one と同じ fixture を再利用）。
    """

    _GOLDEN_PROMPT = (
        "以下の抜粋のみを根拠に日本語で回答してください。各主張に [S番号] を付してください。\n"
        "抜粋に根拠が無い場合は推測せず「資料からは分からない」と答え、"
        "confident は false としてください。\n"
        '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
        '{"answer": "...", "citations": [{"s": 1}], "confident": true}\n'
        "\n質問: 質問です\n\n"
        "[S1] (source: feynman-lectures.md, 節: §3.2, p.42)\n一つ目\n\n"
        "[S2] (source: feynman-lectures.md, 節: §3.2, p.42)\n二つ目\n\n"
        "[S3] (source: feynman-lectures.md, 節: §3.2, p.42)\n三つ目"
    )

    def test_persona_none_and_no_digest_chunks_matches_golden_byte_for_byte(self):
        chunks = [_chunk(text="一つ目"), _chunk(text="二つ目"), _chunk(text="三つ目")]

        prompt = build_ask_prompt("質問です", chunks)

        assert prompt == self._GOLDEN_PROMPT

    def test_explicit_persona_none_keyword_also_matches_golden(self):
        chunks = [_chunk(text="一つ目"), _chunk(text="二つ目"), _chunk(text="三つ目")]

        prompt = build_ask_prompt("質問です", chunks, persona=None)

        assert prompt == self._GOLDEN_PROMPT


class TestBuildAskPromptPersona:
    def test_persona_injects_instruction_mentioning_persona(self):
        prompt = build_ask_prompt("質問", [_chunk()], persona="量子力学の専門家")

        assert "量子力学の専門家" in prompt

    def test_persona_instruction_precedes_existing_instructions(self):
        prompt = build_ask_prompt("質問", [_chunk()], persona="量子力学の専門家")

        assert prompt.index("量子力学の専門家") < prompt.index("[S番号]")

    def test_persona_none_omits_persona_wording(self):
        prompt = build_ask_prompt("質問", [_chunk()], persona="量子力学の専門家")
        prompt_without = build_ask_prompt("質問", [_chunk()])

        assert "専門家" not in prompt_without or "量子力学の専門家" not in prompt_without
        assert prompt != prompt_without


class TestBuildAskPromptInsights:
    def test_digest_chunks_get_l_numbering_separate_from_s(self):
        chunks = [
            _chunk(text="本文抜粋", kind="body"),
            _chunk(text="学びその1", kind="digest"),
            _chunk(text="学びその2", kind="digest"),
        ]

        prompt = build_ask_prompt("質問", chunks)

        assert "[S1]" in prompt
        assert "[L1]" in prompt
        assert "[L2]" in prompt
        assert "[S2]" not in prompt  # digest チャンクは S 系列を消費しない

    def test_summary_kind_chunks_use_s_numbering_like_body(self):
        chunks = [_chunk(text="概要", kind="summary"), _chunk(text="本文", kind="body")]

        prompt = build_ask_prompt("質問", chunks)

        assert "[S1]" in prompt
        assert "[S2]" in prompt
        assert "[L1]" not in prompt

    def test_digest_chunk_text_appears_in_prompt(self):
        chunks = [_chunk(text="この学びが重要", kind="digest")]

        prompt = build_ask_prompt("質問", chunks)

        assert "この学びが重要" in prompt

    def test_no_digest_chunks_omits_insights_instruction_and_l_marker(self):
        prompt = build_ask_prompt("質問", [_chunk(kind="body")])

        assert "[L" not in prompt
        assert '"insights"' not in prompt

    def test_digest_chunks_add_insights_to_json_format_hint(self):
        chunks = [_chunk(kind="body"), _chunk(kind="digest")]

        prompt = build_ask_prompt("質問", chunks)

        assert '"insights"' in prompt
        assert '"l": 1' in prompt


class TestAnswerSchema:
    def test_schema_declares_answer_citations_confident(self):
        assert ANSWER_SCHEMA["type"] == "object"
        props = ANSWER_SCHEMA["properties"]
        assert props["answer"]["type"] == "string"
        assert props["citations"]["type"] == "array"
        assert props["citations"]["items"]["properties"]["s"]["type"] == "integer"
        assert props["confident"]["type"] == "boolean"

    def test_all_object_nodes_forbid_additional_properties(self):
        """OpenAI の厳格 JSON スキーマ検証は全 object ノードに additionalProperties:false を
        要求する（バグ#2: これが無いと codex --output-schema が invalid_json_schema で exit 1
        になる）。スキーマ内の全 object ノードを再帰的に走査して検証する。"""

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

        _walk(ANSWER_SCHEMA)

    def test_insights_is_optional_array_of_l_numbers(self):
        props = ANSWER_SCHEMA["properties"]

        assert props["insights"]["type"] == "array"
        assert props["insights"]["items"]["properties"]["l"]["type"] == "integer"
        assert "insights" not in ANSWER_SCHEMA["required"]


class TestParseAnswer:
    def test_parses_plain_json(self):
        text = '{"answer": "42です [S1]", "citations": [{"s": 1}], "confident": true}'

        result = parse_answer(text)

        assert result == ParsedAnswer(
            answer="42です [S1]", citation_ids=[1], confident=True, parse_ok=True
        )

    def test_parses_json_fenced_in_markdown_code_block(self):
        text = (
            "```json\n"
            '{"answer": "回答", "citations": [{"s": 2}], "confident": false}\n'
            "```"
        )

        result = parse_answer(text)

        assert result.parse_ok is True
        assert result.answer == "回答"
        assert result.citation_ids == [2]
        assert result.confident is False

    def test_parses_json_with_leading_and_trailing_text(self):
        text = (
            "承知しました。以下が回答です。\n"
            '{"answer": "回答本文", "citations": [{"s": 1}], "confident": true}\n'
            "以上です。"
        )

        result = parse_answer(text)

        assert result.parse_ok is True
        assert result.answer == "回答本文"
        assert result.citation_ids == [1]

    def test_degrades_gracefully_on_broken_json(self):
        text = "これはJSONではない壊れたテキスト {answer: 未閉じ"

        result = parse_answer(text)

        assert result.parse_ok is False
        assert result.answer == text
        assert result.citation_ids == []
        assert result.confident is False

    def test_removes_duplicate_citation_ids(self):
        text = '{"answer": "回答", "citations": [{"s": 1}, {"s": 1}, {"s": 2}], "confident": true}'

        result = parse_answer(text)

        assert result.citation_ids == [1, 2]

    def test_removes_non_positive_citation_ids(self):
        text = '{"answer": "回答", "citations": [{"s": 0}, {"s": -1}, {"s": 3}], "confident": true}'

        result = parse_answer(text)

        assert result.citation_ids == [3]

    def test_removes_non_integer_citation_ids(self):
        text = '{"answer": "回答", "citations": [{"s": "1"}, {"s": 1.5}, {"s": 4}], "confident": true}'

        result = parse_answer(text)

        assert result.citation_ids == [4]

    def test_missing_citations_field_yields_empty_list(self):
        text = '{"answer": "回答", "confident": false}'

        result = parse_answer(text)

        assert result.parse_ok is True
        assert result.citation_ids == []
        assert result.confident is False

    def test_parses_insights_from_l_keys(self):
        text = (
            '{"answer": "回答 [L1]", "citations": [], '
            '"insights": [{"l": 1}], "confident": true}'
        )

        result = parse_answer(text)

        assert result.parse_ok is True
        assert result.insight_ids == [1]

    def test_missing_insights_field_yields_empty_list(self):
        text = '{"answer": "回答", "citations": [{"s": 1}], "confident": true}'

        result = parse_answer(text)

        assert result.parse_ok is True
        assert result.insight_ids == []

    def test_removes_duplicate_and_non_positive_insight_ids(self):
        text = (
            '{"answer": "回答", "citations": [], '
            '"insights": [{"l": 1}, {"l": 1}, {"l": 0}, {"l": -1}, {"l": 2}], '
            '"confident": true}'
        )

        result = parse_answer(text)

        assert result.insight_ids == [1, 2]

    def test_degrades_gracefully_on_broken_json_yields_empty_insight_ids(self):
        result = parse_answer("これはJSONではない壊れたテキスト {answer: 未閉じ")

        assert result.insight_ids == []


class TestSummarySchema:
    def test_summary_schema_declares_summary_string_and_forbids_additional_properties(self):
        assert SUMMARY_SCHEMA["type"] == "object"
        assert SUMMARY_SCHEMA["required"] == ["summary"]
        assert SUMMARY_SCHEMA["properties"]["summary"]["type"] == "string"

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

        _walk(SUMMARY_SCHEMA)


class TestBuildSummaryPrompt:
    def test_truncates_input_to_max_chars(self):
        markdown = "あ" * SUMMARY_INPUT_MAX_CHARS + "この目印は含まれない"

        prompt = build_summary_prompt(markdown)

        assert "あ" * SUMMARY_INPUT_MAX_CHARS in prompt
        assert "この目印は含まれない" not in prompt

    def test_includes_json_format_instruction(self):
        prompt = build_summary_prompt("本文")

        assert "JSON" in prompt
        assert "summary" in prompt

    def test_includes_title_when_given(self):
        prompt = build_summary_prompt("本文", title="ファインマン物理学")

        assert "ファインマン物理学" in prompt

    def test_omits_title_section_when_not_given(self):
        prompt_with = build_summary_prompt("本文", title="タイトル")
        prompt_without = build_summary_prompt("本文")

        assert "タイトル" not in prompt_without
        assert "タイトル" in prompt_with


class TestParseSummary:
    def test_parses_plain_json(self):
        text = '{"summary": "量子力学の入門解説"}'

        assert parse_summary(text) == "量子力学の入門解説"

    def test_parses_fenced_json(self):
        text = '```json\n{"summary": "統計力学の教科書"}\n```'

        assert parse_summary(text) == "統計力学の教科書"

    def test_returns_none_on_broken_json(self):
        assert parse_summary("これはJSONではない {summary: 未閉じ") is None

    def test_returns_none_when_not_dict(self):
        assert parse_summary("[1, 2, 3]") is None

    def test_returns_none_when_summary_missing(self):
        assert parse_summary("{}") is None

    def test_returns_none_when_summary_not_string(self):
        assert parse_summary('{"summary": 123}') is None

    def test_returns_none_when_summary_is_blank(self):
        assert parse_summary('{"summary": "   "}') is None

    def test_strips_surrounding_whitespace(self):
        assert parse_summary('{"summary": "  要約本文  "}') == "要約本文"
