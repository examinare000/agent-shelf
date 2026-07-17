"""ShelfService の単体テスト。

Store(":memory:") + FakeEmbedder + FakeAnswerBackend + tmp_path 上の corpus で、
実DB・実埋め込みモデル・実サブスクCLIに触れずに ask/list_notebooks/create_notebook/
add_source/index の経路を検証する。add_source では convert.py の実変換器を使わず、
convert_file/convert_url を持つ最小限のローカル fake（_FakeConverter）を使う。
tests/fakes.py の共有 FakeConverter は convert() 単一メソッドのみを持ち、
service.py が要求する convert_file/convert_url の2メソッド構成とは形が異なるため
ここでは共用せず、本ファイル内に専用の fake を定義する。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import shelf.service
from shelf.convert import ConversionError, ConvertResult
from shelf.digests import MAP_SCHEMA, REDUCE_SCHEMA
from shelf.indexer import index_notebook
from shelf.ports import RawAnswer, RouteTarget
from shelf.prompts import ANSWER_SCHEMA, SUMMARY_SCHEMA
from shelf.service import ShelfService
from shelf.store import Store, UnknownNotebookError
from tests.fakes import FakeAnswerBackend, FakeEmbedder, FakeLibrarian


class _FakeConverter:
    """convert_file/convert_url を持つ決定論的ダブル。呼び出し引数を記録する。"""

    def __init__(
        self,
        markdown: str = "",
        converter: str = "raw",
        title: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> None:
        self._result = ConvertResult(markdown=markdown, converter=converter, title=title, notes=notes)
        self.file_calls: list[Path] = []
        self.url_calls: list[str] = []

    def convert_file(self, path: Path) -> ConvertResult:
        self.file_calls.append(path)
        return self._result

    def convert_url(self, url: str) -> ConvertResult:
        self.url_calls.append(url)
        return self._result


class _FailingConverter:
    """convert_file/convert_url が常に ConversionError を送出するダブル。"""

    def __init__(self, message: str) -> None:
        self._message = message

    def convert_file(self, path: Path) -> ConvertResult:
        raise ConversionError(self._message)

    def convert_url(self, url: str) -> ConvertResult:
        raise ConversionError(self._message)


class _SelectivelyFailingConverter:
    """ファイル名が fail_names に含まれる時だけ ConversionError を送出するダブル。

    add_directory の「変換失敗しても継続する」挙動を検証するため、複数ファイル中の
    一部だけを選択的に失敗させる必要があり、_FailingConverter(常に失敗)では表現できない。
    """

    def __init__(self, fail_names: set[str], markdown: str = "# Doc\n\n" + "content " * 20) -> None:
        self._fail_names = fail_names
        self._result = ConvertResult(markdown=markdown, converter="raw", title=None)
        self.file_calls: list[Path] = []

    def convert_file(self, path: Path) -> ConvertResult:
        self.file_calls.append(path)
        if path.name in self._fail_names:
            raise ConversionError(f"変換に失敗しました: {path.name}")
        return self._result

    def convert_url(self, url: str) -> ConvertResult:
        raise NotImplementedError


class _PermissionErrorConverter:
    """ファイル名が perm_error_names に含まれる時だけ PermissionError を送出するダブル。

    ファイル読み取り不可（権限不足等による OSError）で継続する挙動を検証するため、
    複数ファイル中の一部だけを選択的に PermissionError で失敗させる。
    """

    def __init__(self, perm_error_names: set[str], markdown: str = "# Doc\n\n" + "content " * 20) -> None:
        self._perm_error_names = perm_error_names
        self._result = ConvertResult(markdown=markdown, converter="raw", title=None)
        self.file_calls: list[Path] = []

    def convert_file(self, path: Path) -> ConvertResult:
        self.file_calls.append(path)
        if path.name in self._perm_error_names:
            raise PermissionError(f"Permission denied: {path.name}")
        return self._result

    def convert_url(self, url: str) -> ConvertResult:
        raise NotImplementedError


# FakeEmbedder の既知ベクトル: query と特定チャンク本文を同一ベクトルに固定することで、
# cosine_topk の順位・スコアを決定論的に制御する（実モデルなしで grounding 判定を検証するため）。
_KNOWN_VEC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
_CHUNK_TEXT = "distinctive chunk about whales"
_QUERY_TEXT = "what do whales eat?"


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})


def _seed_notebook(
    store: Store,
    embedder: FakeEmbedder,
    corpus_dir: Path,
    notebook: str = "nb",
    backend: str = "codex",
    text: str = _CHUNK_TEXT,
) -> None:
    store.create_notebook(notebook, backend=backend)
    nb_dir = corpus_dir / notebook
    nb_dir.mkdir(parents=True, exist_ok=True)
    (nb_dir / "doc.md").write_text(f"# Doc\n\n{text}\n", encoding="utf-8")
    index_notebook(corpus_dir, notebook, store, embedder)


def _grounded_raw_answer(citation_ids: list[int], confident: bool = True) -> RawAnswer:
    payload = {
        "answer": "whales eat krill [S1]",
        "citations": [{"s": s} for s in citation_ids],
        "confident": confident,
    }
    return RawAnswer(text=json.dumps(payload), ok=True, error=None)


# -- ask: 未知 notebook ------------------------------------------------------


def test_ask_unknown_notebook_returns_safe_error_without_calling_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("physics", backend="codex")
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("chemistry", "何か質問")

    assert result == {"error": "unknown notebook: chemistry. available: ['physics']"}
    assert backend.calls == []


# -- ask: 空 notebook ---------------------------------------------------------


def test_ask_empty_notebook_returns_warning_without_calling_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("empty", backend="codex")
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("empty", "anything?")

    assert result == {
        "notebook": "empty",
        "backend": "codex",
        "grounded": False,
        "answer": "",
        "citations": [],
        "insights": [],
        "warning": "notebook has no indexed sources",
    }
    assert backend.calls == []


# -- ask: 正常系（プロンプト構成・backend呼び出し） ---------------------------


def test_ask_builds_prompt_with_chunk_and_passes_it_to_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1]))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    service.ask("nb", _QUERY_TEXT)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert "[S1]" in call["prompt"]
    assert _CHUNK_TEXT in call["prompt"]
    assert call["workdir"] == tmp_path / "nb"
    assert call["schema"] == ANSWER_SCHEMA


# -- ask: grounded=True 判定 ---------------------------------------------------


def test_ask_returns_grounded_true_when_confident_and_citation_in_range(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1]))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("nb", _QUERY_TEXT)

    assert result["grounded"] is True
    assert result["backend"] == "codex"
    assert result["warning"] is None
    assert result["citations"] == [
        {
            "n": 1,
            "chunk_id": "nb/doc#0",
            "source": "nb/doc.md",
            "section": "Doc",
            "page": None,
            "quote": _CHUNK_TEXT,
        }
    ]


# -- ask: 範囲外citation除去とgrounded=False ------------------------------------


def test_ask_drops_out_of_range_citation_and_marks_ungrounded(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    # チャンクは1件のみ投入したので s=5 は範囲外。
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1, 5]))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("nb", _QUERY_TEXT)

    assert result["grounded"] is False
    assert len(result["citations"]) == 1
    assert result["citations"][0]["n"] == 1


# -- ask: confident=False で grounded=False -----------------------------------


def test_ask_confident_false_forces_ungrounded_even_with_valid_citation(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1], confident=False))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("nb", _QUERY_TEXT)

    assert result["grounded"] is False
    assert len(result["citations"]) == 1  # citations 自体は範囲内なので残る


# -- ask: パース失敗の劣化返却 --------------------------------------------------


def test_ask_falls_back_to_raw_text_when_engine_output_is_not_json(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(canned=RawAnswer(text="not json at all", ok=True, error=None))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("nb", _QUERY_TEXT)

    assert result == {
        "notebook": "nb",
        "backend": "codex",
        "grounded": False,
        "answer": "not json at all",
        "citations": [],
        "insights": [],
        "warning": "engine output was not valid JSON",
    }


# -- ask: RawAnswer.ok=False のエラー整形 ---------------------------------------


def test_ask_returns_safe_error_when_backend_fails(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(
        canned=RawAnswer(text="", ok=False, error="codex timed out after 300s")
    )
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("nb", _QUERY_TEXT)

    assert result == {
        "error": "backend failed: codex timed out after 300s",
        "notebook": "nb",
    }


# -- ask: notebook毎backend切替 -------------------------------------------------


def test_ask_resolves_backend_per_notebook_via_backend_factory(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="nb_codex", backend="codex")
    _seed_notebook(store, embedder, tmp_path, notebook="nb_gemini", backend="gemini_cli")

    requested_names: list[str] = []

    def factory(name: str) -> FakeAnswerBackend:
        requested_names.append(name)
        return FakeAnswerBackend(canned=_grounded_raw_answer([1]))

    service = ShelfService(store, embedder, factory, tmp_path)

    service.ask("nb_codex", _QUERY_TEXT)
    service.ask("nb_gemini", _QUERY_TEXT)

    assert requested_names == ["codex", "gemini_cli"]


# -- ask: 重複 (source, page) citation の除去 ------------------------------------


def test_ask_deduplicates_citations_with_same_source_and_page(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb_dup", backend="codex")
    # インデクサを経由せず、同一 source_path/page を持つ2チャンクを直接注入する。
    store.upsert_chunks(
        [
            {
                "id": "nb_dup/doc#0", "notebook": "nb_dup", "doc_id": "doc",
                "source_path": "nb_dup/doc.md", "section": None, "page": 1,
                "seq": 0, "text": "alpha content", "embedding": _KNOWN_VEC,
            },
            {
                "id": "nb_dup/doc#1", "notebook": "nb_dup", "doc_id": "doc",
                "source_path": "nb_dup/doc.md", "section": None, "page": 1,
                "seq": 1, "text": "beta content", "embedding": _KNOWN_VEC,
            },
        ]
    )
    local_embedder = FakeEmbedder(dim=8, known={_QUERY_TEXT: _KNOWN_VEC})
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1, 2]))
    service = ShelfService(store, local_embedder, lambda name: backend, tmp_path, top_k=10)

    result = service.ask("nb_dup", _QUERY_TEXT)

    assert len(result["citations"]) == 1
    assert result["grounded"] is True


# -- ask: quote が200字で切詰 ----------------------------------------------------


def test_ask_truncates_citation_quote_to_200_chars(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    long_text = "A" * 250
    store.create_notebook("nb_long", backend="codex")
    store.upsert_chunks(
        [
            {
                "id": "nb_long/doc#0", "notebook": "nb_long", "doc_id": "doc",
                "source_path": "nb_long/doc.md", "section": None, "page": None,
                "seq": 0, "text": long_text, "embedding": _KNOWN_VEC,
            }
        ]
    )
    local_embedder = FakeEmbedder(dim=8, known={_QUERY_TEXT: _KNOWN_VEC})
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1]))
    service = ShelfService(store, local_embedder, lambda name: backend, tmp_path)

    result = service.ask("nb_long", _QUERY_TEXT)

    assert len(result["citations"][0]["quote"]) == 200
    assert result["citations"][0]["quote"] == long_text[:200]


# -- list_notebooks -----------------------------------------------------------


def test_list_notebooks_reports_backend_sources_and_chunks(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="physics", backend="codex")
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    result = service.list_notebooks()

    assert result == [
        {
            "notebook": "physics",
            "description": None,
            "backend": "codex",
            "sources": 0,  # store.upsert_document を経由していないので documents は0件
            "chunks": 1,
        }
    ]


# -- create_notebook ------------------------------------------------------------


def test_create_notebook_rejects_invalid_name(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    with pytest.raises(ValueError):
        service.create_notebook("Invalid Name!")

    assert store.get_notebook("Invalid Name!") is None


def test_create_notebook_rejects_unknown_backend_before_registering(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    def factory(name: str) -> FakeAnswerBackend:
        if name != "codex":
            raise ValueError(f"unknown backend: {name}")
        return FakeAnswerBackend()

    service = ShelfService(store, embedder, factory, tmp_path)

    with pytest.raises(ValueError):
        service.create_notebook("nb", backend="ghost")

    assert store.get_notebook("nb") is None


def test_create_notebook_registers_with_validated_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path
    )

    service.create_notebook("nb", description="説明", backend="codex")

    row = store.get_notebook("nb")
    assert row is not None
    assert row["backend"] == "codex"
    assert row["description"] == "説明"


# -- add_source: ファイル投入からindexまで一気通貫 -------------------------------


def test_add_source_writes_corpus_registers_document_and_indexes(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    source_file = tmp_path / "source-file.txt"
    source_file.write_text("original file content, unused by the fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nfresh content about penguins.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), corpus_dir, converter=converter
    )

    result = service.add_source("nb", str(source_file), auto_summary=False)

    assert converter.file_calls == [Path(str(source_file))]
    assert converter.url_calls == []
    doc_id = result["doc_id"]
    assert result["chunks_written"] == 1
    assert (corpus_dir / "nb" / f"{doc_id}.md").read_text(encoding="utf-8") == (
        "# Doc\n\nfresh content about penguins.\n"
    )
    document = store.get_document(doc_id)
    assert document is not None
    assert document["origin"] == str(source_file)
    assert document["origin_type"] == "txt"
    assert document["converter"] == "raw"
    ids, matrix = store.load_vectors("nb")
    assert matrix.shape[0] == 1


def test_add_source_from_url_uses_convert_url_and_records_url_origin_type(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    converter = _FakeConverter(markdown="# Doc\n\nurl sourced markdown content.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", "https://example.com/article", auto_summary=False)

    assert converter.url_calls == ["https://example.com/article"]
    assert converter.file_calls == []
    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["origin_type"] == "url"
    assert document["fetched_at"] is not None


def test_add_source_returns_safe_error_on_conversion_failure(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    bad_file = tmp_path / "bad.pdf"
    bad_file.write_bytes(b"%PDF-1.4 dummy" + b"x" * 100)
    converter = _FailingConverter("テキストを抽出できませんでした（スキャン PDF の可能性があります）")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(bad_file), auto_summary=False)

    assert result == {
        "error": "テキストを抽出できませんでした（スキャン PDF の可能性があります）"
    }


# -- add_source: converter が返す notes(OCRスキップ等の通知)を返却JSONへ伝搬 ------------


def test_add_source_includes_notes_key_when_converter_reports_notes(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """convert.py 側でテキスト層検出により OCR をスキップした場合、その旨を
    利用者に明示するため notes をそのまま返却 JSON に伝える。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.pdf"
    source_file.write_bytes(b"%PDF-1.4 dummy" + b"x" * 100)
    converter = _FakeConverter(
        markdown="# Doc\n\ntext layer detected content.\n",
        notes=("既存のテキスト層を検出したため OCR をスキップしました",),
    )
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(source_file), auto_summary=False)

    assert result["notes"] == ["既存のテキスト層を検出したため OCR をスキップしました"]


def test_add_source_omits_notes_key_when_converter_reports_no_notes(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """notes が空の場合は JSON にノイズとなる空キーを付けない。"""
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("plain content " * 10, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nplain content.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(source_file), auto_summary=False)

    assert "notes" not in result


# -- add_source: notebook 名の未検証パストラバーサル対策（重大指摘#1） -----------------


def test_add_source_rejects_path_traversal_notebook_name_without_writing_files(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """notebook 名がバリデーション未通過のまま corpus_dir / notebook に使われると、
    "../../tmp/x" のような文字列で corpus 外への書き込みが成立してしまう(実再現済みの
    重大指摘)。validate_notebook_name を通門で必ず通すことで、変換すら試みずに
    安全な error dict を返し、corpus_dir の外は一切変更されないことを保証する。
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    source_file = tmp_path / "source.txt"
    source_file.write_text("harmless content " * 10, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nshould never be written.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), corpus_dir, converter=converter
    )

    result = service.add_source("../../tmp/x", str(source_file), auto_summary=False)

    assert "error" in result
    assert converter.file_calls == []
    # corpus_dir の外は一切作られていない(tmp_path 直下に新規ファイル/ディレクトリが
    # 増えていないことで、パストラバーサル書き込みがゼロ件であることを確認する)。
    assert set(tmp_path.iterdir()) == {corpus_dir, source_file}


def test_add_source_returns_error_dict_for_unknown_notebook_without_side_effects(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """既存実装は create_notebook でしか notebook 存在を検証しておらず、add_source は
    未知 notebook でも変換・書き込み・store 登録を進めてしまっていた(重大指摘#1)。
    存在チェックを入口に置き、副作用ゼロで安全な error dict を返すことを固定する。
    """
    source_file = tmp_path / "source.txt"
    source_file.write_text("harmless content " * 10, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nshould never be written.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("does-not-exist", str(source_file), auto_summary=False)

    assert result == {"error": "unknown notebook: does-not-exist. available: []"}
    assert converter.file_calls == []
    assert not (tmp_path / "does-not-exist").exists()


def test_ask_rejects_invalid_notebook_name_with_safe_error(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.ask("../../tmp/x", "何か質問")

    assert "error" in result
    assert backend.calls == []


def test_index_raises_value_error_for_invalid_notebook_name(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    with pytest.raises(ValueError):
        service.index("../../tmp/x")


def test_index_raises_unknown_notebook_error_for_unregistered_notebook(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    from shelf.store import UnknownNotebookError

    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    with pytest.raises(UnknownNotebookError):
        service.index("does-not-exist")


# -- add_source: mask を corpus md 保存前に適用（中位指摘#2） -----------------------


def test_add_source_applies_mask_before_writing_corpus_file(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """mask はチャンク時(chunker.chunk_markdown)だけに適用され、corpus に永続化する
    生 markdown はマスクされないまま書き込まれていた(中位指摘#2)。DEEP_DIVE 等で
    チャンク境界の外から md を直接参照されると、マスク境界がまるごと無効化される。
    """
    store.create_notebook("nb", backend="codex")
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdefghij"
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(
        markdown=f"# Doc\n\ntoken: {secret} and some more padding text.\n"
    )

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path,
        converter=converter, mask=fake_mask,
    )

    result = service.add_source("nb", str(source_file), auto_summary=False)

    written = (tmp_path / "nb" / f"{result['doc_id']}.md").read_text(encoding="utf-8")
    assert secret not in written
    assert "<REDACTED>" in written


# -- add_source: doc_id が notebook 依存になり、別 notebook への同一 origin 投入で
# documents 行が移動しない（中位指摘#3） -----------------------------------------


def test_add_source_same_origin_into_different_notebooks_creates_separate_documents(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """doc_id が origin+stem のみに依存していると、同一 origin を別 notebook に add した
    際に ON CONFLICT(id) DO UPDATE SET notebook=excluded.notebook が発火し、既存行の
    notebook が黙って書き換わってしまっていた(中位指摘#3)。doc_id に notebook を
    混ぜることで、2つの notebook それぞれに独立した document 行が残ることを確認する。
    """
    store.create_notebook("nb_a", backend="codex")
    store.create_notebook("nb_b", backend="codex")
    source_file = tmp_path / "shared.txt"
    source_file.write_text("shared file content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nshared markdown content here.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result_a = service.add_source("nb_a", str(source_file), auto_summary=False)
    result_b = service.add_source("nb_b", str(source_file), auto_summary=False)

    assert result_a["doc_id"] != result_b["doc_id"]
    doc_a = store.get_document(result_a["doc_id"])
    doc_b = store.get_document(result_b["doc_id"])
    assert doc_a is not None and doc_a["notebook"] == "nb_a"
    assert doc_b is not None and doc_b["notebook"] == "nb_b"


# -- add_source: 相対/絶対パスの表記揺れによる doc_id 分裂・二重登録の防止 -----------
# add_directory は Path(dir_path).resolve() してから走査するため origin は常に絶対
# パスで記録される。一方 add_source は origin 文字列を無加工で doc_id 採番・
# store 登録に使っていたため、同一物理ファイルでも相対パスで add した場合と
# 親ディレクトリ一括 add した場合とで doc_id が分裂し、documents に二重登録されていた。


def test_add_source_stores_absolute_origin_for_relative_path(
    store: Store, embedder: FakeEmbedder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_notebook("nb", backend="codex")
    (tmp_path / "doc.txt").write_text("relative path content " * 5, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nrelative path markdown.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )
    monkeypatch.chdir(tmp_path)

    result = service.add_source("nb", "./doc.txt", auto_summary=False)

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["origin"] == str((tmp_path / "doc.txt").resolve())


def test_add_source_relative_and_absolute_paths_converge_to_same_doc_id(
    store: Store, embedder: FakeEmbedder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_notebook("nb", backend="codex")
    (tmp_path / "doc.txt").write_text("shared content for convergence " * 5, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nshared markdown.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )
    monkeypatch.chdir(tmp_path)

    result_relative = service.add_source("nb", "./doc.txt", auto_summary=False)
    result_absolute = service.add_source("nb", str((tmp_path / "doc.txt").resolve()), auto_summary=False)

    assert result_relative["doc_id"] == result_absolute["doc_id"]
    assert len(store.list_documents("nb")) == 1


def test_add_source_url_origin_is_not_resolved(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """相対パス origin を resolve する変更が URL 分岐を巻き込んでいないことを固定する。"""
    store.create_notebook("nb", backend="codex")
    converter = _FakeConverter(markdown="# Doc\n\nurl markdown.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", "https://example.com/article", auto_summary=False)

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["origin"] == "https://example.com/article"


# -- add_source: ファイル系 origin のパス/サイズ検証（design doc §7、中位指摘#5） -------


def test_add_source_dispatches_empty_directory_and_returns_error_dict(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """ディレクトリ origin は add_directory へ委譲されるようになった(旧: 一律拒否)。
    空ディレクトリは added・errors とも0件になるため、add_directory 自身が
    error dict を返す(旧 _validate_file_origin の「ファイルではない」拒否とは別経路
    であることを、専用のエラーメッセージで区別する)。
    """
    store.create_notebook("nb", backend="codex")
    directory = tmp_path / "some_dir"
    directory.mkdir()
    converter = _FakeConverter(markdown="unused")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(directory), auto_summary=False)

    assert result == {"error": f"投入対象のファイルが見つかりませんでした: {directory}"}
    assert converter.file_calls == []


def test_add_source_rejects_symlink_to_directory_without_dispatching(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """symlink の指す先がディレクトリでも add_directory へはディスパッチせず、
    従来どおりシンボリックリンクとして拒否する(ディスパッチのすり抜け防止・回帰テスト)。
    """
    store.create_notebook("nb", backend="codex")
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    link = tmp_path / "link_dir"
    link.symlink_to(real_dir, target_is_directory=True)
    converter = _FakeConverter(markdown="unused")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(link), auto_summary=False)

    assert "error" in result
    assert converter.file_calls == []


def test_add_source_rejects_symlink_origin(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    real_file = tmp_path / "real.txt"
    real_file.write_text("real content " * 10, encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real_file)
    converter = _FakeConverter(markdown="unused")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(link), auto_summary=False)

    assert "error" in result
    assert converter.file_calls == []


# -- add_directory: ディレクトリ再帰投入（shelf add にディレクトリを渡した場合） -------


def test_add_directory_ingests_nested_files_recursively_with_absolute_origin(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "top.md").write_text("# Top\n\n" + "content " * 20, encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested content " * 20, encoding="utf-8")
    (root / "sub" / "deep.pdf").write_bytes(b"%PDF-1.4 dummy" + b"x" * 100)
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert result["notebook"] == "nb"
    assert result["skipped"] == []
    assert result["errors"] == []
    assert result["chunks_written"] > 0
    added_origins = {item["origin"] for item in result["added"]}
    assert added_origins == {
        str((root / "top.md").resolve()),
        str((root / "sub" / "nested.txt").resolve()),
        str((root / "sub" / "deep.pdf").resolve()),
    }
    assert all(Path(o).is_absolute() for o in added_origins)
    for item in result["added"]:
        document = store.get_document(item["doc_id"])
        assert document is not None
        assert document["origin"] == item["origin"]


def test_add_directory_attaches_notes_to_matching_added_entry(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """add_directory の added エントリ単位でも、add_source と同様に converter の
    notes（OCRスキップ通知等）が個別に伝わる。
    """
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "scanned.pdf").write_bytes(b"%PDF-1.4 dummy" + b"x" * 100)
    converter = _FakeConverter(
        markdown="# Doc\n\n" + "text layer content " * 10,
        notes=("既存のテキスト層を検出したため OCR をスキップしました",),
    )
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert len(result["added"]) == 1
    assert result["added"][0]["notes"] == ["既存のテキスト層を検出したため OCR をスキップしました"]


def test_add_directory_skips_unsupported_extension_without_calling_converter(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert len(result["added"]) == 1
    assert result["added"][0]["origin"] == str((root / "note.md").resolve())
    assert result["skipped"] == [
        {"origin": str((root / "image.png").resolve()), "reason": "未対応の形式です"}
    ]
    assert converter.file_calls == [Path(str((root / "note.md").resolve()))]


def test_add_directory_skips_symlinked_file_and_does_not_follow_symlinked_directory(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """rglob はディレクトリシンボリックリンクを辿らない(Python 3.11+)ため、tree外の
    実ディレクトリを指す symlink 配下のファイルは走査対象にすら現れない。symlink
    自体(ファイルへの symlink)は skipped に記録される。
    """
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    # symlink 以外に実ファイルを1件混ぜておく: added が空だと「投入対象なし」の
    # error dict 分岐に入ってしまい、skipped の中身を検証する前提が崩れるため。
    (root / "real.md").write_text("# Real\n\n" + "content " * 20, encoding="utf-8")
    real_target = tmp_path / "real_target.md"
    real_target.write_text("target content " * 20, encoding="utf-8")
    (root / "link.md").symlink_to(real_target)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("outside content " * 20, encoding="utf-8")
    (root / "linked_dir").symlink_to(outside_dir, target_is_directory=True)

    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert result["added"] == [
        {"doc_id": result["added"][0]["doc_id"], "origin": str((root / "real.md").resolve())}
    ]
    skipped_origins = {s["origin"] for s in result["skipped"]}
    assert str((root / "link.md")) in skipped_origins
    assert str(outside_dir / "secret.md") not in skipped_origins
    assert not any("secret.md" in o for o in skipped_origins)


def test_add_directory_ignores_hidden_files_and_directories(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config.md").write_text("# Config\n\n" + "content " * 20, encoding="utf-8")
    (root / ".hidden.md").write_text("# Hidden\n\n" + "content " * 20, encoding="utf-8")
    (root / "visible.md").write_text("# Visible\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert len(result["added"]) == 1
    assert result["added"][0]["origin"] == str((root / "visible.md").resolve())
    assert result["skipped"] == []
    assert result["errors"] == []


def test_add_directory_continues_after_conversion_failure_and_records_error(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "good.md").write_text("# Good\n\n" + "content " * 20, encoding="utf-8")
    (root / "bad.md").write_text("# Bad\n\n" + "content " * 20, encoding="utf-8")
    converter = _SelectivelyFailingConverter(fail_names={"bad.md"})
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert len(result["added"]) == 1
    assert result["added"][0]["origin"] == str((root / "good.md").resolve())
    assert result["errors"] == [
        {
            "origin": str((root / "bad.md").resolve()),
            "error": "変換に失敗しました: bad.md",
        }
    ]
    assert result["skipped"] == []
    assert result["chunks_written"] > 0


def test_add_directory_records_unreadable_file_and_continues(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """ファイル読み取り不可（権限不足等による PermissionError/OSError）時も、
    他ファイルの処理を継続し、そのファイルを安全なメッセージで errors に記録する
    （生トレースバック stdout に出さない）。
    """
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "readable.md").write_text("# Readable\n\n" + "content " * 20, encoding="utf-8")
    (root / "unreadable.md").write_text("# Unreadable\n\n" + "content " * 20, encoding="utf-8")

    converter = _PermissionErrorConverter(perm_error_names={"unreadable.md"})
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_directory("nb", str(root), auto_summary=False)

    # readable.md は added に入り、chunks_written > 0 を確認
    assert len(result["added"]) == 1
    assert result["added"][0]["origin"] == str((root / "readable.md").resolve())
    assert result["chunks_written"] > 0

    # unreadable.md は errors に固定メッセージで記録される
    assert len(result["errors"]) == 1
    assert result["errors"][0]["origin"] == str((root / "unreadable.md").resolve())
    assert result["errors"][0]["error"] == "ファイルを読み取れませんでした"

    # skipped は空（ファイル形式サポートチェックは通ったが、読み取り時に失敗したので）
    assert result["skipped"] == []


def test_add_directory_indexes_once_regardless_of_file_count(
    store: Store, embedder: FakeEmbedder, tmp_path: Path, monkeypatch
) -> None:
    """index_notebook はファイル数分ではなく1回だけ呼ばれる。embed 呼び出し回数は
    チャンク数に依存し索引呼び出し回数の代理にならないため、index_notebook 自体の
    呼び出し回数を monkeypatch で直接数える(計画で明記された理由)。
    """
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# A\n\n" + "content " * 20, encoding="utf-8")
    (root / "b.md").write_text("# B\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    call_count = 0
    real_index_notebook = shelf.service.index_notebook

    def counting(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_index_notebook(*args, **kwargs)

    monkeypatch.setattr(shelf.service, "index_notebook", counting)

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert call_count == 1
    assert result["chunks_written"] > 0


def test_add_directory_reruns_idempotently_without_duplicating_documents(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    first = service.add_directory("nb", str(root), auto_summary=False)
    second = service.add_directory("nb", str(root), auto_summary=False)

    assert first["added"][0]["doc_id"] == second["added"][0]["doc_id"]
    assert len(store.list_documents("nb")) == 1


# -- _persist_converted: 変換・要約済み markdown を直接永続化する経路
# （design doc §13.10 V4 extract-method・将来 shelve が変換・要約済み markdown を
# 渡して永続化だけ行うためのシグネチャであることの回帰ガード）。


def test_persist_converted_persists_already_converted_markdown_without_calling_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """_ingest_file を介さず _persist_converted を直接呼べること、かつ変換
    （convert_file/convert_url）・要約（backend 呼び出し）を一切行わず、渡された
    markdown/description をそのまま永続化するだけであることを確認する。converter
    に本物を渡さず backend を呼べば例外になる fake を渡すことで、内部で変換や
    要約が起きていないことを間接的に証明する。
    """
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"

    class _ExplodingConverter:
        def convert_file(self, path: Path) -> ConvertResult:
            raise AssertionError("_persist_converted は変換してはいけない")

        def convert_url(self, url: str) -> ConvertResult:
            raise AssertionError("_persist_converted は変換してはいけない")

    def _exploding_backend_factory(name: str) -> FakeAnswerBackend:
        raise AssertionError("_persist_converted は要約のため backend を呼んではいけない")

    service = ShelfService(
        store, embedder, _exploding_backend_factory, corpus_dir,
        converter=_ExplodingConverter(),
    )

    result = service._persist_converted(
        "nb",
        "https://example.com/already-converted",
        is_url=True,
        markdown="# Doc\n\nalready converted and summarized markdown.\n",
        title="Doc",
        converter="raw",
        conversion_notes=(),
        description="既に生成済みの要約",
        description_source="auto",
    )

    assert (corpus_dir / "nb" / f"{result.doc_id}.md").read_text(encoding="utf-8") == (
        "# Doc\n\nalready converted and summarized markdown.\n"
    )
    document = store.get_document(result.doc_id)
    assert document is not None
    assert document["origin"] == "https://example.com/already-converted"
    assert document["origin_type"] == "url"
    assert document["converter"] == "raw"
    assert document["description"] == "既に生成済みの要約"
    assert document["description_source"] == "auto"
    assert document["fetched_at"] is not None


# -- add_source/add_directory: description の自動生成(要約) ----------------------
# 優先順位: 明示 description > auto_summary による自動生成 > 生成失敗時は既存維持。


def test_add_source_with_explicit_desc_stores_masked_description_without_backend_call(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """明示 description が最優先。mask はチャンク本文と同様 description にも適用され、
    かつ明示済みなので backend(要約生成)は一切呼ばれない(auto_summary=True でも同様)。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    secret = "sk-ABCDEF1234567890"
    backend = FakeAnswerBackend()

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    service = ShelfService(
        store, embedder, lambda name: backend, tmp_path,
        converter=converter, mask=fake_mask,
    )

    result = service.add_source(
        "nb", str(source_file), description=f"token {secret} document", auto_summary=True
    )

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["description"] == "token <REDACTED> document"
    assert document["description_source"] == "user"
    assert backend.calls == []


def test_add_source_auto_generates_summary_via_notebook_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """description 未指定 + auto_summary=True(既定)なら notebook の backend で要約を
    生成し、SUMMARY_SCHEMA・corpus_dir/notebook の workdir・markdown を含むプロンプトで
    backend.answer が呼ばれる。
    """
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    markdown = "# ペンギン図鑑\n\nペンギンの生態について解説する資料です。\n"
    converter = _FakeConverter(markdown=markdown, title="ペンギン図鑑")
    backend = FakeAnswerBackend(canned='{"summary": "ペンギンの生態資料"}')
    captured_backend_names: list[str] = []

    def backend_factory(name: str):
        captured_backend_names.append(name)
        return backend

    service = ShelfService(store, embedder, backend_factory, corpus_dir, converter=converter)

    result = service.add_source("nb", str(source_file))

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["description"] == "ペンギンの生態資料"
    assert document["description_source"] == "auto"
    assert captured_backend_names == ["codex"]
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["schema"] == SUMMARY_SCHEMA
    assert call["workdir"] == corpus_dir / "nb"
    assert "ペンギンの生態について解説する資料です" in call["prompt"]


def test_add_source_masks_auto_generated_summary(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """auto_summary=True で backend が生成した summary にも、明示 description と
    同様に mask が適用される。backend の出力にシークレット様の文字列が紛れ込んでも、
    保存される description に生の値が残らないことを固定する（明示 desc 経路の
    mask 固定テストの auto 版）。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    secret = "sk-ABCDEF1234567890"
    backend = FakeAnswerBackend(canned=f'{{"summary": "token {secret} document"}}')

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    service = ShelfService(
        store, embedder, lambda name: backend, tmp_path,
        converter=converter, mask=fake_mask,
    )

    result = service.add_source("nb", str(source_file))

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["description"] == "token <REDACTED> document"
    assert document["description_source"] == "auto"
    assert secret not in document["description"]


def test_add_source_auto_summary_failure_keeps_add_successful_with_note(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """要約生成が失敗(ok=False)しても add 自体は成功として doc_id を返し、
    description は保存せず notes に失敗を明示する。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="timeout"))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)

    result = service.add_source("nb", str(source_file))

    assert "doc_id" in result
    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["description"] is None
    assert document["description_source"] is None
    assert result["notes"] == ["要約生成に失敗しました"]


def test_add_source_auto_summary_failure_preserves_existing_description(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """再add時に要約生成が失敗しても、既存の description/description_source が
    あれば上書きせず維持し、その旨を notes に積む。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )
    first = service.add_source("nb", str(source_file), description="手動の説明")
    assert "doc_id" in first

    failing_backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="timeout"))
    service_fail = ShelfService(
        store, embedder, lambda name: failing_backend, tmp_path, converter=converter
    )
    second = service_fail.add_source("nb", str(source_file))

    assert second["doc_id"] == first["doc_id"]
    document = store.get_document(second["doc_id"])
    assert document is not None
    assert document["description"] == "手動の説明"
    assert document["description_source"] == "user"
    assert second["notes"] == ["要約の再生成に失敗したため既存の説明を維持しました"]


def test_add_source_no_summary_skips_backend_and_preserves_existing_description(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """auto_summary=False での再addは既存の description を維持し、backend は
    一切呼ばれない(NULL 化して既存の要約を失わせない)。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    backend = FakeAnswerBackend(canned='{"summary": "初回の要約"}')
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)
    service.add_source("nb", str(source_file))
    assert len(backend.calls) == 1

    second = service.add_source("nb", str(source_file), auto_summary=False)

    assert len(backend.calls) == 1
    document = store.get_document(second["doc_id"])
    assert document is not None
    assert document["description"] == "初回の要約"
    assert document["description_source"] == "auto"


def test_add_source_blank_desc_is_treated_as_unspecified(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """strip 後に空文字となる description は「未指定」と同一視し、auto_summary=False
    と組み合わせれば description は NULL のまま、backend も呼ばれない。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nsome content about penguins.\n")
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)

    result = service.add_source("nb", str(source_file), description="   ", auto_summary=False)

    document = store.get_document(result["doc_id"])
    assert document is not None
    assert document["description"] is None
    assert document["description_source"] is None
    assert backend.calls == []


def test_add_source_rejects_desc_for_directory_origin(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """--desc はファイル単位の指定を意図しており、ディレクトリ投入(add_directory委譲)には
    使えない。converter を呼ぶ前(副作用ゼロ)に error dict で拒否する。
    """
    store.create_notebook("nb", backend="codex")
    directory = tmp_path / "some_dir"
    directory.mkdir()
    (directory / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="unused")
    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, converter=converter
    )

    result = service.add_source("nb", str(directory), description="説明")

    assert result == {
        "error": "--desc はディレクトリ投入では指定できません（ファイル単位で指定してください）"
    }
    assert converter.file_calls == []


def test_add_directory_auto_generates_summary_per_file(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """add_directory は auto_summary=True(既定)で各ファイルごとに要約生成する
    (1ファイルにつき backend.answer を1回呼ぶ)。
    """
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# A\n\n" + "content " * 20, encoding="utf-8")
    (root / "b.md").write_text("# B\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    backend = FakeAnswerBackend(canned='{"summary": "要約"}')
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)

    result = service.add_directory("nb", str(root))

    assert len(backend.calls) == 2
    assert len(result["added"]) == 2
    for item in result["added"]:
        document = store.get_document(item["doc_id"])
        assert document is not None
        assert document["description"] == "要約"
        assert document["description_source"] == "auto"


def test_add_directory_no_summary_skips_generation(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# A\n\n" + "content " * 20, encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\n" + "converted content " * 10)
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)

    result = service.add_directory("nb", str(root), auto_summary=False)

    assert backend.calls == []
    document = store.get_document(result["added"][0]["doc_id"])
    assert document is not None
    assert document["description"] is None


def test_add_source_summary_chunk_written_increments_chunks_written(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """description が保存されると、indexer が要約チャンクを自動的に索引化するため
    chunks_written は本文分(1)+要約分(1)=2になる(indexer 連携の一気通貫確認)。
    """
    store.create_notebook("nb", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("placeholder content, unused by fake converter", encoding="utf-8")
    converter = _FakeConverter(markdown="# Doc\n\nfresh content about penguins.\n")
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path, converter=converter)

    result = service.add_source(
        "nb", str(source_file), description="ペンギンの説明", auto_summary=False
    )

    assert backend.calls == []
    assert result["chunks_written"] == 2


# -- set_persona（設計書 §7-A） -------------------------------------------------


def test_set_persona_stores_masked_persona(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    secret = "sk-ABCDEF1234567890"

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    service = ShelfService(
        store, embedder, lambda name: FakeAnswerBackend(), tmp_path, mask=fake_mask
    )

    service.set_persona("nb", f"token {secret} expert")

    row = store.get_notebook("nb")
    assert row is not None
    assert row["persona"] == "token <REDACTED> expert"


def test_set_persona_rejects_invalid_notebook_name(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    with pytest.raises(ValueError):
        service.set_persona("../../tmp/x", "persona text")


def test_set_persona_raises_for_unknown_notebook(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    with pytest.raises(UnknownNotebookError):
        service.set_persona("does-not-exist", "persona text")


# -- ask: persona 注入（設計書 §7-A・互換保証） ---------------------------------


def test_ask_injects_persona_instruction_into_prompt_when_notebook_has_persona(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    store.set_persona("nb", "量子力学の専門家")
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1]))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    service.ask("nb", _QUERY_TEXT)

    assert "あなたは量子力学の専門家である。" in backend.calls[0]["prompt"]


def test_ask_without_persona_prompt_has_no_persona_instruction(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path)
    backend = FakeAnswerBackend(canned=_grounded_raw_answer([1]))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    service.ask("nb", _QUERY_TEXT)

    assert "あなたは" not in backend.calls[0]["prompt"]


# -- ask: insights（retrieved digest チャンクから構成・設計書 §5-C） -------------


def test_ask_returns_insights_built_from_retrieved_digest_chunks(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb_digest", backend="codex")
    store.upsert_chunks(
        [
            {
                "id": "nb_digest/doc#0", "notebook": "nb_digest", "doc_id": "doc",
                "source_path": "nb_digest/doc.md", "section": None, "page": None,
                "seq": 0, "text": _CHUNK_TEXT, "embedding": _KNOWN_VEC, "kind": "body",
            },
            {
                "id": "nb_digest/doc#-2", "notebook": "nb_digest", "doc_id": "doc",
                "source_path": "nb_digest/doc.md", "section": None, "page": None,
                "seq": -2, "text": "whales migrate long distances", "embedding": _KNOWN_VEC,
                "kind": "digest",
            },
        ]
    )
    local_embedder = FakeEmbedder(dim=8, known={_QUERY_TEXT: _KNOWN_VEC})
    payload = {
        "answer": "whales eat krill [S1] and migrate [L1]",
        "citations": [{"s": 1}],
        "insights": [{"l": 1}],
        "confident": True,
    }
    backend = FakeAnswerBackend(canned=RawAnswer(text=json.dumps(payload), ok=True, error=None))
    service = ShelfService(store, local_embedder, lambda name: backend, tmp_path)

    result = service.ask("nb_digest", _QUERY_TEXT)

    assert result["insights"] == [
        {
            "l": 1,
            "note_id": "nb_digest/doc#-2",
            "source": "nb_digest/doc.md",
            "text": "whales migrate long distances",
        }
    ]
    assert result["citations"] == [
        {
            "n": 1, "chunk_id": "nb_digest/doc#0", "source": "nb_digest/doc.md",
            "section": None, "page": None, "quote": _CHUNK_TEXT,
        }
    ]


# -- consult（設計書 §5-A・§6） -------------------------------------------------


def _routing_answer(targets: list[dict], answerable: bool = True) -> str:
    return json.dumps({"answerable": answerable, "targets": targets})


def test_consult_returns_answered_false_when_no_notebooks_exist(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    backend = FakeAnswerBackend()
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.consult("何か質問")

    assert result == {
        "question": "何か質問",
        "answered": False,
        "routed": [],
        "warning": "利用可能な notebook がありません",
    }
    assert backend.calls == []  # 司書すら呼ばない(カタログが空と分かった時点で短絡)


def test_consult_returns_answered_false_when_librarian_finds_no_targets(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="nb")
    backend = FakeAnswerBackend(canned=_routing_answer([], answerable=False))
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.consult("何か質問")

    assert result == {
        "question": "何か質問",
        "answered": False,
        "routed": [],
        "warning": "資料からは分からない",
    }
    assert len(backend.calls) == 1  # ルーティングのみ呼ばれ、専門家推論は呼ばれない


def test_consult_routes_to_single_expert_and_aggregates_answer(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="nb")
    store.set_persona("nb", "鯨の専門家")
    routing_json = _routing_answer(
        [{"notebook": "nb", "score": 0.9, "subquery": _QUERY_TEXT, "reason": "鯨の話題"}]
    )
    answer_json = _grounded_raw_answer([1]).text
    backend = FakeAnswerBackend(canned=[routing_json, answer_json])
    service = ShelfService(store, embedder, lambda name: backend, tmp_path)

    result = service.consult(_QUERY_TEXT)

    assert result["question"] == _QUERY_TEXT
    assert result["answered"] is True
    assert result["warning"] is None
    assert len(result["routed"]) == 1
    routed = result["routed"][0]
    assert routed["notebook"] == "nb"
    assert routed["reason"] == "鯨の話題"
    assert routed["subquery"] == _QUERY_TEXT
    assert routed["score"] == 0.9
    assert routed["backend"] == "codex"
    assert routed["persona"] == "鯨の専門家"
    assert routed["grounded"] is True
    assert routed["citations"][0]["chunk_id"] == "nb/doc#0"
    assert routed["insights"] == []
    assert len(backend.calls) == 2  # ルーティング1回 + 専門家1回


def test_consult_fans_out_to_multiple_experts_when_librarian_returns_multiple_targets(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="nb_a", text=_CHUNK_TEXT)
    _seed_notebook(store, embedder, tmp_path, notebook="nb_b", text=_CHUNK_TEXT)
    targets = [
        RouteTarget(notebook="nb_a", score=0.9, subquery=_QUERY_TEXT, reason="A"),
        RouteTarget(notebook="nb_b", score=0.8, subquery=_QUERY_TEXT, reason="B"),
    ]
    fake_librarian = FakeLibrarian(targets)
    answer_json = _grounded_raw_answer([1]).text
    backend = FakeAnswerBackend(canned=answer_json)
    service = ShelfService(
        store, embedder, lambda name: backend, tmp_path, librarian=fake_librarian
    )

    result = service.consult(_QUERY_TEXT)

    assert result["answered"] is True
    assert [r["notebook"] for r in result["routed"]] == ["nb_a", "nb_b"]
    assert len(backend.calls) == 2  # 司書は FakeLibrarian が代替、専門家2回のみ backend を呼ぶ
    assert len(fake_librarian.calls) == 1


def test_consult_degrades_gracefully_when_expert_backend_call_fails(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    _seed_notebook(store, embedder, tmp_path, notebook="nb")
    targets = [RouteTarget(notebook="nb", score=0.9, subquery=_QUERY_TEXT, reason="R")]
    fake_librarian = FakeLibrarian(targets)
    backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="timeout"))
    service = ShelfService(
        store, embedder, lambda name: backend, tmp_path, librarian=fake_librarian
    )

    result = service.consult(_QUERY_TEXT)

    assert result["answered"] is True  # ルーティング自体(=fake_librarian)は成功しているため
    routed = result["routed"][0]
    assert routed["grounded"] is False
    assert routed["answer"] == ""
    assert routed["citations"] == []
    assert routed["insights"] == []


# -- digest（学びノート生成・map-reduce パイプライン・設計書 §7-B） -------------


def _map_answer(notes: list[dict]) -> str:
    return json.dumps({"notes": notes})


def _reduce_answer(notes: list[dict], tags: list[str] | None = None) -> str:
    return json.dumps({"notes": notes, "tags": tags or []})


def test_digest_generates_and_stores_study_notes_then_reindexes(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
        title="鯨の資料",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "鯨は哺乳類である", "chunks": [1]}]),
            _reduce_answer([{"text": "鯨は哺乳類である", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert len(notes) == 1
    assert notes[0]["text"] == "鯨は哺乳類である"
    assert notes[0]["pipeline"] == 2
    # 生成後に再索引され、kind='digest' チャンクが検索対象になる
    ids, _ = store.load_vectors("nb")
    chunk_kinds = {store.get_chunk(i)["kind"] for i in ids}
    assert "digest" in chunk_kinds


def test_digest_writes_digest_chunks_when_run_after_prior_indexing(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """shelf add(内部で index 済み・file_state 確定)→ shelf digest の実運用順序の回帰テスト。

    先に index_notebook が走って file_state が mtime/size で確定していると、
    digest() 末尾の再索引呼び出しが indexer.py の mtime/size 一致 early-skip に
    吸われてしまい、study_notes を保存しても kind='digest' チャンクが chunks に
    一度も書かれない(Critical不具合)。既存の
    test_digest_generates_and_stores_study_notes_then_reindexes は事前に
    index_notebook を呼ばないためこの early-skip を踏まず、不具合を再現できない。
    """
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
        title="鯨の資料",
    )
    # shelf add 相当: 先に一度索引化して file_state (mtime/size) を確定させる。
    index_notebook(corpus_dir, "nb", store, embedder)

    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "鯨は哺乳類である", "chunks": [1]}]),
            _reduce_answer([{"text": "鯨は哺乳類である", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert len(notes) == 1
    ids, _ = store.load_vectors("nb")
    chunk_kinds = {store.get_chunk(i)["kind"] for i in ids}
    assert "digest" in chunk_kinds


def test_digest_uses_notebook_persona_and_document_title_in_map_and_reduce_prompts(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex", persona="鯨類学者")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
        title="鯨の資料",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "学び", "chunks": [1]}]),
            _reduce_answer([{"text": "学び", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    service.digest("nb")

    assert len(backend.calls) == 2
    map_call, reduce_call = backend.calls
    assert "あなたは鯨類学者である。" in map_call["prompt"]
    assert "鯨の資料" in map_call["prompt"]
    assert map_call["schema"] == MAP_SCHEMA
    assert map_call["workdir"] == corpus_dir / "nb"
    assert "あなたは鯨類学者である。" in reduce_call["prompt"]
    assert reduce_call["schema"] == REDUCE_SCHEMA
    assert reduce_call["workdir"] == corpus_dir / "nb"


def test_digest_multi_window_doc_merges_map_notes_via_reduce_with_chunk_id_union(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """節が変わると group_into_windows が新ウィンドウを開始する（digests.py §設計）ため、
    2節の doc は map が2回・reduce が1回呼ばれる。reduce の sources 参照は
    parse_reduce により両ウィンドウの chunk_ids 和集合へ解決される。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text(
        "# Doc\n\n## 鯨\n\nwhale content in section one.\n\n"
        "## アザラシ\n\nseal content in section two.\n",
        encoding="utf-8",
    )
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "鯨は哺乳類", "chunks": [1]}]),
            _map_answer([{"text": "アザラシは哺乳類", "chunks": [1]}]),
            _reduce_answer(
                [{"text": "海洋哺乳類の学び", "sources": [1, 2]}], tags=["海洋生物"]
            ),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    assert len(backend.calls) == 3
    notes = store.list_study_notes("nb", "doc")
    assert len(notes) == 1
    assert notes[0]["text"] == "海洋哺乳類の学び"
    assert notes[0]["pipeline"] == 2
    assert set(notes[0]["source_chunk_ids"]) == {"nb/doc#0", "nb/doc#1"}
    assert store.list_document_tags("nb", "doc") == ["海洋生物"]


def test_digest_single_window_failure_continues_to_other_windows_within_doc(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text(
        "# Doc\n\n## 鯨\n\nwhale content in section one.\n\n"
        "## アザラシ\n\nseal content in section two.\n",
        encoding="utf-8",
    )
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            RawAnswer(text="", ok=False, error="timeout"),
            _map_answer([{"text": "アザラシは哺乳類", "chunks": [1]}]),
            _reduce_answer([{"text": "アザラシは哺乳類", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert notes[0]["text"] == "アザラシは哺乳類"


def test_digest_all_windows_failing_records_doc_error_without_calling_reduce(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text(
        "# Doc\n\n## 鯨\n\nwhale content in section one.\n\n"
        "## アザラシ\n\nseal content in section two.\n",
        encoding="utf-8",
    )
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            RawAnswer(text="", ok=False, error="timeout"),
            RawAnswer(text="", ok=False, error="timeout"),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result["generated"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["doc_id"] == "doc"
    assert len(backend.calls) == 2  # map 2件のみ・reduce は一度も呼ばれない


def test_digest_all_windows_failing_includes_last_backend_error_text_in_doc_error(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """コードレビュー指摘#5: 全ウィンドウ全滅時の doc エラー文言は固定文言のみ
    だったが、可観測性のため最後に観測した backend の raw.error 文言を含める。
    戻り値スキーマ(generated/skipped/errorsキー)自体は変えない。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(canned=[RawAnswer(text="", ok=False, error="rate limited")])
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result["errors"][0]["doc_id"] == "doc"
    assert "rate limited" in result["errors"][0]["error"]


def test_digest_reduce_failure_falls_back_to_clamped_map_notes_with_empty_tags(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer(
                [
                    {"text": "1件目の学び", "chunks": [1]},
                    {"text": "2件目の学び", "chunks": [1]},
                ]
            ),
            RawAnswer(text="", ok=False, error="timeout"),
        ]
    )
    service = ShelfService(
        store, embedder, lambda name: backend, corpus_dir, digest_max_notes=1
    )

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert len(notes) == 1
    assert notes[0]["text"] == "1件目の学び"
    assert notes[0]["pipeline"] == 2
    assert store.list_document_tags("nb", "doc") == []


def test_digest_reduce_failure_logs_warning_with_last_backend_error_text(
    store: Store, embedder: FakeEmbedder, tmp_path: Path, caplog
) -> None:
    """コードレビュー指摘#5: reduce 失敗（劣化継続）時に、最後に観測した backend の
    raw.error 文言を含む warning ログを残す（observability。戻り値スキーマ・
    ログ以外の既存挙動は変えない）。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nwhale content here.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "1件目の学び", "chunks": [1]}]),
            RawAnswer(text="", ok=False, error="reduce timeout"),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    with caplog.at_level("WARNING"):
        service.digest("nb")

    assert any("reduce timeout" in record.message for record in caplog.records)


def test_digest_reduce_prompt_includes_existing_notebook_tag_catalog(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc1.md").write_text("# Doc1\n\ncontent one.\n", encoding="utf-8")
    (nb_dir / "doc2.md").write_text("# Doc2\n\ncontent two.\n", encoding="utf-8")
    store.upsert_document(
        id="doc1", notebook="nb", origin="doc1.md", origin_type="md",
        normalized_path="nb/doc1.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    store.upsert_document(
        id="doc2", notebook="nb", origin="doc2.md", origin_type="md",
        normalized_path="nb/doc2.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    store.replace_document_tags("nb", "doc1", ["既存タグ"])
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "学び", "chunks": [1]}]),
            _reduce_answer([{"text": "学び", "sources": [1]}], tags=["既存タグ"]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    service.digest("nb", doc_id="doc2")

    reduce_call = backend.calls[-1]
    assert "既存タグ" in reduce_call["prompt"]
    assert reduce_call["schema"] == REDUCE_SCHEMA


def test_digest_calls_list_tags_by_notebook_once_per_run_not_per_document(
    store: Store, embedder: FakeEmbedder, tmp_path: Path, monkeypatch
) -> None:
    """コードレビュー指摘#3: _digest_one が文書ごとに notebook 横断 GROUP BY の
    list_tags_by_notebook を呼んでいたN+1を解消し、digest() のループ前に1回だけ
    引く。2文書のnotebookでも呼び出しは1回に留まることを検証する。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc1.md").write_text("# Doc1\n\ncontent one.\n", encoding="utf-8")
    (nb_dir / "doc2.md").write_text("# Doc2\n\ncontent two.\n", encoding="utf-8")
    store.upsert_document(
        id="doc1", notebook="nb", origin="doc1.md", origin_type="md",
        normalized_path="nb/doc1.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    store.upsert_document(
        id="doc2", notebook="nb", origin="doc2.md", origin_type="md",
        normalized_path="nb/doc2.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "doc1の学び", "chunks": [1]}]),
            _reduce_answer([{"text": "doc1の学び", "sources": [1]}], tags=["タグ1"]),
            _map_answer([{"text": "doc2の学び", "chunks": [1]}]),
            _reduce_answer([{"text": "doc2の学び", "sources": [1]}], tags=["タグ1"]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)
    call_count = 0
    original = store.list_tags_by_notebook

    def counting(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "list_tags_by_notebook", counting)

    result = service.digest("nb")

    assert result["generated"] == ["doc1", "doc2"]
    assert call_count == 1


def test_digest_reduce_prompt_for_later_doc_includes_tags_saved_by_earlier_doc_in_same_run(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """list_tags_by_notebook をループ前に1回だけ引く最適化後も、後続文書の reduce
    プロンプトに先行文書がこの実行内で保存したタグが反映される実質挙動は保つ
    (brief §修正3: DBへ問い合わせ直さず、ローカルにタグを追記して引き継ぐ)。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc1.md").write_text("# Doc1\n\ncontent one.\n", encoding="utf-8")
    (nb_dir / "doc2.md").write_text("# Doc2\n\ncontent two.\n", encoding="utf-8")
    store.upsert_document(
        id="doc1", notebook="nb", origin="doc1.md", origin_type="md",
        normalized_path="nb/doc1.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    store.upsert_document(
        id="doc2", notebook="nb", origin="doc2.md", origin_type="md",
        normalized_path="nb/doc2.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "doc1の学び", "chunks": [1]}]),
            _reduce_answer([{"text": "doc1の学び", "sources": [1]}], tags=["新タグ"]),
            _map_answer([{"text": "doc2の学び", "chunks": [1]}]),
            _reduce_answer([{"text": "doc2の学び", "sources": [1]}], tags=["新タグ"]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    service.digest("nb")

    # calls: [doc1 map, doc1 reduce, doc2 map, doc2 reduce]
    doc2_reduce_call = backend.calls[3]
    assert "新タグ" in doc2_reduce_call["prompt"]


def test_digest_masks_note_text_section_and_tags_before_storing(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    secret = "sk-abcdefgh12345678"
    (nb_dir / "doc.md").write_text(
        f"# Doc\n\n## {secret}\n\ncontent body text.\n", encoding="utf-8"
    )
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": f"token {secret} learned", "chunks": [1]}]),
            _reduce_answer(
                [{"text": f"token {secret} learned", "sources": [1]}],
                tags=[f"{secret}タグ"],
            ),
        ]
    )

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    service = ShelfService(store, embedder, lambda name: backend, corpus_dir, mask=fake_mask)

    service.digest("nb")

    notes = store.list_study_notes("nb", "doc")
    assert notes[0]["text"] == "token <REDACTED> learned"
    assert secret not in (notes[0]["section"] or "")
    tags = store.list_document_tags("nb", "doc")
    assert all(secret not in tag for tag in tags)


def test_digest_skips_regeneration_when_pipeline2_and_hash_unchanged_and_not_forced(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nstable content.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "初回の学び", "chunks": [1]}]),
            _reduce_answer([{"text": "初回の学び", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)
    service.digest("nb")
    assert len(backend.calls) == 2

    result = service.digest("nb")

    assert len(backend.calls) == 2  # 再生成されない = backend が再度呼ばれない
    assert result == {"notebook": "nb", "generated": [], "skipped": ["doc"], "errors": []}


def test_digest_regenerates_legacy_pipeline1_notes_even_without_force(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """旧パイプライン(pipeline=1)の study_notes は source_hash が一致していても
    --force なしで再生成対象になる(移行が force なしで進むように・完了条件)。"""
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    markdown = "# Doc\n\nstable content.\n"
    (nb_dir / "doc.md").write_text(markdown, encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    store.replace_study_notes(
        "nb", "doc",
        [{"text": "旧パイプラインの学び", "source_hash": content_hash, "pipeline": 1}],
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "新しい学び", "chunks": [1]}]),
            _reduce_answer([{"text": "新しい学び", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert notes[0]["text"] == "新しい学び"
    assert notes[0]["pipeline"] == 2


def test_digest_force_regenerates_even_when_content_unchanged(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\nstable content.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "初回", "chunks": [1]}]),
            _reduce_answer([{"text": "初回", "sources": [1]}]),
            _map_answer([{"text": "再生成", "chunks": [1]}]),
            _reduce_answer([{"text": "再生成", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)
    service.digest("nb")

    result = service.digest("nb", force=True)

    assert len(backend.calls) == 4
    assert result == {"notebook": "nb", "generated": ["doc"], "skipped": [], "errors": []}
    notes = store.list_study_notes("nb", "doc")
    assert notes[0]["text"] == "再生成"


def test_digest_records_failure_and_continues_without_stopping_other_docs(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "good.md").write_text("# Good\n\ngood content.\n", encoding="utf-8")
    (nb_dir / "bad.md").write_text("# Bad\n\nbad content.\n", encoding="utf-8")
    store.upsert_document(
        id="good", notebook="nb", origin="good.md", origin_type="md",
        normalized_path="nb/good.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    store.upsert_document(
        id="bad", notebook="nb", origin="bad.md", origin_type="md",
        normalized_path="nb/bad.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    # store.list_documents は id 昇順で返すため処理順は "bad" -> "good"。
    # "bad" は(唯一の)ウィンドウの map 応答自体が失敗し全滅=doc エラーとなり、
    # reduce は一度も呼ばれない。
    backend = FakeAnswerBackend(
        canned=[
            RawAnswer(text="", ok=False, error="timeout"),
            _map_answer([{"text": "学び", "chunks": [1]}]),
            _reduce_answer([{"text": "学び", "sources": [1]}]),
        ]
    )
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result["generated"] == ["good"]
    assert result["skipped"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["doc_id"] == "bad"


def test_digest_records_parse_failure_as_error(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\ncontent.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    backend = FakeAnswerBackend(canned=RawAnswer(text="not json", ok=True, error=None))
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir)

    result = service.digest("nb")

    assert result["generated"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["doc_id"] == "doc"


def test_digest_uses_configured_digest_backend_over_notebook_backend(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\ncontent.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    codex_backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="not used"))
    ollama_backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "学び", "chunks": [1]}]),
            _reduce_answer([{"text": "学び", "sources": [1]}]),
        ]
    )
    registry = {"codex": codex_backend, "ollama": ollama_backend}
    service = ShelfService(
        store, embedder, lambda name: registry[name], corpus_dir, digest_backend="ollama"
    )

    result = service.digest("nb")

    assert result["generated"] == ["doc"]
    assert codex_backend.calls == []
    assert len(ollama_backend.calls) == 2


def test_digest_falls_back_to_notebook_backend_when_digest_backend_unset(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    corpus_dir = tmp_path / "corpus"
    nb_dir = corpus_dir / "nb"
    nb_dir.mkdir(parents=True)
    (nb_dir / "doc.md").write_text("# Doc\n\ncontent.\n", encoding="utf-8")
    store.upsert_document(
        id="doc", notebook="nb", origin="doc.md", origin_type="md",
        normalized_path="nb/doc.md", converter="raw", added_at="2024-01-01T00:00:00+00:00",
    )
    codex_backend = FakeAnswerBackend(
        canned=[
            _map_answer([{"text": "学び", "chunks": [1]}]),
            _reduce_answer([{"text": "学び", "sources": [1]}]),
        ]
    )
    ollama_backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="not used"))
    registry = {"codex": codex_backend, "ollama": ollama_backend}
    service = ShelfService(store, embedder, lambda name: registry[name], corpus_dir)

    result = service.digest("nb")

    assert result["generated"] == ["doc"]
    assert ollama_backend.calls == []
    assert len(codex_backend.calls) == 2


def test_digest_rejects_invalid_notebook_name(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    result = service.digest("../../tmp/x")

    assert "error" in result


def test_digest_returns_error_for_unknown_notebook(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    result = service.digest("does-not-exist")

    assert result == {"error": "unknown notebook: does-not-exist. available: []"}


def test_digest_returns_error_for_unknown_doc_id(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    store.create_notebook("nb", backend="codex")
    service = ShelfService(store, embedder, lambda name: FakeAnswerBackend(), tmp_path)

    result = service.digest("nb", doc_id="ghost")

    assert result == {"error": "unknown document: ghost"}


# -- shelve: 自動分類投入（設計書 §13）----------------------------------------
#
# service.shelve は「scan → convert → summarize → classify(Shelver.plan) →
# (dry-run: 打ち切り / apply: notebook作成+_persist_converted+index) → notes」の
# 2フェーズ配線（§13.2）。要約は既存 build_summary_prompt 経路（SUMMARY_SCHEMA）を
# 再利用し、分類は Shelver（V6）にそのまま委譲する。
#
# shelve() は summarize 用に backend_factory を1回、(_get_shelver 経由で) classify
# 用にもう1回だけ呼ぶ（§13.9「要約は service、分類は Shelver が別 backend インスタンス
# を使う」）。_shelve_backend_factory はこの呼び出し順を利用して summarize/classify
# それぞれに別々の FakeAnswerBackend を割り当てる。

_NEW_NOTEBOOK_CLASSIFICATION = (
    '{"action": "new", "notebook": "quantum-notes", '
    '"description": "量子力学の講義ノート集", "reason": "既存に合致なし"}'
)


def _shelve_backend_factory(summarize_backend, classify_backend):
    calls: list[str] = []

    def factory(name: str):
        calls.append(name)
        return summarize_backend if len(calls) == 1 else classify_backend

    return factory


def test_shelve_dry_run_has_zero_persistent_side_effects(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """--dry-run は notebook 作成・corpus 書き込み・documents 行のいずれも行わない
    （§13.2「永続副作用ゼロ」の厳密な定義）。推論（要約+分類）自体は非変異なので
    dry-run でも実行されるが、計画のみを返す。
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    corpus_dir = tmp_path / "corpus"
    converter = _FakeConverter(markdown="# 量子力学入門\n\n量子力学の基礎を解説する資料です。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "量子力学の基礎資料"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    result = service.shelve(str(root), dry_run=True)

    assert result["directory"] == str(root.resolve())
    assert result["dry_run"] is True
    assert result["plan"] == [
        {
            "origin": str((root / "note.md").resolve()),
            "notebook": "quantum-notes",
            "new_notebook": True,
            "summary": "量子力学の基礎資料",
            "reason": "既存に合致なし",
        }
    ]
    assert result["created_notebooks"] == [
        {"notebook": "quantum-notes", "description": "量子力学の講義ノート集", "backend": "ollama"}
    ]
    assert result["skipped"] == []
    assert result["errors"] == []
    # 永続副作用ゼロの検証
    assert store.list_notebooks() == []
    assert store.list_documents("quantum-notes") == []
    assert not corpus_dir.exists()


def test_shelve_apply_creates_notebook_persists_document_and_indexes(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """dry_run=False では計画どおりに新 notebook を作成し(backend=ollama既定・
    persona=None)、要約済み markdown を description=要約/source='auto' で永続化、
    影響 notebook を索引化する。
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    corpus_dir = tmp_path / "corpus"
    converter = _FakeConverter(markdown="# 量子力学入門\n\n量子力学の基礎を解説する資料です。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "量子力学の基礎資料"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    result = service.shelve(str(root), dry_run=False)

    assert result["dry_run"] is False
    assert result["created_notebooks"] == ["quantum-notes"]
    notebook_row = store.get_notebook("quantum-notes")
    assert notebook_row is not None
    assert notebook_row["backend"] == "ollama"
    assert notebook_row["persona"] is None

    assert len(result["added"]) == 1
    added_entry = result["added"][0]
    assert added_entry["origin"] == str((root / "note.md").resolve())
    assert added_entry["notebook"] == "quantum-notes"

    document = store.get_document(added_entry["doc_id"])
    assert document is not None
    assert document["description"] == "量子力学の基礎資料"
    assert document["description_source"] == "auto"

    assert result["chunks_written"] > 0
    assert result["skipped"] == []
    assert result["errors"] == []


def test_shelve_reruns_idempotently_all_skipped_without_duplicating(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """再実行では全件が既投入スキップとなり、notebook・documents とも増えない
    （§13.7 冪等性）。2回目は要約・分類の backend 呼び出しも一切発生しない。
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    corpus_dir = tmp_path / "corpus"
    converter = _FakeConverter(markdown="# 量子力学入門\n\n量子力学の基礎を解説する資料です。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "量子力学の基礎資料"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    first = service.shelve(str(root), dry_run=False)
    summarize_calls_after_first = len(summarize_backend.calls)
    classify_calls_after_first = len(classify_backend.calls)

    second = service.shelve(str(root), dry_run=False)

    assert second["added"] == []
    assert second["created_notebooks"] == []
    assert len(second["skipped"]) == 1
    assert second["skipped"][0]["origin"] == str((root / "note.md").resolve())
    assert "quantum-notes" in second["skipped"][0]["reason"]
    assert second["errors"] == []
    assert len(store.list_notebooks()) == 1
    assert len(store.list_documents("quantum-notes")) == 1
    assert first["added"][0]["doc_id"] == store.list_documents("quantum-notes")[0]["id"]
    # 2回目は推論(要約・分類)を一切呼ばない
    assert len(summarize_backend.calls) == summarize_calls_after_first
    assert len(classify_backend.calls) == classify_calls_after_first


def test_shelve_skips_origin_already_ingested_in_other_notebook(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """既に別 notebook に投入済みの origin は、変換・要約・分類のコストを払わずに
    スキップする（§13.1 決定4: notebook 跨ぎの重複投入・再分類ドリフト防止）。
    """
    store.create_notebook("physics", backend="codex")
    corpus_dir = tmp_path / "corpus"
    root = tmp_path / "docs"
    root.mkdir()
    seeded_file = root / "already-shelved.md"
    seeded_file.write_text("# Seeded\n\n" + "content " * 20, encoding="utf-8")
    seeded_origin = str(seeded_file.resolve())
    (corpus_dir / "physics").mkdir(parents=True)
    (corpus_dir / "physics" / "already-shelved-abc12345.md").write_text(
        "# Seeded\n\ncontent", encoding="utf-8"
    )
    store.upsert_document(
        id="already-shelved-abc12345",
        notebook="physics",
        origin=seeded_origin,
        origin_type="md",
        normalized_path="physics/already-shelved-abc12345.md",
        converter="raw",
        added_at="2026-01-01T00:00:00+00:00",
    )
    converter = _FakeConverter(markdown="# Seeded\n\nこの資料は既に投入済みです。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "使われないはずの要約"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    result = service.shelve(str(root), dry_run=False)

    assert result["added"] == []
    assert result["created_notebooks"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["origin"] == seeded_origin
    assert "physics" in result["skipped"][0]["reason"]
    assert summarize_backend.calls == []
    assert classify_backend.calls == []
    assert len(store.list_documents("physics")) == 1


def test_shelve_scan_rules_skip_hidden_symlink_and_unsupported_files(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """add_directory と同一のスキャン規則を再利用する: 隠しファイル/ディレクトリは
    記録すら残さず除外し、symlink・未対応形式は skipped に記録する（§13.2 手順1）。
    """
    root = tmp_path / "docs"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config.md").write_text("# Config\n\n" + "content " * 20, encoding="utf-8")
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    real_target = tmp_path / "real_target.md"
    real_target.write_text("target content " * 20, encoding="utf-8")
    (root / "link.md").symlink_to(real_target)
    (root / "visible.md").write_text("# Visible\n\n" + "content " * 20, encoding="utf-8")
    corpus_dir = tmp_path / "corpus"
    converter = _FakeConverter(markdown="# 量子力学入門\n\n量子力学の基礎を解説する資料です。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "量子力学の基礎資料"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    result = service.shelve(str(root), dry_run=True)

    assert result["plan"] == [
        {
            "origin": str((root / "visible.md").resolve()),
            "notebook": "quantum-notes",
            "new_notebook": True,
            "summary": "量子力学の基礎資料",
            "reason": "既存に合致なし",
        }
    ]
    skipped_origins = {s["origin"] for s in result["skipped"]}
    assert str(root / "link.md") in skipped_origins
    assert str((root / "image.png").resolve()) in skipped_origins
    assert not any(".git" in o for o in skipped_origins)
    assert len(result["skipped"]) == 2


def test_shelve_converts_each_file_once_uses_summary_as_description_and_recommends_digest(
    store: Store, embedder: FakeEmbedder, tmp_path: Path
) -> None:
    """変換(convert_file)は投入ファイルごとにちょうど1回だけ呼ばれる(§13.1 決定3・
    二重変換禁止)。description は分類用要約と同一のテキストで、digest は自動実行
    されず(study_notes 0件)、notes で `shelf digest` の実行を案内する(§13.1 決定5)。
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# Note\n\n" + "content " * 20, encoding="utf-8")
    corpus_dir = tmp_path / "corpus"
    converter = _FakeConverter(markdown="# 量子力学入門\n\n量子力学の基礎を解説する資料です。\n")
    summarize_backend = FakeAnswerBackend(canned='{"summary": "量子力学の基礎資料"}')
    classify_backend = FakeAnswerBackend(canned=_NEW_NOTEBOOK_CLASSIFICATION)
    service = ShelfService(
        store, embedder,
        _shelve_backend_factory(summarize_backend, classify_backend),
        corpus_dir, converter=converter,
    )

    result = service.shelve(str(root), dry_run=False)

    assert len(converter.file_calls) == 1
    assert converter.file_calls[0] == (root / "note.md").resolve()

    added_entry = result["added"][0]
    document = store.get_document(added_entry["doc_id"])
    assert document is not None
    assert document["description"] == "量子力学の基礎資料"
    assert store.list_study_notes("quantum-notes") == []
    assert result["notes"] == [
        "学びノートは自動生成されません。`shelf digest <notebook>` の実行を検討してください。"
    ]
