"""digests.py の純粋関数テスト。StudyNote/markdown fixture のみを使い、
外部 LLM・DB・subprocess・ネットワークには一切触れない。
"""
from __future__ import annotations

import inspect
import json

from shelf.digests import (
    MAP_DEFAULT_NOTES,
    MAP_SCHEMA,
    REDUCE_DEFAULT_NOTES,
    REDUCE_SCHEMA,
    WINDOW_DEFAULT_CHARS,
    build_map_prompt,
    build_reduce_prompt,
    group_into_windows,
    normalize_tag,
    normalize_tags,
    parse_map,
    parse_reduce,
    select_reduce_input,
)
from shelf.ports import StudyNote


def _chunk(id: str, *, section: str | None, page: int | None, seq: int, text: str) -> dict:
    return {"id": id, "section": section, "page": page, "seq": seq, "text": text}


class TestGroupIntoWindows:
    def test_empty_input_returns_empty_list(self):
        assert group_into_windows([]) == []

    def test_single_chunk_becomes_single_window(self):
        chunks = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        windows = group_into_windows(chunks)

        assert windows == [chunks]

    def test_packs_same_section_chunks_under_char_limit_into_one_window(self):
        chunks = [
            _chunk("nb/doc#0", section="§1", page=1, seq=0, text="あ" * 100),
            _chunk("nb/doc#1", section="§1", page=1, seq=1, text="い" * 100),
        ]

        windows = group_into_windows(chunks, window_chars=1000)

        assert windows == [chunks]

    def test_packs_different_section_chunks_under_char_limit_into_one_window(self):
        # 節が変わっても、window_chars の予算内であれば window を分けない
        # （P5: 見出し密度に比例して map 呼び出しが増える強制分割を廃止した）。
        # チャンクは section/page メタデータを保持したまま同一 window に混在できる
        # （接地はチャンク単位であり、prompt 整形も各チャンクを個別に節・ページ付きで表示する）。
        first = _chunk("nb/doc#0", section="§1", page=1, seq=0, text="あ" * 10)
        second = _chunk("nb/doc#1", section="§2", page=2, seq=1, text="い" * 10)

        windows = group_into_windows([first, second], window_chars=1000)

        assert windows == [[first, second]]

    def test_breaks_window_when_char_limit_exceeded_within_same_section(self):
        first = _chunk("nb/doc#0", section="§1", page=1, seq=0, text="あ" * 60)
        second = _chunk("nb/doc#1", section="§1", page=1, seq=1, text="い" * 60)

        windows = group_into_windows([first, second], window_chars=100)

        assert windows == [[first], [second]]

    def test_oversized_single_chunk_becomes_its_own_window(self):
        huge = _chunk("nb/doc#0", section="§1", page=1, seq=0, text="あ" * 200)
        small = _chunk("nb/doc#1", section="§1", page=1, seq=1, text="い" * 10)

        windows = group_into_windows([huge, small], window_chars=100)

        assert windows == [[huge], [small]]

    def test_none_section_chunks_pack_together(self):
        first = _chunk("nb/doc#0", section=None, page=None, seq=0, text="あ" * 10)
        second = _chunk("nb/doc#1", section=None, page=None, seq=1, text="い" * 10)

        windows = group_into_windows([first, second], window_chars=1000)

        assert windows == [[first, second]]

    def test_preserves_chunk_order_within_and_across_windows(self):
        # window_chars=25: 先頭2チャンク(計20字)は収まるが、3チャンク目を足すと
        # 30字で超過するため、そこで window を分ける（節が変わったからではなく、
        # 純粋に文字数予算のみが境界を決めることを確認する）。
        chunks = [
            _chunk("nb/doc#0", section="§1", page=1, seq=0, text="あ" * 10),
            _chunk("nb/doc#1", section="§1", page=1, seq=1, text="い" * 10),
            _chunk("nb/doc#2", section="§2", page=2, seq=2, text="う" * 10),
        ]

        windows = group_into_windows(chunks, window_chars=25)

        assert windows == [
            [chunks[0], chunks[1]],
            [chunks[2]],
        ]

    def test_many_small_sections_pack_into_few_windows_bounded_by_window_chars(self):
        # 見出し密度が高い文書（見出しごとに1節=1チャンク）でも、window は
        # window_chars 予算のみで決まるため、節数に比例して window 数が
        # 増えることはない（P5 の核心: 150見出し文書が150 map 呼び出しになる
        # 問題の再発防止）。
        chunks = [
            _chunk(f"nb/doc#{i}", section=f"§{i}", page=None, seq=i, text="あ" * 10)
            for i in range(20)
        ]

        windows = group_into_windows(chunks, window_chars=100)

        assert len(windows) == 2
        assert [len(w) for w in windows] == [10, 10]


class TestSharedDefaultConstants:
    """既定値5/20/8000がdigests.py・config.py・service.pyで裸リテラル重複しないよう、
    named constant を唯一の情報源として signature が参照していることを固定する
    （P13: 従来はコメントでの「同値にして矛盾を避ける」目視同期しかなかった）。"""

    def test_map_default_notes_is_five(self):
        assert MAP_DEFAULT_NOTES == 5

    def test_reduce_default_notes_is_twenty(self):
        assert REDUCE_DEFAULT_NOTES == 20

    def test_build_map_prompt_and_parse_map_default_max_notes_share_constant(self):
        assert (
            inspect.signature(build_map_prompt).parameters["max_notes"].default
            == MAP_DEFAULT_NOTES
        )
        assert inspect.signature(parse_map).parameters["max_notes"].default == MAP_DEFAULT_NOTES

    def test_build_reduce_prompt_and_parse_reduce_default_max_notes_share_constant(self):
        assert (
            inspect.signature(build_reduce_prompt).parameters["max_notes"].default
            == REDUCE_DEFAULT_NOTES
        )
        assert (
            inspect.signature(parse_reduce).parameters["max_notes"].default
            == REDUCE_DEFAULT_NOTES
        )

    def test_group_into_windows_default_window_chars_is_eight_thousand(self):
        assert WINDOW_DEFAULT_CHARS == 8000
        assert (
            inspect.signature(group_into_windows).parameters["window_chars"].default
            == WINDOW_DEFAULT_CHARS
        )


class TestBuildMapPrompt:
    def test_numbers_chunks_with_section_and_page(self):
        window = [
            _chunk("nb/doc#0", section="§1", page=1, seq=0, text="第一段落の本文"),
            _chunk("nb/doc#1", section="§1", page=2, seq=1, text="第二段落の本文"),
        ]

        prompt = build_map_prompt(window)

        assert "[C1] (節: §1, p.1)" in prompt
        assert "第一段落の本文" in prompt
        assert "[C2] (節: §1, p.2)" in prompt
        assert "第二段落の本文" in prompt

    def test_omits_meta_parens_when_section_and_page_are_none(self):
        window = [_chunk("nb/doc#0", section=None, page=None, seq=0, text="本文")]

        prompt = build_map_prompt(window)

        assert "[C1]\n本文" in prompt

    def test_includes_json_format_instruction_with_chunks_field(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window)

        assert "JSON" in prompt
        assert "notes" in prompt
        assert "chunks" in prompt

    def test_includes_grounding_instruction_for_chunk_reference(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window)

        assert "根拠チャンク番号" in prompt

    def test_includes_no_hallucination_instruction(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window)

        assert "推測で補わない" in prompt

    def test_includes_max_notes_in_instruction(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window, max_notes=3)

        assert "3" in prompt

    def test_omits_persona_section_when_none(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window, persona=None)

        assert "あなたは" not in prompt

    def test_includes_persona_when_given(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt = build_map_prompt(window, persona="量子力学の専門家")

        assert "量子力学の専門家" in prompt

    def test_omits_title_section_when_not_given(self):
        window = [_chunk("nb/doc#0", section="§1", page=1, seq=0, text="本文")]

        prompt_with = build_map_prompt(window, title="タイトル")
        prompt_without = build_map_prompt(window)

        assert "タイトル" not in prompt_without
        assert "タイトル" in prompt_with


class TestMapSchema:
    def test_schema_declares_notes_array_of_text_and_chunks(self):
        assert MAP_SCHEMA["type"] == "object"
        assert MAP_SCHEMA["required"] == ["notes"]
        notes = MAP_SCHEMA["properties"]["notes"]
        assert notes["type"] == "array"
        item = notes["items"]
        assert item["properties"]["text"]["type"] == "string"
        assert item["properties"]["chunks"]["type"] == "array"
        assert item["properties"]["chunks"]["items"]["type"] == "integer"
        assert set(item["required"]) == {"text", "chunks"}

    def test_all_object_nodes_forbid_additional_properties(self):
        _walk_forbids_additional_properties(MAP_SCHEMA)


class TestParseMap:
    def _window(self):
        return [
            _chunk("nb/doc#0", section="§1", page=1, seq=0, text="第一段落"),
            _chunk("nb/doc#1", section="§1", page=2, seq=1, text="第二段落"),
        ]

    def test_resolves_chunk_number_to_id_section_page(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [1]}]}'

        result = parse_map(text, window)

        assert result == [
            StudyNote(text="学び本文", chunk_ids=("nb/doc#0",), section="§1", page=1)
        ]

    def test_multiple_chunk_references_become_chunk_ids_tuple_in_order(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [2, 1]}]}'

        result = parse_map(text, window)

        assert result[0].chunk_ids == ("nb/doc#1", "nb/doc#0")

    def test_representative_section_page_come_from_first_referenced_chunk(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [2, 1]}]}'

        result = parse_map(text, window)

        assert result[0].section == "§1"
        assert result[0].page == 2

    def test_out_of_range_chunk_number_is_dropped_but_note_kept(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [99, 1]}]}'

        result = parse_map(text, window)

        assert result == [
            StudyNote(text="学び本文", chunk_ids=("nb/doc#0",), section="§1", page=1)
        ]

    def test_zero_chunk_number_is_dropped(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [0, 1]}]}'

        result = parse_map(text, window)

        assert result[0].chunk_ids == ("nb/doc#0",)

    def test_negative_chunk_number_is_dropped(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [-1, 1]}]}'

        result = parse_map(text, window)

        assert result[0].chunk_ids == ("nb/doc#0",)

    def test_non_integer_chunk_number_is_dropped(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": ["a", 1]}]}'

        result = parse_map(text, window)

        assert result[0].chunk_ids == ("nb/doc#0",)

    def test_note_kept_with_empty_chunk_ids_when_all_references_invalid(self):
        window = self._window()
        text = '{"notes": [{"text": "学び本文", "chunks": [99]}]}'

        result = parse_map(text, window)

        assert result == [StudyNote(text="学び本文", chunk_ids=(), section=None, page=None)]

    def test_note_dropped_when_text_missing(self):
        window = self._window()
        text = '{"notes": [{"chunks": [1]}]}'

        assert parse_map(text, window) == []

    def test_note_dropped_when_text_empty_after_strip(self):
        window = self._window()
        text = '{"notes": [{"text": "   ", "chunks": [1]}]}'

        assert parse_map(text, window) == []

    def test_degrades_gracefully_on_broken_json(self):
        window = self._window()
        text = "これはJSONではない壊れたテキスト {notes: 未閉じ"

        assert parse_map(text, window) == []

    def test_returns_empty_list_when_notes_missing(self):
        window = self._window()

        assert parse_map("{}", window) == []

    def test_clamps_to_max_notes(self):
        window = self._window()
        notes = [{"text": f"学び{i}", "chunks": [1]} for i in range(7)]
        text = json.dumps({"notes": notes})

        result = parse_map(text, window, max_notes=3)

        assert len(result) == 3
        assert [n.text for n in result] == ["学び0", "学び1", "学び2"]

    def test_strips_surrounding_whitespace_from_text(self):
        window = self._window()
        text = '{"notes": [{"text": "  学び本文  ", "chunks": [1]}]}'

        result = parse_map(text, window)

        assert result[0].text == "学び本文"


class TestBuildReducePrompt:
    def _map_notes(self):
        return [
            StudyNote(text="学び1", chunk_ids=("nb/doc#0",), section="§1", page=1),
            StudyNote(text="学び2", chunk_ids=("nb/doc#1",), section="§2", page=2),
        ]

    def test_numbers_map_notes(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "[N1] 学び1" in prompt
        assert "[N2] 学び2" in prompt

    def test_includes_json_format_instruction_with_sources_and_tags_field(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "JSON" in prompt
        assert "notes" in prompt
        assert "sources" in prompt
        assert "tags" in prompt

    def test_includes_dedup_instruction(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "重複" in prompt

    def test_includes_source_note_number_instruction(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "元ノート番号" in prompt

    def test_includes_keep_specificity_instruction(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "薄い一般論に丸めない" in prompt

    def test_includes_tag_instruction(self):
        prompt = build_reduce_prompt(self._map_notes())

        assert "タグ" in prompt

    def test_omits_tag_catalog_section_when_empty(self):
        prompt = build_reduce_prompt(self._map_notes(), tag_catalog=())

        assert "既存タグ一覧" not in prompt

    def test_includes_tag_catalog_when_given(self):
        prompt = build_reduce_prompt(self._map_notes(), tag_catalog=("量子力学", "物理"))

        assert "既存タグ一覧" in prompt
        assert "量子力学" in prompt
        assert "物理" in prompt

    def test_includes_max_notes_in_instruction(self):
        prompt = build_reduce_prompt(self._map_notes(), max_notes=10)

        assert "10" in prompt

    def test_omits_persona_section_when_none(self):
        prompt = build_reduce_prompt(self._map_notes(), persona=None)

        assert "あなたは" not in prompt

    def test_includes_persona_when_given(self):
        prompt = build_reduce_prompt(self._map_notes(), persona="量子力学の専門家")

        assert "量子力学の専門家" in prompt

    def test_omits_title_section_when_not_given(self):
        prompt_with = build_reduce_prompt(self._map_notes(), title="タイトル")
        prompt_without = build_reduce_prompt(self._map_notes())

        assert "タイトル" not in prompt_without
        assert "タイトル" in prompt_with


class TestSelectReduceInput:
    """コードレビュー指摘 P6: reduce プロンプトへ全 map ノートを無上限展開すると
    ウィンドウ数に比例して入力が肥大化し、reduce 呼び出しが失敗しやすくなる。
    REDUCE_INPUT_MAX_CHARS 予算内に収めるための間引き選択のテスト。"""

    def _notes(self, n: int, *, text_len: int) -> list[StudyNote]:
        return [
            StudyNote(text=f"学び{i}" + "あ" * (text_len - len(f"学び{i}")), chunk_ids=(f"nb/doc#{i}",))
            for i in range(n)
        ]

    def test_returns_all_notes_unchanged_when_under_budget(self):
        notes = self._notes(3, text_len=10)

        selected = select_reduce_input(notes, max_chars=1000)

        assert selected == notes

    def test_empty_list_returns_empty_list(self):
        assert select_reduce_input([], max_chars=1000) == []

    def test_samples_evenly_across_whole_list_keeping_first_and_last_within_budget(self):
        notes = self._notes(10, text_len=50)  # 合計500字

        selected = select_reduce_input(notes, max_chars=170)

        # 均等ストライドで [0, 4, 9] の3件（計150字、次の4件(200字)は予算超過）。
        assert selected == [notes[0], notes[4], notes[9]]
        assert sum(len(note.text) for note in selected) <= 170

    def test_sampled_selection_preserves_original_order(self):
        notes = self._notes(10, text_len=50)

        selected = select_reduce_input(notes, max_chars=170)

        original_indices = [notes.index(note) for note in selected]
        assert original_indices == sorted(original_indices)


class TestReduceSchema:
    def test_schema_declares_notes_array_of_text_and_sources_and_tags(self):
        assert REDUCE_SCHEMA["type"] == "object"
        assert set(REDUCE_SCHEMA["required"]) == {"notes", "tags"}
        notes = REDUCE_SCHEMA["properties"]["notes"]
        assert notes["type"] == "array"
        item = notes["items"]
        assert item["properties"]["text"]["type"] == "string"
        assert item["properties"]["sources"]["type"] == "array"
        assert item["properties"]["sources"]["items"]["type"] == "integer"
        assert set(item["required"]) == {"text", "sources"}
        tags = REDUCE_SCHEMA["properties"]["tags"]
        assert tags["type"] == "array"
        assert tags["items"]["type"] == "string"

    def test_all_object_nodes_forbid_additional_properties(self):
        _walk_forbids_additional_properties(REDUCE_SCHEMA)


class TestParseReduce:
    def _map_notes(self):
        return [
            StudyNote(text="学び1", chunk_ids=("nb/doc#0",), section="§1", page=1),
            StudyNote(text="学び2", chunk_ids=("nb/doc#1",), section="§2", page=2),
        ]

    def test_resolves_source_number_to_chunk_ids_section_page(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "統合された学び", "sources": [1]}], "tags": []}'

        notes, tags = parse_reduce(text, map_notes)

        assert notes == [
            StudyNote(
                text="統合された学び", chunk_ids=("nb/doc#0",), section="§1", page=1
            )
        ]
        assert tags == []

    def test_multiple_sources_union_chunk_ids_in_order_deduped(self):
        map_notes = [
            StudyNote(text="学び1", chunk_ids=("nb/doc#0", "nb/doc#1"), section="§1", page=1),
            StudyNote(text="学び2", chunk_ids=("nb/doc#1", "nb/doc#2"), section="§2", page=2),
        ]
        text = '{"notes": [{"text": "統合された学び", "sources": [1, 2]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes[0].chunk_ids == ("nb/doc#0", "nb/doc#1", "nb/doc#2")

    def test_representative_section_page_come_from_first_referenced_source(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "統合された学び", "sources": [2, 1]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes[0].section == "§2"
        assert notes[0].page == 2

    def test_out_of_range_source_number_is_dropped_but_note_kept(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "統合された学び", "sources": [99, 1]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes[0].chunk_ids == ("nb/doc#0",)

    def test_note_kept_with_empty_chunk_ids_when_all_sources_invalid(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "統合された学び", "sources": [99]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes == [
            StudyNote(text="統合された学び", chunk_ids=(), section=None, page=None)
        ]

    def test_note_dropped_when_text_missing(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"sources": [1]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes == []

    def test_note_dropped_when_text_empty_after_strip(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "   ", "sources": [1]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes == []

    def test_degrades_gracefully_on_broken_json(self):
        map_notes = self._map_notes()
        text = "これはJSONではない壊れたテキスト {notes: 未閉じ"

        notes, tags = parse_reduce(text, map_notes)

        assert notes == []
        assert tags == []

    def test_returns_empty_when_notes_and_tags_missing(self):
        map_notes = self._map_notes()

        notes, tags = parse_reduce("{}", map_notes)

        assert notes == []
        assert tags == []

    def test_clamps_to_max_notes(self):
        map_notes = self._map_notes()
        notes_payload = [{"text": f"学び{i}", "sources": [1]} for i in range(7)]
        text = json.dumps({"notes": notes_payload, "tags": []})

        notes, _tags = parse_reduce(text, map_notes, max_notes=3)

        assert len(notes) == 3
        assert [n.text for n in notes] == ["学び0", "学び1", "学び2"]

    def test_tags_are_normalized(self):
        map_notes = self._map_notes()
        text = json.dumps(
            {"notes": [], "tags": ["量子力学", "  Quantum  Mechanics  ", ""]}
        )

        _notes, tags = parse_reduce(text, map_notes)

        assert tags == ["量子力学", "quantum-mechanics"]

    def test_strips_surrounding_whitespace_from_text(self):
        map_notes = self._map_notes()
        text = '{"notes": [{"text": "  統合された学び  ", "sources": [1]}], "tags": []}'

        notes, _tags = parse_reduce(text, map_notes)

        assert notes[0].text == "統合された学び"


class TestNormalizeTag:
    def test_lowercases_ascii(self):
        assert normalize_tag("Quantum") == "quantum"

    def test_fullwidth_to_halfwidth_via_nfkc(self):
        # "ＱＭ"(全角) は NFKC 正規化で "QM"(半角) になり、さらに lower される。
        assert normalize_tag("ＱＭ") == "qm"

    def test_replaces_internal_whitespace_with_hyphen(self):
        assert normalize_tag("quantum mechanics") == "quantum-mechanics"

    def test_collapses_consecutive_whitespace_to_single_hyphen(self):
        assert normalize_tag("quantum   mechanics") == "quantum-mechanics"

    def test_strips_surrounding_whitespace_before_hyphenation(self):
        assert normalize_tag("  quantum mechanics  ") == "quantum-mechanics"

    def test_japanese_tag_passes_through_unchanged(self):
        assert normalize_tag("量子力学") == "量子力学"

    def test_returns_none_for_empty_string(self):
        assert normalize_tag("") is None

    def test_returns_none_for_whitespace_only_string(self):
        assert normalize_tag("   ") is None

    def test_returns_none_for_non_string(self):
        assert normalize_tag(123) is None

    def test_allows_exactly_30_chars(self):
        tag = "あ" * 30
        assert normalize_tag(tag) == tag

    def test_returns_none_for_31_chars(self):
        assert normalize_tag("あ" * 31) is None

    def test_strips_quotes_colons_and_brackets(self):
        # コードレビュー指摘#8: LLM生成タグは司書ルーティングのカタログ経由で
        # プロンプトに混入するため、記号を落として間接プロンプト注入を緩和する。
        assert normalize_tag('quantum`: [inject]"') == "quantum-inject"

    def test_returns_none_when_only_symbols_remain(self):
        assert normalize_tag("!!!") is None

    def test_preserves_japanese_while_stripping_surrounding_brackets(self):
        assert normalize_tag("「量子力学」") == "量子力学"


class TestNormalizeTags:
    def test_drops_none_results(self):
        assert normalize_tags(["量子力学", "", "   ", 123]) == ["量子力学"]

    def test_dedupes_preserving_first_occurrence_order(self):
        result = normalize_tags(["Quantum", "物理", "quantum", "物理"])

        assert result == ["quantum", "物理"]

    def test_clamps_to_default_max_tags(self):
        tags = [f"tag{i}" for i in range(10)]

        result = normalize_tags(tags)

        assert len(result) == 8
        assert result == [f"tag{i}" for i in range(8)]

    def test_clamps_to_custom_max_tags(self):
        tags = ["a", "b", "c", "d"]

        result = normalize_tags(tags, max_tags=2)

        assert result == ["a", "b"]

    def test_empty_input_returns_empty_list(self):
        assert normalize_tags([]) == []


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
