"""Store の notebook/document/chunk CRUD・file_state/meta・ベクタキャッシュの単体テスト。

:memory: SQLite のみを使用し、ネットワーク・実ファイル・時計に依存しない
（recall/tests/test_store.py のテストスタイルを踏襲）。
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from shelf.store import DuplicateNotebookError, Store, UnknownNotebookError


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _chunk_row(
    id_="doc1#0",
    notebook="physics",
    doc_id="doc1",
    source_path="corpus/physics/doc1.md",
    seq=0,
    text="本文",
    embedding=(0.1, 0.2, 0.3, 0.4),
    section=None,
    page=None,
):
    return {
        "id": id_,
        "notebook": notebook,
        "doc_id": doc_id,
        "source_path": source_path,
        "section": section,
        "page": page,
        "seq": seq,
        "text": text,
        "embedding": np.asarray(embedding, dtype=np.float32),
    }


def _make_notebook(store, name="physics", description="物理の論文", backend=None):
    store.create_notebook(name, description=description, backend=backend)


def _make_document(
    store,
    id_="doc1",
    notebook="physics",
    origin="feynman.pdf",
    origin_type="pdf",
    normalized_path="corpus/physics/doc1.md",
    converter="pymupdf4llm",
    added_at="2026-01-01T00:00:00Z",
    title=None,
    content_hash=None,
    fetched_at=None,
    description=None,
    description_source=None,
):
    store.upsert_document(
        id=id_,
        notebook=notebook,
        origin=origin,
        origin_type=origin_type,
        normalized_path=normalized_path,
        converter=converter,
        added_at=added_at,
        title=title,
        content_hash=content_hash,
        fetched_at=fetched_at,
        description=description,
        description_source=description_source,
    )


class TestNotebookCRUD:
    def test_create_then_get_notebook_round_trips(self, store):
        store.create_notebook("physics", description="物理の論文", backend="codex")

        nb = store.get_notebook("physics")

        assert nb["name"] == "physics"
        assert nb["description"] == "物理の論文"
        assert nb["backend"] == "codex"
        assert nb["created_at"]  # 非空文字列であればよい（時計依存の厳密比較はしない）

    def test_create_notebook_defaults_backend_to_codex(self, store):
        store.create_notebook("physics", description="物理の論文")

        assert store.get_notebook("physics")["backend"] == "codex"

    def test_get_notebook_returns_none_for_unknown_name(self, store):
        assert store.get_notebook("does-not-exist") is None

    def test_create_notebook_with_duplicate_name_raises(self, store):
        store.create_notebook("physics", description="1回目")

        with pytest.raises(DuplicateNotebookError):
            store.create_notebook("physics", description="2回目")

    def test_list_notebooks_aggregates_document_and_chunk_counts(self, store):
        _make_notebook(store, name="physics")
        _make_notebook(store, name="empty-nb")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", notebook="physics", doc_id="doc1", seq=0),
             _chunk_row(id_="doc1#1", notebook="physics", doc_id="doc1", seq=1)]
        )

        listed = {nb["name"]: nb for nb in store.list_notebooks()}

        assert listed["physics"]["documents"] == 1
        assert listed["physics"]["chunks"] == 2
        assert listed["empty-nb"]["documents"] == 0
        assert listed["empty-nb"]["chunks"] == 0

    def test_delete_notebook_cascades_documents_chunks_and_file_state(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks([_chunk_row(id_="doc1#0", notebook="physics", doc_id="doc1")])
        store.set_file_state(
            "corpus/physics/doc1.md", mtime=1.0, size=10, model="fake-model"
        )

        store.delete_notebook("physics")

        assert store.get_notebook("physics") is None
        assert store.list_documents("physics") == []
        ids, _ = store.load_vectors("physics")
        assert ids == []
        assert store.get_file_state("corpus/physics/doc1.md") is None

    def test_delete_notebook_cascades_study_notes(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.replace_study_notes("physics", "doc1", [{"text": "学び1"}])

        store.delete_notebook("physics")

        assert store.list_study_notes("physics") == []


class TestNotebookPersona:
    def test_create_notebook_with_persona_round_trips(self, store):
        store.create_notebook("physics", description="物理の論文", persona="量子力学の専門家")

        nb = store.get_notebook("physics")

        assert nb["persona"] == "量子力学の専門家"

    def test_create_notebook_defaults_persona_to_none(self, store):
        store.create_notebook("physics", description="物理の論文")

        assert store.get_notebook("physics")["persona"] is None

    def test_list_notebooks_includes_persona(self, store):
        _make_notebook(store, name="physics")
        store.set_persona("physics", "量子力学の専門家")

        listed = {nb["name"]: nb for nb in store.list_notebooks()}

        assert listed["physics"]["persona"] == "量子力学の専門家"

    def test_set_persona_updates_existing_notebook(self, store):
        _make_notebook(store, name="physics")

        store.set_persona("physics", "量子力学の専門家")

        assert store.get_notebook("physics")["persona"] == "量子力学の専門家"

    def test_set_persona_can_clear_back_to_none(self, store):
        _make_notebook(store, name="physics")
        store.set_persona("physics", "量子力学の専門家")

        store.set_persona("physics", None)

        assert store.get_notebook("physics")["persona"] is None

    def test_set_persona_unknown_notebook_raises(self, store):
        with pytest.raises(UnknownNotebookError):
            store.set_persona("does-not-exist", "専門家")


class TestDocumentCRUD:
    def test_upsert_then_get_document_round_trips(self, store):
        _make_notebook(store, name="physics")
        _make_document(
            store,
            id_="doc1",
            notebook="physics",
            origin="feynman.pdf",
            origin_type="pdf",
            normalized_path="corpus/physics/doc1.md",
            converter="pymupdf4llm",
            added_at="2026-01-01T00:00:00Z",
            title="Feynman Lectures",
            content_hash="abc123",
            fetched_at=None,
        )

        doc = store.get_document("doc1")

        assert doc["id"] == "doc1"
        assert doc["notebook"] == "physics"
        assert doc["origin"] == "feynman.pdf"
        assert doc["origin_type"] == "pdf"
        assert doc["normalized_path"] == "corpus/physics/doc1.md"
        assert doc["converter"] == "pymupdf4llm"
        assert doc["added_at"] == "2026-01-01T00:00:00Z"
        assert doc["title"] == "Feynman Lectures"
        assert doc["content_hash"] == "abc123"
        assert doc["fetched_at"] is None

    def test_get_document_returns_none_for_unknown_id(self, store):
        assert store.get_document("does-not-exist") is None

    def test_upsert_document_replaces_existing_row_with_same_id(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics", title="旧タイトル")
        _make_document(store, id_="doc1", notebook="physics", title="新タイトル")

        assert store.get_document("doc1")["title"] == "新タイトル"
        assert len(store.list_documents("physics")) == 1

    def test_upsert_document_with_unknown_notebook_raises(self, store):
        with pytest.raises(UnknownNotebookError):
            _make_document(store, id_="doc1", notebook="does-not-exist")

    def test_upsert_document_unique_origin_conflict_raises_integrity_error_not_unknown_notebook(
        self, store
    ):
        """UNIQUE(notebook, origin) 制約違反を FK 違反(UnknownNotebookError)と同じ
        except 節で握りつぶすと、"notebook が存在しない" という誤った診断になる
        (中位指摘#3)。事前の get_notebook 存在確認で FK 違反経路を切り離した後は、
        真の UNIQUE 違反がそのまま sqlite3.IntegrityError として上がることを確認する。
        """
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics", origin="same.pdf")

        with pytest.raises(sqlite3.IntegrityError):
            _make_document(store, id_="doc2", notebook="physics", origin="same.pdf")

    def test_list_documents_filters_by_notebook(self, store):
        _make_notebook(store, name="physics")
        _make_notebook(store, name="math")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="math", origin="algebra.pdf",
                       normalized_path="corpus/math/doc2.md")

        docs = store.list_documents("physics")

        assert [d["id"] for d in docs] == ["doc1"]

    def test_delete_document_removes_its_chunks(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="physics", origin="doc2.pdf",
                       normalized_path="corpus/physics/doc2.md")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", notebook="physics", doc_id="doc1"),
             _chunk_row(id_="doc2#0", notebook="physics", doc_id="doc2",
                        source_path="corpus/physics/doc2.md")]
        )

        store.delete_document("doc1")

        assert store.get_document("doc1") is None
        assert store.get_document("doc2") is not None
        ids, _ = store.load_vectors("physics")
        assert ids == ["doc2#0"]

    def test_delete_document_removes_its_study_notes(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.replace_study_notes("physics", "doc1", [{"text": "学び1"}])

        store.delete_document("doc1")

        assert store.list_study_notes("physics", "doc1") == []

    def test_upsert_document_round_trips_description_and_source(self, store):
        _make_notebook(store, name="physics")
        _make_document(
            store,
            id_="doc1",
            notebook="physics",
            description="ファインマン物理学の要約",
            description_source="auto",
        )

        doc = store.get_document("doc1")
        listed = store.list_documents("physics")[0]

        assert doc["description"] == "ファインマン物理学の要約"
        assert doc["description_source"] == "auto"
        assert listed["description"] == "ファインマン物理学の要約"
        assert listed["description_source"] == "auto"

    def test_upsert_document_updates_description_on_conflict(self, store):
        _make_notebook(store, name="physics")
        _make_document(
            store, id_="doc1", notebook="physics",
            description="旧要約", description_source="auto",
        )

        _make_document(
            store, id_="doc1", notebook="physics",
            description="新要約", description_source="user",
        )

        doc = store.get_document("doc1")
        assert doc["description"] == "新要約"
        assert doc["description_source"] == "user"

    def test_upsert_document_defaults_description_to_none(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        doc = store.get_document("doc1")

        assert doc["description"] is None
        assert doc["description_source"] is None


class TestDocumentSchemaMigration:
    """既存 DB（description 列なしの旧スキーマ）を開いた際の冪等マイグレーションを検証する。

    Store の通常フィクスチャは CREATE TABLE IF NOT EXISTS から始まる新規 DB しか
    作らないため、ここだけは生の sqlite3 で旧スキーマファイルを用意する。
    """

    def _create_legacy_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE notebooks (
              name        TEXT PRIMARY KEY,
              description TEXT,
              backend     TEXT NOT NULL DEFAULT 'codex',
              created_at  TEXT NOT NULL
            );
            CREATE TABLE documents (
              id              TEXT PRIMARY KEY,
              notebook        TEXT NOT NULL REFERENCES notebooks(name),
              origin          TEXT NOT NULL,
              origin_type     TEXT NOT NULL,
              normalized_path TEXT NOT NULL,
              title           TEXT,
              converter       TEXT NOT NULL,
              content_hash    TEXT,
              added_at        TEXT NOT NULL,
              fetched_at      TEXT,
              UNIQUE(notebook, origin)
            );
            """
        )
        conn.commit()
        conn.close()

    def test_init_migrates_existing_db_without_description_columns(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store = Store(db_path)
        try:
            _make_notebook(store, name="physics")
            _make_document(
                store, id_="doc1", notebook="physics",
                description="移行後の要約", description_source="auto",
            )

            doc = store.get_document("doc1")
            assert doc["description"] == "移行後の要約"
            assert doc["description_source"] == "auto"
        finally:
            store.close()

    def test_init_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store1 = Store(db_path)
        store1.close()
        store2 = Store(db_path)  # 2回目の open で ALTER TABLE の重複エラーが出ないことを確認
        try:
            _make_notebook(store2, name="physics")
            _make_document(store2, id_="doc1", notebook="physics", description="OK")
            assert store2.get_document("doc1")["description"] == "OK"
        finally:
            store2.close()


class TestPersonaKindStudyNotesSchemaMigration:
    """旧スキーマ DB（notebooks.persona・chunks.kind・study_notes のいずれも欠く）を
    開いた際の冪等マイグレーションを検証する（R2: docs/design-shelf-reference-service.md §4-A）。

    既存の chunks 行を伴わせておくことで、ALTER TABLE ... DEFAULT 'body' が
    「新規追加された列だけでなく既存行にも正しく後入れされる」ことを確認する。
    """

    def _create_legacy_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE notebooks (
              name        TEXT PRIMARY KEY,
              description TEXT,
              backend     TEXT NOT NULL DEFAULT 'codex',
              created_at  TEXT NOT NULL
            );
            CREATE TABLE documents (
              id              TEXT PRIMARY KEY,
              notebook        TEXT NOT NULL REFERENCES notebooks(name),
              origin          TEXT NOT NULL,
              origin_type     TEXT NOT NULL,
              normalized_path TEXT NOT NULL,
              title           TEXT,
              converter       TEXT NOT NULL,
              content_hash    TEXT,
              added_at        TEXT NOT NULL,
              fetched_at      TEXT,
              description        TEXT,
              description_source TEXT,
              UNIQUE(notebook, origin)
            );
            CREATE TABLE chunks (
              id          TEXT PRIMARY KEY,
              notebook    TEXT NOT NULL,
              doc_id      TEXT NOT NULL,
              source_path TEXT NOT NULL,
              section     TEXT,
              page        INTEGER,
              seq         INTEGER NOT NULL,
              text        TEXT NOT NULL,
              embedding   BLOB NOT NULL,
              dim         INTEGER NOT NULL,
              UNIQUE(notebook, doc_id, seq)
            );
            INSERT INTO notebooks (name, description, backend, created_at)
                VALUES ('physics', '物理の論文', 'codex', '2026-01-01T00:00:00Z');
            INSERT INTO documents
                (id, notebook, origin, origin_type, normalized_path, converter, added_at)
                VALUES ('doc1', 'physics', 'feynman.pdf', 'pdf', 'corpus/physics/doc1.md',
                        'pymupdf4llm', '2026-01-01T00:00:00Z');
            INSERT INTO chunks
                (id, notebook, doc_id, source_path, seq, text, embedding, dim)
                VALUES ('doc1#0', 'physics', 'doc1', 'corpus/physics/doc1.md', 0, '既存本文',
                        X'0000803F', 1);
            """
        )
        conn.commit()
        conn.close()

    def test_init_migrates_persona_column_and_existing_rows_default_to_none(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store = Store(db_path)
        try:
            assert store.get_notebook("physics")["persona"] is None
            store.set_persona("physics", "量子力学の専門家")
            assert store.get_notebook("physics")["persona"] == "量子力学の専門家"
        finally:
            store.close()

    def test_init_migrates_kind_column_and_existing_chunk_rows_default_to_body(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store = Store(db_path)
        try:
            # ALTER TABLE ... DEFAULT 'body' は既存行にも遡って値を入れる。
            assert store.get_chunk("doc1#0")["kind"] == "body"
        finally:
            store.close()

    def test_init_creates_study_notes_table_usable_immediately(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store = Store(db_path)
        try:
            store.replace_study_notes("physics", "doc1", [{"text": "移行後の学び"}])
            notes = store.list_study_notes("physics", "doc1")
            assert [n["text"] for n in notes] == ["移行後の学び"]
        finally:
            store.close()

    def test_init_migration_is_idempotent_across_two_opens(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store1 = Store(db_path)
        store1.close()
        store2 = Store(db_path)  # 2回目の open で ALTER TABLE の重複エラーが出ないことを確認
        try:
            assert store2.get_chunk("doc1#0")["kind"] == "body"
            store2.set_persona("physics", "専門家")
            assert store2.get_notebook("physics")["persona"] == "専門家"
            store2.replace_study_notes("physics", "doc1", [{"text": "学び"}])
            assert len(store2.list_study_notes("physics", "doc1")) == 1
        finally:
            store2.close()


class TestChunkCRUD:
    def test_upsert_chunks_then_get_chunk_returns_metadata_and_text(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", section="§3.2", page=42, text="本文テキスト")]
        )

        chunk = store.get_chunk("doc1#0")

        assert chunk["id"] == "doc1#0"
        assert chunk["notebook"] == "physics"
        assert chunk["doc_id"] == "doc1"
        assert chunk["source_path"] == "corpus/physics/doc1.md"
        assert chunk["section"] == "§3.2"
        assert chunk["page"] == 42
        assert chunk["seq"] == 0
        assert chunk["text"] == "本文テキスト"

    def test_get_chunk_returns_none_for_unknown_id(self, store):
        assert store.get_chunk("does-not-exist") is None

    def test_upsert_chunks_replaces_existing_row_with_same_id(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks([_chunk_row(id_="doc1#0", text="旧")])
        store.upsert_chunks([_chunk_row(id_="doc1#0", text="新")])

        assert store.get_chunk("doc1#0")["text"] == "新"
        ids, _ = store.load_vectors("physics")
        assert ids == ["doc1#0"]

    def test_delete_by_source_file_removes_its_chunks_only(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="physics", origin="doc2.pdf",
                       normalized_path="corpus/physics/doc2.md")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", doc_id="doc1", source_path="corpus/physics/doc1.md"),
             _chunk_row(id_="doc2#0", doc_id="doc2", source_path="corpus/physics/doc2.md")]
        )

        store.delete_by_source_file("corpus/physics/doc1.md")

        ids, _ = store.load_vectors("physics")
        assert ids == ["doc2#0"]


class TestChunkKind:
    def test_upsert_chunks_defaults_kind_to_body(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks([_chunk_row(id_="doc1#0")])

        assert store.get_chunk("doc1#0")["kind"] == "body"

    def test_upsert_chunks_respects_explicit_kind(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        row = _chunk_row(id_="doc1#-2", seq=-2)
        row["kind"] = "digest"

        store.upsert_chunks([row])

        assert store.get_chunk("doc1#-2")["kind"] == "digest"

    def test_upsert_chunks_updates_kind_on_conflict(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        first = _chunk_row(id_="doc1#0")
        first["kind"] = "body"
        store.upsert_chunks([first])
        second = _chunk_row(id_="doc1#0")
        second["kind"] = "summary"

        store.upsert_chunks([second])

        assert store.get_chunk("doc1#0")["kind"] == "summary"


class TestStudyNotesCRUD:
    def test_replace_study_notes_then_list_round_trips(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes(
            "physics",
            "doc1",
            [{"text": "学び1", "source_span": "§1", "source_hash": "h1", "model": "qwen3:8b"}],
        )

        notes = store.list_study_notes("physics", "doc1")
        assert len(notes) == 1
        note = notes[0]
        assert note["id"] == "physics/doc1#d0"
        assert note["notebook"] == "physics"
        assert note["doc_id"] == "doc1"
        assert note["seq"] == 0
        assert note["text"] == "学び1"
        assert note["source_span"] == "§1"
        assert note["source_hash"] == "h1"
        assert note["model"] == "qwen3:8b"
        assert note["created_at"]

    def test_replace_study_notes_generates_sequential_ids(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes(
            "physics", "doc1", [{"text": "学び1"}, {"text": "学び2"}]
        )

        notes = store.list_study_notes("physics", "doc1")
        assert [n["id"] for n in notes] == ["physics/doc1#d0", "physics/doc1#d1"]
        assert [n["seq"] for n in notes] == [0, 1]

    def test_replace_study_notes_overwrites_existing_notes_for_doc(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.replace_study_notes("physics", "doc1", [{"text": "旧学び"}])

        store.replace_study_notes("physics", "doc1", [{"text": "新学び"}])

        notes = store.list_study_notes("physics", "doc1")
        assert [n["text"] for n in notes] == ["新学び"]

    def test_replace_study_notes_with_empty_list_clears_notes(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.replace_study_notes("physics", "doc1", [{"text": "学び1"}])

        store.replace_study_notes("physics", "doc1", [])

        assert store.list_study_notes("physics", "doc1") == []

    def test_list_study_notes_filters_by_doc_id(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="physics", origin="doc2.pdf",
                        normalized_path="corpus/physics/doc2.md")
        store.replace_study_notes("physics", "doc1", [{"text": "doc1の学び"}])
        store.replace_study_notes("physics", "doc2", [{"text": "doc2の学び"}])

        notes = store.list_study_notes("physics", "doc1")

        assert [n["doc_id"] for n in notes] == ["doc1"]

    def test_list_study_notes_without_doc_id_returns_all_for_notebook(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="physics", origin="doc2.pdf",
                        normalized_path="corpus/physics/doc2.md")
        store.replace_study_notes("physics", "doc1", [{"text": "doc1の学び"}])
        store.replace_study_notes("physics", "doc2", [{"text": "doc2の学び"}])

        notes = store.list_study_notes("physics")

        assert {n["doc_id"] for n in notes} == {"doc1", "doc2"}

    def test_list_study_notes_returns_empty_when_none_exist(self, store):
        _make_notebook(store, name="physics")

        assert store.list_study_notes("physics") == []


class TestLoadVectors:
    def test_returns_empty_for_notebook_with_no_chunks(self, store):
        _make_notebook(store, name="physics")

        ids, matrix = store.load_vectors("physics")

        assert ids == []
        assert matrix.shape == (0, 0)

    def test_restores_float32_matrix_matching_dim(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        vec = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
        store.upsert_chunks([_chunk_row(id_="doc1#0", embedding=vec)])

        ids, matrix = store.load_vectors("physics")

        assert ids == ["doc1#0"]
        assert matrix.dtype == np.float32
        assert matrix.shape == (1, 4)
        np.testing.assert_allclose(matrix[0], vec)

    def test_filters_by_notebook(self, store):
        _make_notebook(store, name="physics")
        _make_notebook(store, name="math")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="math", origin="doc2.pdf",
                        normalized_path="corpus/math/doc2.md")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", notebook="physics", doc_id="doc1"),
             _chunk_row(id_="doc2#0", notebook="math", doc_id="doc2",
                        source_path="corpus/math/doc2.md")]
        )

        ids, matrix = store.load_vectors("physics")

        assert ids == ["doc1#0"]
        assert matrix.shape == (1, 4)

    def test_second_call_without_writes_returns_cached_objects(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks([_chunk_row(id_="doc1#0")])

        ids1, matrix1 = store.load_vectors("physics")
        ids2, matrix2 = store.load_vectors("physics")

        # generation が変わっていなければ SQLite に再クエリせず同一オブジェクトを返す。
        assert ids2 is ids1
        assert matrix2 is matrix1

    def test_write_after_load_invalidates_cache(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.upsert_chunks([_chunk_row(id_="doc1#0")])
        ids1, matrix1 = store.load_vectors("physics")

        store.upsert_chunks([_chunk_row(id_="doc1#1", seq=1)])
        ids2, matrix2 = store.load_vectors("physics")

        assert ids2 is not ids1
        assert ids2 == ["doc1#0", "doc1#1"]
        assert matrix2.shape == (2, 4)


class TestFileState:
    def test_get_file_state_returns_none_when_absent(self, store):
        assert store.get_file_state("a.md") is None

    def test_set_then_get_file_state_round_trips(self, store):
        store.set_file_state("a.md", mtime=123.456, size=789, model="fake-model")

        state = store.get_file_state("a.md")

        assert state == {"mtime": 123.456, "size": 789, "model": "fake-model"}

    def test_set_file_state_upserts_existing_entry(self, store):
        store.set_file_state("a.md", mtime=1.0, size=10, model="fake-model")
        store.set_file_state("a.md", mtime=2.0, size=20, model="fake-model")

        assert store.get_file_state("a.md") == {"mtime": 2.0, "size": 20, "model": "fake-model"}

    def test_delete_file_state_removes_entry(self, store):
        store.set_file_state("a.md", mtime=1.0, size=10, model="fake-model")

        store.delete_file_state("a.md")

        assert store.get_file_state("a.md") is None

    def test_delete_file_state_is_idempotent_when_absent(self, store):
        # 存在しない source_file を消してもエラーにしない(呼び出し側が存在確認を
        # 事前に行わずとも安全に使えるようにするため)。
        store.delete_file_state("does-not-exist.md")

        assert store.get_file_state("does-not-exist.md") is None


class TestPrune:
    def test_prune_missing_removes_chunks_and_file_state_not_in_set(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        _make_document(store, id_="doc2", notebook="physics", origin="doc2.pdf",
                       normalized_path="corpus/physics/doc2.md")
        store.upsert_chunks(
            [_chunk_row(id_="doc1#0", doc_id="doc1", source_path="corpus/physics/doc1.md"),
             _chunk_row(id_="doc2#0", doc_id="doc2", source_path="corpus/physics/doc2.md")]
        )
        store.set_file_state("corpus/physics/doc1.md", mtime=1.0, size=1, model="m")
        store.set_file_state("corpus/physics/doc2.md", mtime=1.0, size=1, model="m")

        pruned = store.prune_missing({"corpus/physics/doc2.md"})

        assert pruned == 1
        ids, _ = store.load_vectors("physics")
        assert ids == ["doc2#0"]
        assert store.get_file_state("corpus/physics/doc1.md") is None
        assert store.get_file_state("corpus/physics/doc2.md") is not None

    def test_prune_missing_returns_zero_when_nothing_stale(self, store):
        store.set_file_state("a.md", mtime=1.0, size=1, model="m")
        assert store.prune_missing({"a.md"}) == 0


class TestFindDocumentsByOrigin:
    """`shelf shelve` の既投入 origin スキップ判定が使う横断検索
    （docs/design-shelf-reference-service.md §13.7・§13.10 V3）。notebook を
    引数に取らず全 notebook を横断する点が list_documents との違い。
    """

    def test_returns_hit_across_all_notebooks(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics", origin="feynman.pdf")

        hits = store.find_documents_by_origin("feynman.pdf")

        assert [h["id"] for h in hits] == ["doc1"]
        assert [h["notebook"] for h in hits] == ["physics"]

    def test_returns_empty_list_when_origin_not_ingested(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics", origin="feynman.pdf")

        assert store.find_documents_by_origin("not-ingested.pdf") == []

    def test_returns_multiple_hits_when_same_origin_in_different_notebooks(self, store):
        _make_notebook(store, name="physics")
        _make_notebook(store, name="math")
        _make_document(
            store, id_="doc1", notebook="physics", origin="shared.pdf",
            normalized_path="corpus/physics/doc1.md",
        )
        _make_document(
            store, id_="doc2", notebook="math", origin="shared.pdf",
            normalized_path="corpus/math/doc2.md",
        )

        hits = store.find_documents_by_origin("shared.pdf")

        assert {(h["id"], h["notebook"]) for h in hits} == {
            ("doc1", "physics"), ("doc2", "math"),
        }


class TestMeta:
    def test_get_meta_returns_none_when_absent(self, store):
        assert store.get_meta("model") is None

    def test_set_then_get_meta_round_trips(self, store):
        store.set_meta("model", "fake-model")
        assert store.get_meta("model") == "fake-model"

    def test_set_meta_overwrites_existing_value(self, store):
        store.set_meta("model", "old")
        store.set_meta("model", "new")
        assert store.get_meta("model") == "new"


class TestStudyNotesGrounding:
    """学びノートのチャンク接地（section/page/source_chunk_ids）とパイプライン版数。

    「学び抽出の全文グラウンディング改良」第1ステップ（永続層のみ）。
    """

    def test_replace_study_notes_round_trips_grounding_fields(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes(
            "physics",
            "doc1",
            [
                {
                    "text": "学び1",
                    "section": "§3.2",
                    "page": 42,
                    "source_chunk_ids": ["physics/doc1#3", "physics/doc1#5"],
                    "pipeline": 2,
                }
            ],
        )

        note = store.list_study_notes("physics", "doc1")[0]
        assert note["section"] == "§3.2"
        assert note["page"] == 42
        assert note["source_chunk_ids"] == ["physics/doc1#3", "physics/doc1#5"]
        assert note["pipeline"] == 2

    def test_replace_study_notes_defaults_pipeline_to_1_when_omitted(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes("physics", "doc1", [{"text": "学び1"}])

        note = store.list_study_notes("physics", "doc1")[0]
        assert note["pipeline"] == 1

    def test_replace_study_notes_defaults_pipeline_to_1_when_explicitly_none(self, store):
        # コードレビュー指摘#4: note.get("pipeline", 1) は明示的な None を素通しし、
        # pipeline 列は NOT NULL 制約なので IntegrityError になっていた。
        # source_chunk_ids と同じ「is not None」パターンに揃え、None を渡した場合も
        # 省略時と同じ既定値1にフォールバックする。
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes("physics", "doc1", [{"text": "学び1", "pipeline": None}])

        note = store.list_study_notes("physics", "doc1")[0]
        assert note["pipeline"] == 1

    def test_replace_study_notes_with_legacy_dict_without_new_keys_still_works(self, store):
        # service.py の既存呼び出し元は section/page/source_chunk_ids/pipeline を
        # 渡さない（旧形式 dict）。互換性が壊れていないことを確認する。
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")

        store.replace_study_notes(
            "physics",
            "doc1",
            [{"text": "学び1", "source_span": "§1", "source_hash": "h1", "model": "qwen3:8b"}],
        )

        note = store.list_study_notes("physics", "doc1")[0]
        assert note["text"] == "学び1"
        assert note["section"] is None
        assert note["page"] is None
        assert note["source_chunk_ids"] is None
        assert note["pipeline"] == 1

    def test_list_study_notes_source_chunk_ids_defaults_to_none_when_absent(self, store):
        _make_notebook(store, name="physics")
        _make_document(store, id_="doc1", notebook="physics")
        store.replace_study_notes("physics", "doc1", [{"text": "学び1"}])

        note = store.list_study_notes("physics", "doc1")[0]

        assert note["source_chunk_ids"] is None


class TestStudyNotesGroundingSchemaMigration:
    """旧スキーマ DB（study_notes に section/page/source_chunk_ids/pipeline 列を
    欠く）を開いた際の冪等マイグレーションを検証する。
    """

    def _create_legacy_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE notebooks (
              name        TEXT PRIMARY KEY,
              description TEXT,
              backend     TEXT NOT NULL DEFAULT 'codex',
              created_at  TEXT NOT NULL,
              persona     TEXT
            );
            CREATE TABLE documents (
              id              TEXT PRIMARY KEY,
              notebook        TEXT NOT NULL REFERENCES notebooks(name),
              origin          TEXT NOT NULL,
              origin_type     TEXT NOT NULL,
              normalized_path TEXT NOT NULL,
              title           TEXT,
              converter       TEXT NOT NULL,
              content_hash    TEXT,
              added_at        TEXT NOT NULL,
              fetched_at      TEXT,
              description        TEXT,
              description_source TEXT,
              UNIQUE(notebook, origin)
            );
            CREATE TABLE study_notes (
              id          TEXT PRIMARY KEY,
              notebook    TEXT NOT NULL,
              doc_id      TEXT NOT NULL,
              seq         INTEGER NOT NULL,
              text        TEXT NOT NULL,
              source_span TEXT,
              source_hash TEXT,
              model       TEXT,
              created_at  TEXT NOT NULL,
              UNIQUE(notebook, doc_id, seq)
            );
            INSERT INTO notebooks (name, description, backend, created_at)
                VALUES ('physics', '物理の論文', 'codex', '2026-01-01T00:00:00Z');
            INSERT INTO documents
                (id, notebook, origin, origin_type, normalized_path, converter, added_at)
                VALUES ('doc1', 'physics', 'feynman.pdf', 'pdf', 'corpus/physics/doc1.md',
                        'pymupdf4llm', '2026-01-01T00:00:00Z');
            INSERT INTO study_notes
                (id, notebook, doc_id, seq, text, created_at)
                VALUES ('physics/doc1#d0', 'physics', 'doc1', 0, '既存の学び',
                        '2026-01-01T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

    def test_init_migrates_columns_and_existing_rows_default_pipeline_to_1(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store = Store(db_path)
        try:
            note = store.list_study_notes("physics", "doc1")[0]
            assert note["text"] == "既存の学び"
            assert note["section"] is None
            assert note["page"] is None
            assert note["source_chunk_ids"] is None
            # ALTER TABLE ... DEFAULT 1 は既存行にも遡って値を入れる。
            assert note["pipeline"] == 1
        finally:
            store.close()

    def test_init_migration_is_idempotent_across_two_opens(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        store1 = Store(db_path)
        store1.close()
        store2 = Store(db_path)  # 2回目の open で ALTER TABLE の重複エラーが出ないことを確認
        try:
            note = store2.list_study_notes("physics", "doc1")[0]
            assert note["pipeline"] == 1
            store2.replace_study_notes(
                "physics", "doc1", [{"text": "新しい学び", "pipeline": 2}]
            )
            assert store2.list_study_notes("physics", "doc1")[0]["pipeline"] == 2
        finally:
            store2.close()
