"""shelf.jsonutil（LLM 出力からの JSON 抽出・パース共通ヘルパー）のテスト（P12）。

prompts.py/shelving.py/digests.py が各自複製していた _extract_json_payload と、
digests.py 固有だった _parse_json_object を統合した公開 API を検証する。
"""
from __future__ import annotations

from shelf.jsonutil import extract_json_payload, parse_json_object


class TestExtractJsonPayload:
    def test_extracts_plain_json(self):
        assert extract_json_payload('{"a": 1}') == '{"a": 1}'

    def test_extracts_json_from_fenced_code_block(self):
        text = '```json\n{"a": 1}\n```'
        assert extract_json_payload(text) == '{"a": 1}'

    def test_extracts_json_surrounded_by_prose(self):
        text = 'ここに回答があります: {"a": 1} 以上です。'
        assert extract_json_payload(text) == '{"a": 1}'

    def test_returns_none_when_no_braces_present(self):
        assert extract_json_payload("JSON がありません") is None

    def test_returns_none_when_closing_brace_precedes_opening_brace(self):
        assert extract_json_payload("} {") is None


class TestParseJsonObject:
    def test_parses_plain_json_object(self):
        assert parse_json_object('{"a": 1}') == {"a": 1}

    def test_parses_json_from_fenced_code_block(self):
        text = '```json\n{"a": 1, "b": [1, 2]}\n```'
        assert parse_json_object(text) == {"a": 1, "b": [1, 2]}

    def test_returns_none_for_non_json_text(self):
        assert parse_json_object("JSON がありません") is None

    def test_returns_none_for_malformed_json(self):
        assert parse_json_object("{a: 1,}") is None

    def test_returns_none_when_top_level_value_is_not_an_object(self):
        assert parse_json_object("[1, 2, 3]") is None
