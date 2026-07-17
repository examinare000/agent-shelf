"""indexer.py の単体テスト。

Store(":memory:") + FakeEmbedder + tmp_path 上の corpus ディレクトリで、
実DB・実embeddingモデル・ネットワークに触れずに増分索引の分岐を検証する。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from shelf.indexer import (
    SUMMARY_SECTION_AUTO,
    SUMMARY_SECTION_USER,
    IndexStats,
    index_notebook,
)
from shelf.search import cosine_topk
from shelf.store import Store
from tests.fakes import FakeEmbedder


def _write(corpus_dir: Path, notebook: str, name: str, text: str) -> Path:
    nb_dir = corpus_dir / notebook
    nb_dir.mkdir(parents=True, exist_ok=True)
    path = nb_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _add_document(
    store: Store,
    notebook: str,
    doc_id: str,
    *,
    description: str | None = None,
    description_source: str | None = None,
) -> None:
    """indexer が store.get_document(doc_id) で参照する documents 行を用意する。

    doc_id は corpus 上のファイル stem と一致させる必要がある
    （service.py の実運用では corpus/<notebook>/<doc_id>.md として書き出されるため）。
    """
    if store.get_notebook(notebook) is None:
        store.create_notebook(notebook)
    store.upsert_document(
        id=doc_id,
        notebook=notebook,
        origin=f"/tmp/{doc_id}.md",
        origin_type="file",
        normalized_path=f"{notebook}/{doc_id}.md",
        converter="raw",
        added_at="2024-01-01T00:00:00+00:00",
        description=description,
        description_source=description_source,
    )


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=8)


def test_index_new_notebook_indexes_all_files(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _write(tmp_path, "physics", "b.md", "# B\n\nbye world\n")

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.indexed == 2
    assert stats.skipped == 0
    assert stats.pruned == 0
    assert stats.chunks_written == 2
    assert stats.errors == []
    # チャンクが実際に store へ書き込まれている（id は notebook/doc_id#seq）。
    assert store.get_chunk("physics/a#0") is not None
    assert store.get_chunk("physics/b#0") is not None


def test_unchanged_file_is_skipped_on_second_run(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    index_notebook(tmp_path, "physics", store, embedder)

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.indexed == 0
    assert stats.skipped == 1
    assert stats.chunks_written == 0


def test_mtime_change_triggers_reindex(tmp_path, store, embedder) -> None:
    path = _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    index_notebook(tmp_path, "physics", store, embedder)

    # 内容を変えて mtime/size を更新する（+10秒先に進めて確実に差分を作る）。
    new_stat_time = path.stat().st_mtime + 10
    path.write_text("# A\n\nhello updated world\n", encoding="utf-8")
    os.utime(path, (new_stat_time, new_stat_time))

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.indexed == 1
    assert stats.skipped == 0


def test_removed_file_is_pruned(tmp_path, store, embedder) -> None:
    path_a = _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _write(tmp_path, "physics", "b.md", "# B\n\nbye world\n")
    index_notebook(tmp_path, "physics", store, embedder)

    path_a.unlink()
    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.pruned == 1
    assert store.get_chunk("physics/a#0") is None
    assert store.get_chunk("physics/b#0") is not None


def test_full_rebuild_reindexes_unchanged_files(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _write(tmp_path, "physics", "b.md", "# B\n\nbye world\n")
    index_notebook(tmp_path, "physics", store, embedder)

    stats = index_notebook(tmp_path, "physics", store, embedder, full=True)

    assert stats.indexed == 2
    assert stats.skipped == 0


def test_model_change_forces_full_rebuild(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _write(tmp_path, "physics", "b.md", "# B\n\nbye world\n")
    index_notebook(tmp_path, "physics", store, embedder)

    other_embedder = FakeEmbedder(dim=8)
    other_embedder.model_name = "fake-embedder-v2"
    stats = index_notebook(tmp_path, "physics", store, other_embedder)

    assert stats.indexed == 2
    assert stats.skipped == 0


def test_broken_file_is_recorded_as_error_and_others_continue(
    tmp_path, store, embedder, monkeypatch
) -> None:
    _write(tmp_path, "physics", "broken.md", "# Broken\n\nunreadable\n")
    _write(tmp_path, "physics", "ok.md", "# OK\n\nreadable\n")

    original_read_text = Path.read_text

    def flaky_read_text(self: Path, *args, **kwargs):
        if self.name == "broken.md":
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.indexed == 1
    assert len(stats.errors) == 1
    assert "broken.md" in stats.errors[0]
    assert store.get_chunk("physics/ok#0") is not None


def test_mask_is_applied_to_document_body(tmp_path, store, embedder) -> None:
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdefghij"
    _write(tmp_path, "physics", "a.md", f"# A\n\ntoken: {secret}\n")

    def fake_mask(text: str) -> str:
        return text.replace(secret, "<REDACTED>")

    index_notebook(tmp_path, "physics", store, embedder, mask=fake_mask)

    chunk = store.get_chunk("physics/a#0")
    assert chunk is not None
    assert secret not in chunk["text"]
    assert "<REDACTED>" in chunk["text"]


def test_indexing_one_notebook_does_not_prune_another_notebooks_files(
    tmp_path, store, embedder
) -> None:
    """file_state は notebook 列を持たずグローバル管理のため、notebook A の索引実行が
    notebook B の既存ファイルを誤って prune しないことを保証する回帰テスト。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _write(tmp_path, "chemistry", "c.md", "# C\n\nreaction\n")
    index_notebook(tmp_path, "physics", store, embedder)
    index_notebook(tmp_path, "chemistry", store, embedder)

    # physics だけを再索引しても chemistry 側のファイルは pruned にならない。
    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.pruned == 0
    assert store.get_chunk("chemistry/c#0") is not None


def test_index_notebook_rejects_invalid_notebook_name_before_touching_corpus(
    tmp_path, store, embedder
) -> None:
    """indexer.index_notebook は ShelfService を経由しない直接呼び出し経路への防御として
    validate_notebook_name を通す(重大指摘#1: notebook 名の未検証パストラバーサル)。
    corpus_dir に一切アクセスしないよう、ValueError が glob より先に送出されることを
    「corpus_dir 直下に何も作られない」という観測で確認する。
    """
    with pytest.raises(ValueError):
        index_notebook(tmp_path, "../../tmp/evil", store, embedder)

    assert list(tmp_path.iterdir()) == []


def test_index_stats_is_recall_shaped() -> None:
    stats = IndexStats(indexed=1, skipped=2, pruned=3, chunks_written=4, errors=["x"])

    assert stats.indexed == 1
    assert stats.skipped == 2
    assert stats.pruned == 3
    assert stats.chunks_written == 4
    assert stats.errors == ["x"]


def test_document_description_is_indexed_as_summary_chunk_with_seq_minus_one(
    tmp_path, store, embedder
) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(
        store, "physics", "a", description="要約テキスト", description_source="auto"
    )

    index_notebook(tmp_path, "physics", store, embedder)

    body_chunk = store.get_chunk("physics/a#0")
    summary_chunk = store.get_chunk("physics/a#-1")
    assert summary_chunk is not None
    assert summary_chunk["text"] == "要約テキスト"
    assert summary_chunk["section"] == SUMMARY_SECTION_AUTO
    assert summary_chunk["page"] is None
    assert summary_chunk["seq"] == -1
    assert body_chunk is not None
    assert summary_chunk["source_path"] == body_chunk["source_path"]


def test_user_description_gets_user_section_label(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(
        store, "physics", "a", description="ユーザー要約", description_source="user"
    )

    index_notebook(tmp_path, "physics", store, embedder)

    summary_chunk = store.get_chunk("physics/a#-1")
    assert summary_chunk is not None
    assert summary_chunk["section"] == SUMMARY_SECTION_USER


def test_description_source_null_falls_back_to_user_label(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description="出所不明の要約", description_source=None)

    index_notebook(tmp_path, "physics", store, embedder)

    summary_chunk = store.get_chunk("physics/a#-1")
    assert summary_chunk is not None
    assert summary_chunk["section"] == SUMMARY_SECTION_USER


def test_summary_chunk_is_searchable_via_load_vectors(tmp_path, store) -> None:
    description = "この文書は量子力学の入門です"
    known_vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    embedder = FakeEmbedder(dim=8, known={description: known_vec})
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description=description, description_source="auto")

    index_notebook(tmp_path, "physics", store, embedder)

    ids, matrix = store.load_vectors("physics")
    query_vec = embedder.embed_query(description)
    hits = cosine_topk(matrix, ids, query_vec, limit=1)
    assert hits[0].id == "physics/a#-1"


def test_reindex_removes_stale_summary_chunk_when_description_cleared(
    tmp_path, store, embedder
) -> None:
    path = _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description="要約テキスト", description_source="auto")
    index_notebook(tmp_path, "physics", store, embedder)
    assert store.get_chunk("physics/a#-1") is not None

    _add_document(store, "physics", "a", description=None, description_source=None)
    new_stat_time = path.stat().st_mtime + 10
    os.utime(path, (new_stat_time, new_stat_time))

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-1") is None


def test_document_without_description_produces_no_summary_chunk(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description=None, description_source=None)

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-1") is None
    assert store.get_chunk("physics/a#0") is not None


def test_chunks_written_counts_summary_chunk(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description="要約テキスト", description_source="auto")

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.chunks_written == 2


def test_summary_and_body_chunk_kinds_are_labeled(tmp_path, store, embedder) -> None:
    """R7: 既存 description サマリチャンクは kind='summary'、本文は kind='body' に整合させる。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a", description="要約テキスト", description_source="auto")

    index_notebook(tmp_path, "physics", store, embedder)

    summary_chunk = store.get_chunk("physics/a#-1")
    body_chunk = store.get_chunk("physics/a#0")
    assert summary_chunk is not None
    assert summary_chunk["kind"] == "summary"
    assert body_chunk is not None
    assert body_chunk["kind"] == "body"


def test_study_notes_are_indexed_as_digest_chunks(tmp_path, store, embedder) -> None:
    """R7: study_notes を kind='digest' チャンクとして索引化する（§4-B）。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": "学び1", "source_span": "§1"}])

    index_notebook(tmp_path, "physics", store, embedder)

    digest_chunk = store.get_chunk("physics/a#-2")
    body_chunk = store.get_chunk("physics/a#0")
    assert digest_chunk is not None
    assert digest_chunk["text"] == "学び1"
    assert digest_chunk["kind"] == "digest"
    assert digest_chunk["seq"] == -2
    assert digest_chunk["section"] == "§1"
    assert body_chunk is not None
    assert digest_chunk["source_path"] == body_chunk["source_path"]


def test_multiple_study_notes_get_sequential_negative_seqs(tmp_path, store, embedder) -> None:
    """digest チャンクの seq は本文(≥0)・サマリ(-1)と衝突しない予約負域(≤-2)を使う。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes(
        "physics", "a", [{"text": "学び1"}, {"text": "学び2"}, {"text": "学び3"}]
    )

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-2")["text"] == "学び1"
    assert store.get_chunk("physics/a#-3")["text"] == "学び2"
    assert store.get_chunk("physics/a#-4")["text"] == "学び3"


def test_digest_chunk_uses_note_section_and_page_when_present(tmp_path, store, embedder) -> None:
    """map-reduce パイプライン(pipeline=2)の study_notes は section/page を直接持つ。
    旧 source_span より優先して使う（新パイプラインの正確なチャンク接地情報を
    人間可読表示に反映するため）。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes(
        "physics", "a",
        [{"text": "学び1", "source_span": "§旧", "section": "§2.3", "page": 5}],
    )

    index_notebook(tmp_path, "physics", store, embedder)

    digest_chunk = store.get_chunk("physics/a#-2")
    assert digest_chunk["section"] == "§2.3"
    assert digest_chunk["page"] == 5


def test_digest_chunk_falls_back_to_source_span_when_section_absent(
    tmp_path, store, embedder
) -> None:
    """旧パイプライン(pipeline=1)の study_notes は section を持たないため、
    従来どおり source_span を代替の人間可読表示に使う（後方互換）。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": "学び1", "source_span": "§1"}])

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-2")["section"] == "§1"
    assert store.get_chunk("physics/a#-2")["page"] is None


def test_document_without_study_notes_produces_no_digest_chunks(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-2") is None


def test_digest_chunk_is_searchable_via_load_vectors(tmp_path, store) -> None:
    note_text = "この学びは量子もつれについて"
    known_vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    embedder = FakeEmbedder(dim=8, known={note_text: known_vec})
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": note_text}])

    index_notebook(tmp_path, "physics", store, embedder)

    ids, matrix = store.load_vectors("physics")
    query_vec = embedder.embed_query(note_text)
    hits = cosine_topk(matrix, ids, query_vec, limit=1)
    assert hits[0].id == "physics/a#-2"


def test_chunks_written_counts_digest_chunks(tmp_path, store, embedder) -> None:
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": "学び1"}, {"text": "学び2"}])

    stats = index_notebook(tmp_path, "physics", store, embedder)

    assert stats.chunks_written == 3  # body 1 + digest 2


def test_reindexing_with_full_rebuild_does_not_duplicate_digest_chunks(
    tmp_path, store, embedder
) -> None:
    """`shelf index` 再実行(full rebuild)での冪等性: 重複チャンクを作らない。"""
    _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": "学び1"}])
    index_notebook(tmp_path, "physics", store, embedder)

    index_notebook(tmp_path, "physics", store, embedder, full=True)

    ids, _ = store.load_vectors("physics")
    digest_ids = [i for i in ids if i == "physics/a#-2"]
    assert len(digest_ids) == 1


def test_reindex_removes_stale_digest_chunk_when_study_notes_cleared(
    tmp_path, store, embedder
) -> None:
    """study_notes 削除時、次回 index で対応する digest チャンクが prune される。"""
    path = _write(tmp_path, "physics", "a.md", "# A\n\nhello world\n")
    _add_document(store, "physics", "a")
    store.replace_study_notes("physics", "a", [{"text": "学び1"}])
    index_notebook(tmp_path, "physics", store, embedder)
    assert store.get_chunk("physics/a#-2") is not None

    store.replace_study_notes("physics", "a", [])
    new_stat_time = path.stat().st_mtime + 10
    os.utime(path, (new_stat_time, new_stat_time))

    index_notebook(tmp_path, "physics", store, embedder)

    assert store.get_chunk("physics/a#-2") is None
