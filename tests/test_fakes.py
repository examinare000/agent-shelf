"""tests/fakes.py 自体の単体テスト。

これらのダブルは他の単体テスト（service/search 等）の土台になるため、
「決定論的である」「呼び出し履歴を正しく記録する」ことをここで固定しておく。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from shelf.convert import ConvertResult
from shelf.ports import RawAnswer
from tests.fakes import FakeAnswerBackend, FakeConverter, FakeEmbedder


class TestFakeEmbedder:
    def test_default_dim_is_eight(self):
        embedder = FakeEmbedder()

        assert embedder.dim == 8

    def test_dim_is_configurable(self):
        embedder = FakeEmbedder(dim=16)

        vec = embedder.embed_query("hello")

        assert vec.shape == (16,)

    def test_embed_query_is_deterministic_for_same_text(self):
        embedder = FakeEmbedder()

        first = embedder.embed_query("同じ文章")
        second = embedder.embed_query("同じ文章")

        np.testing.assert_array_equal(first, second)

    def test_embed_query_differs_for_different_text(self):
        embedder = FakeEmbedder()

        a = embedder.embed_query("文章A")
        b = embedder.embed_query("文章B")

        assert not np.array_equal(a, b)

    def test_known_text_maps_to_normalized_given_vector(self):
        embedder = FakeEmbedder(known={"猫": [3.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})

        vec = embedder.embed_query("猫")

        np.testing.assert_allclose(vec, [0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], atol=1e-6)

    def test_embed_documents_returns_matrix_of_unit_vectors(self):
        embedder = FakeEmbedder()

        matrix = embedder.embed_documents(["文章A", "文章B"])

        assert matrix.shape == (2, 8)
        norms = np.linalg.norm(matrix, axis=1)
        np.testing.assert_allclose(norms, [1.0, 1.0], atol=1e-6)


class TestFakeAnswerBackend:
    def test_name_is_fake(self):
        backend = FakeAnswerBackend()

        assert backend.name == "fake"

    def test_returns_canned_raw_answer(self):
        canned = RawAnswer(text="回答本文", ok=True, error=None)
        backend = FakeAnswerBackend(canned)

        result = backend.answer("prompt", Path("/tmp/workdir"), None)

        assert result == canned

    def test_canned_string_is_wrapped_into_ok_raw_answer(self):
        backend = FakeAnswerBackend("回答本文")

        result = backend.answer("prompt", Path("/tmp/workdir"), None)

        assert result == RawAnswer(text="回答本文", ok=True, error=None)

    def test_records_call_history(self):
        backend = FakeAnswerBackend("回答")
        workdir = Path("/tmp/workdir")
        schema = {"type": "object"}

        backend.answer("質問プロンプト", workdir, schema)

        assert backend.calls == [
            {"prompt": "質問プロンプト", "workdir": workdir, "schema": schema}
        ]

    def test_returns_canned_list_in_order_across_multiple_calls(self):
        first = RawAnswer(text="1回目", ok=True, error=None)
        second = RawAnswer(text="2回目", ok=True, error=None)
        backend = FakeAnswerBackend([first, second])

        result1 = backend.answer("p1", Path("/tmp"), None)
        result2 = backend.answer("p2", Path("/tmp"), None)

        assert result1 == first
        assert result2 == second
        assert len(backend.calls) == 2


class TestFakeConverter:
    def test_convert_file_returns_canned_convert_result(self):
        converter = FakeConverter(markdown="# タイトル\n本文", converter="raw", title="タイトル")

        result = converter.convert_file(Path("dummy.txt"))

        assert result == ConvertResult(markdown="# タイトル\n本文", converter="raw", title="タイトル")

    def test_convert_url_returns_canned_convert_result(self):
        converter = FakeConverter(markdown="# タイトル\n本文")

        result = converter.convert_url("https://example.com/doc")

        assert result == ConvertResult(markdown="# タイトル\n本文", converter="raw", title=None)

    def test_records_call_history_separately_for_file_and_url(self):
        converter = FakeConverter(markdown="本文")
        path = Path("dummy.pdf")

        converter.convert_file(path)
        converter.convert_url("https://example.com/a")

        assert converter.file_calls == [path]
        assert converter.url_calls == ["https://example.com/a"]
