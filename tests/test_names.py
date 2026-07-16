"""names.py: notebook 名検証と doc_id 生成の純粋関数群のテスト。

意図: MCP 境界に渡る前の入力（notebook 名・ファイル名由来の doc_id）を
純粋関数として切り出し、副作用なしに仕様を固定する（docs/design-shelf-mcp.md §5）。
"""
from __future__ import annotations

import pytest

from shelf.names import (
    assign_unique_name,
    doc_id_for,
    normalize_notebook_name,
    validate_notebook_name,
)


class TestValidateNotebookName:
    def test_accepts_lowercase_alnum_name(self):
        assert validate_notebook_name("physics-papers") == "physics-papers"

    def test_accepts_underscore_and_digits(self):
        assert validate_notebook_name("notebook_01") == "notebook_01"

    def test_accepts_single_character_name(self):
        assert validate_notebook_name("a") == "a"

    def test_accepts_max_length_64_name(self):
        name = "a" * 64
        assert validate_notebook_name(name) == name

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            validate_notebook_name("")

    def test_rejects_name_longer_than_64(self):
        with pytest.raises(ValueError):
            validate_notebook_name("a" * 65)

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            validate_notebook_name("Physics")

    def test_rejects_symbols(self):
        with pytest.raises(ValueError):
            validate_notebook_name("foo!bar")

    def test_rejects_slash_as_path_separator(self):
        with pytest.raises(ValueError):
            validate_notebook_name("foo/bar")

    def test_rejects_dot_dot_path_traversal(self):
        with pytest.raises(ValueError):
            validate_notebook_name("../etc")

    def test_rejects_japanese_characters(self):
        with pytest.raises(ValueError):
            validate_notebook_name("物理学")

    def test_error_message_includes_input_value(self):
        with pytest.raises(ValueError, match="Foo"):
            validate_notebook_name("Foo")

    def test_error_message_truncates_long_input_to_64_chars(self):
        # 意図: 不正な超長文字列がそのままエラーメッセージ/ログに流れ込むと
        # メッセージ肥大やログ汚染につながるため、境界で64字に切り詰める。
        too_long = "A" * 100
        with pytest.raises(ValueError) as excinfo:
            validate_notebook_name(too_long)
        assert too_long[:64] in str(excinfo.value)
        assert too_long not in str(excinfo.value)


class TestDocIdFor:
    def test_is_deterministic_for_same_inputs(self):
        first = doc_id_for("feynman-lectures.pdf", "feynman-lectures")
        second = doc_id_for("feynman-lectures.pdf", "feynman-lectures")
        assert first == second

    def test_format_is_slug_hyphen_sha8(self):
        doc_id = doc_id_for("feynman-lectures.pdf", "feynman-lectures")
        slug, sha8 = doc_id.rsplit("-", 1)
        assert slug == "feynman-lectures"
        assert len(sha8) == 8
        assert all(c in "0123456789abcdef" for c in sha8)

    def test_sha8_matches_sha256_of_origin(self):
        import hashlib

        origin = "feynman-lectures.pdf"
        expected_sha8 = hashlib.sha256(origin.encode()).hexdigest()[:8]
        doc_id = doc_id_for(origin, "feynman-lectures")
        assert doc_id.endswith(expected_sha8)

    def test_different_origin_changes_suffix_even_with_same_stem(self):
        first = doc_id_for("a/feynman-lectures.pdf", "feynman-lectures")
        second = doc_id_for("b/feynman-lectures.pdf", "feynman-lectures")
        assert first != second

    def test_slug_lowercases_stem(self):
        doc_id = doc_id_for("origin.pdf", "MyBook")
        assert doc_id.startswith("mybook-")

    def test_slug_compresses_non_alnum_runs_to_single_hyphen(self):
        doc_id = doc_id_for("origin.pdf", "My  Book!!Title")
        slug = doc_id.rsplit("-", 1)[0]
        assert slug == "my-book-title"

    def test_slug_strips_leading_and_trailing_hyphens(self):
        doc_id = doc_id_for("origin.pdf", "--already-hyphenated--")
        slug = doc_id.rsplit("-", 1)[0]
        assert slug == "already-hyphenated"

    def test_slug_falls_back_to_doc_when_empty_after_stripping(self):
        doc_id = doc_id_for("origin.pdf", "物理学")
        slug = doc_id.rsplit("-", 1)[0]
        assert slug == "doc"

    def test_slug_is_truncated_to_max_40_chars(self):
        stem = "a" * 60
        doc_id = doc_id_for("origin.pdf", stem)
        slug = doc_id.rsplit("-", 1)[0]
        assert len(slug) == 40
        assert slug == "a" * 40


class TestNormalizeNotebookName:
    """docs/design-shelf-reference-service.md §13.5: LLM 提案名の決定的正規化。

    構成上必ず validate_notebook_name を通る値を返すことが仕様の核心
    （§13.10 V2 の検証ステップ）。
    """

    def test_returns_already_valid_name_unchanged(self):
        assert normalize_notebook_name("physics-papers") == "physics-papers"

    def test_lowercases_input(self):
        assert normalize_notebook_name("Physics") == "physics"

    def test_compresses_invalid_char_runs_to_single_hyphen(self):
        assert normalize_notebook_name("cooking recipes!!") == "cooking-recipes"

    def test_preserves_underscore(self):
        assert normalize_notebook_name("notebook_01") == "notebook_01"

    def test_strips_leading_and_trailing_hyphen_after_compression(self):
        assert normalize_notebook_name("  quantum mechanics  ") == "quantum-mechanics"

    def test_falls_back_to_notebook_when_empty_after_normalization(self):
        assert normalize_notebook_name("物理学") == "notebook"

    def test_falls_back_to_notebook_when_only_symbols(self):
        assert normalize_notebook_name("!!!") == "notebook"

    def test_truncates_to_max_64_chars(self):
        raw = "a" * 100
        result = normalize_notebook_name(raw)
        assert len(result) == 64
        assert result == "a" * 64

    def test_truncation_does_not_leave_trailing_hyphen(self):
        # 63文字の英数字 + 1文字分の不正文字("!")が64字目の境界に来ると、
        # 圧縮後のハイフンが切り詰め境界に落ちて末尾ハイフンになり得る。
        raw = "a" * 63 + "!" * 5
        result = normalize_notebook_name(raw)
        assert not result.endswith("-")

    def test_is_deterministic_for_same_input(self):
        raw = "Cooking Recipes!!"
        assert normalize_notebook_name(raw) == normalize_notebook_name(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "physics-papers",
            "Physics",
            "cooking recipes!!",
            "notebook_01",
            "  quantum mechanics  ",
            "物理学",
            "!!!",
            "a" * 100,
            "a" * 63 + "!" * 5,
            "",
        ],
    )
    def test_output_always_passes_validate_notebook_name(self, raw):
        # normalize_notebook_name は構成上 validate_notebook_name を必ず通る
        # 値を返す（§13.5・§13.10 V2 検証ステップ）。
        normalized = normalize_notebook_name(raw)
        assert validate_notebook_name(normalized) == normalized


class TestAssignUniqueName:
    """docs/design-shelf-reference-service.md §13.5: 既存名衝突時の決定的連番付与。"""

    def test_returns_base_when_no_collision(self):
        assert assign_unique_name("physics", {"cooking", "math"}) == "physics"

    def test_returns_base_2_on_single_collision(self):
        assert assign_unique_name("physics", {"physics"}) == "physics-2"

    def test_returns_base_3_when_base_and_base_2_taken(self):
        taken = {"physics", "physics-2"}
        assert assign_unique_name("physics", taken) == "physics-3"

    def test_skips_further_to_first_free_slot(self):
        taken = {"physics", "physics-2", "physics-3", "physics-4"}
        assert assign_unique_name("physics", taken) == "physics-5"

    def test_truncates_base_to_stay_within_64_chars_with_suffix(self):
        base = "a" * 64
        result = assign_unique_name(base, {base})
        assert len(result) == 64
        assert result == "a" * 62 + "-2"

    def test_result_stays_within_64_chars_for_double_digit_suffix(self):
        base = "a" * 64
        taken = {base} | {f"{'a' * 62}-{n}" for n in range(2, 10)}
        result = assign_unique_name(base, taken)
        assert len(result) <= 64
        assert result not in taken

    def test_accepts_list_as_taken(self):
        assert assign_unique_name("physics", ["physics"]) == "physics-2"

    def test_is_deterministic_for_same_input(self):
        taken = {"physics"}
        assert assign_unique_name("physics", taken) == assign_unique_name("physics", taken)

    def test_result_passes_validate_notebook_name(self):
        base = "a" * 64
        result = assign_unique_name(base, {base})
        assert validate_notebook_name(result) == result
