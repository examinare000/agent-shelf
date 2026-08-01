"""shelf の主要フロー全体を通しで検証する e2e テスト。

new(create_notebook) → add(add_source) → index → ask の一気通貫を、
tests/fakes.py の決定論的ダブル（FakeEmbedder/FakeAnswerBackend/FakeConverter）
だけを使って検証する。実DB(":memory:")・実埋め込みモデル・実サブスクCLI・実ファイル
変換のいずれにも触れない（他のユニットテストと同じ方針。test_service.py の各段階の
単体テストに対し、こちらは段階間の配線が壊れていないことを確認する統合テスト）。
"""
from __future__ import annotations

import json
from pathlib import Path

from shelf.ports import RawAnswer
from shelf.service import ShelfService
from shelf.store import Store
from tests.fakes import FakeAnswerBackend, FakeConverter, FakeEmbedder

_KNOWN_VEC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
_CHUNK_TEXT = "penguins huddle together for warmth in antarctica"
_QUERY_TEXT = "how do penguins stay warm?"


def test_new_add_index_ask_end_to_end_returns_grounded_answer_with_citations(
    tmp_path: Path,
) -> None:
    store = Store(":memory:")
    embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
    payload = {
        "answer": "penguins huddle together for warmth [S1]",
        "citations": [{"s": 1}],
        "confident": True,
    }
    backend = FakeAnswerBackend(canned=RawAnswer(text=json.dumps(payload), ok=True, error=None))
    converter = FakeConverter(markdown=f"# Doc\n\n{_CHUNK_TEXT}\n")
    corpus_dir = tmp_path / "corpus"
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir, converter=converter)

    service.create_notebook("wildlife", backend="codex")

    source_file = tmp_path / "source.txt"
    source_file.write_text("unused by fake converter", encoding="utf-8")
    add_result = service.add_source("wildlife", str(source_file), auto_summary=False)
    assert "error" not in add_result
    assert add_result["chunks_written"] == 1

    # add_source は内部で既に索引化しており、file_state(mtime/size)が変化して
    # いないため、明示 index() の再実行は増分判定により skip される（冪等性の確認）。
    stats = service.index("wildlife")
    assert stats.skipped == 1
    assert stats.chunks_written == 0

    result = service.ask("wildlife", _QUERY_TEXT)

    assert "error" not in result
    assert result["grounded"] is True
    doc_id = add_result["doc_id"]
    assert result["citations"] == [
        {
            "n": 1,
            "chunk_id": f"wildlife/{doc_id}#0",
            "source": f"wildlife/{doc_id}.md",
            # チャンカーは markdown の見出しをそのまま section に採用するため
            # "# Doc" 見出し配下の本文チャンクは section="Doc" になる。
            "section": "Doc",
            "page": None,
            "quote": _CHUNK_TEXT,
        }
    ]


def test_new_add_index_ask_end_to_end_ungrounded_when_backend_not_confident(
    tmp_path: Path,
) -> None:
    """同じ一気通貫の配線で、backend が confident=False を返した場合は
    grounded=False として最後まで安全に伝播することを確認する。"""
    store = Store(":memory:")
    embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
    payload = {
        "answer": "penguins huddle together for warmth [S1]",
        "citations": [{"s": 1}],
        "confident": False,
    }
    backend = FakeAnswerBackend(canned=RawAnswer(text=json.dumps(payload), ok=True, error=None))
    converter = FakeConverter(markdown=f"# Doc\n\n{_CHUNK_TEXT}\n")
    corpus_dir = tmp_path / "corpus"
    service = ShelfService(store, embedder, lambda name: backend, corpus_dir, converter=converter)

    service.create_notebook("wildlife", backend="codex")
    source_file = tmp_path / "source.txt"
    source_file.write_text("unused by fake converter", encoding="utf-8")
    add_result = service.add_source("wildlife", str(source_file), auto_summary=False)
    service.index("wildlife")

    result = service.ask("wildlife", _QUERY_TEXT)

    assert "error" not in result
    assert result["grounded"] is False
    assert result["citations"][0]["chunk_id"] == f"wildlife/{add_result['doc_id']}#0"
