"""shelver.py（Shelver クラス）の配線テスト（設計書 §13.1 決定 1 / §13.9）。

FakeAnswerBackend のみを外部境界とし、実 DB・実プロセス・ネットワークには
一切触れない。plan() の配線（prompt 構成・backend 呼び出し・parse・classify_step
委譲）・増分カタログスレッディング（this-run で作成した notebook が後続ファイルの
プロンプトに反映されること）・backend 呼び出し失敗時の継続（当該ファイルのみ
errors に落として処理を止めない）を検証する（librarian.py の test_librarian.py と
同型のテスト構成）。
"""
from __future__ import annotations

from pathlib import Path

from shelf.ports import FileSummary, NotebookCard, RawAnswer
from shelf.shelver import Shelver
from shelf.shelving import CLASSIFY_SCHEMA

from tests.fakes import FakeAnswerBackend


def _card(**overrides) -> NotebookCard:
    base = dict(name="physics", description="物理学の教材集", persona=None, doc_count=3)
    base.update(overrides)
    return NotebookCard(**base)


def _summary(**overrides) -> FileSummary:
    base = dict(origin="/abs/dir/note.md", summary="ニュートン力学の講義ノート")
    base.update(overrides)
    return FileSummary(**base)


_ASSIGN_PHYSICS = '{"action": "assign", "notebook": "physics", "reason": "主題が一致"}'
_NEW_COOKING = (
    '{"action": "new", "notebook": "cooking-recipes", '
    '"description": "料理レシピ集", "reason": "既存に合致なし"}'
)


class TestPlanWiring:
    def test_calls_backend_with_classification_prompt_and_schema(self):
        backend = FakeAnswerBackend(canned=_ASSIGN_PHYSICS)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        shelver.plan([_summary()], [_card()])

        assert len(backend.calls) == 1
        call = backend.calls[0]
        assert "ニュートン力学の講義ノート" in call["prompt"]
        assert call["schema"] == CLASSIFY_SCHEMA
        assert call["workdir"] == Path("/corpus")

    def test_calls_backend_once_per_summary(self):
        backend = FakeAnswerBackend(canned=_ASSIGN_PHYSICS)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], [_card()]
        )

        assert len(backend.calls) == 2

    def test_empty_summaries_does_not_call_backend(self):
        backend = FakeAnswerBackend(canned=_ASSIGN_PHYSICS)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan([], [_card()])

        assert backend.calls == []
        assert result.assignments == []
        assert result.created == []


class TestPlanHappyPathAssign:
    def test_assigns_to_existing_notebook(self):
        backend = FakeAnswerBackend(canned=_ASSIGN_PHYSICS)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan([_summary()], [_card(name="physics")])

        assert len(result.assignments) == 1
        assert result.assignments[0].notebook == "physics"
        assert result.assignments[0].new_notebook is False
        assert result.created == []
        assert result.errors == []


class TestPlanHappyPathNewNotebook:
    def test_creates_new_notebook_with_configured_backend(self):
        backend = FakeAnswerBackend(canned=_NEW_COOKING)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan([_summary()], [_card(name="physics")])

        assert len(result.created) == 1
        assert result.created[0].name == "cooking-recipes"
        assert result.created[0].description == "料理レシピ集"
        assert result.created[0].backend == "ollama"
        assert result.assignments[0].notebook == "cooking-recipes"
        assert result.assignments[0].new_notebook is True

    def test_notebook_backend_is_threaded_into_created_spec(self):
        """notebook_backend はコンストラクタで受け取り、classify_step へ都度渡す
        （設計書申し送り: backend 文字列は Shelver が受け取り classify_step へ渡す）。"""
        backend = FakeAnswerBackend(canned=_NEW_COOKING)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="codex")

        result = shelver.plan([_summary()], [])

        assert result.created[0].backend == "codex"


class TestPlanIncrementalCatalogThreading:
    """増分カタログスレッディングの証明（設計書 §13.9 done-criteria）。"""

    def test_second_file_prompt_includes_notebook_created_by_first_file(self):
        backend = FakeAnswerBackend(canned=[_NEW_COOKING, _ASSIGN_PHYSICS])
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], []
        )

        assert len(backend.calls) == 2
        first_prompt = backend.calls[0]["prompt"]
        second_prompt = backend.calls[1]["prompt"]
        assert "cooking-recipes" not in first_prompt
        assert "cooking-recipes" in second_prompt

    def test_second_file_can_assign_to_notebook_created_by_first_file(self):
        second_assign = (
            '{"action": "assign", "notebook": "cooking-recipes", "reason": "同一主題"}'
        )
        backend = FakeAnswerBackend(canned=[_NEW_COOKING, second_assign])
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], []
        )

        assert len(result.created) == 1
        assert result.assignments[1].notebook == "cooking-recipes"
        assert result.assignments[1].new_notebook is False

    def test_initial_catalog_is_not_mutated(self):
        """working カタログは catalog のコピーであり、呼び出し元のリストを汚染しない。"""
        backend = FakeAnswerBackend(canned=_NEW_COOKING)
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")
        initial_catalog = [_card(name="physics")]

        shelver.plan([_summary()], initial_catalog)

        assert initial_catalog == [_card(name="physics")]


class TestPlanBackendFailureContinues:
    """backend.answer() 自体の失敗（RawAnswer.ok=False・例外）のみ errors に落とし、
    処理を継続する（parse 失敗は classify_step が新規作成へ吸収済みのため対象外・
    設計書申し送り §13.9）。"""

    def test_ok_false_is_recorded_as_error_and_produces_no_assignment(self):
        backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="timeout"))
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan([_summary(origin="/a.md")], [_card()])

        assert result.assignments == []
        assert result.created == []
        assert len(result.errors) == 1
        assert result.errors[0]["origin"] == "/a.md"
        assert result.errors[0]["error"] == "timeout"

    def test_failure_on_one_file_does_not_block_subsequent_files(self):
        backend = FakeAnswerBackend(
            canned=[RawAnswer(text="", ok=False, error="timeout"), _ASSIGN_PHYSICS]
        )
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], [_card(name="physics")]
        )

        assert len(result.errors) == 1
        assert result.errors[0]["origin"] == "/a.md"
        assert len(result.assignments) == 1
        assert result.assignments[0].origin == "/b.md"

    def test_backend_exception_is_recorded_as_error_and_processing_continues(self):
        backend = FakeAnswerBackend(canned=[RuntimeError("接続が切れました"), _ASSIGN_PHYSICS])
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], [_card(name="physics")]
        )

        assert len(result.errors) == 1
        assert result.errors[0]["origin"] == "/a.md"
        assert "接続が切れました" in result.errors[0]["error"]
        assert len(result.assignments) == 1
        assert result.assignments[0].origin == "/b.md"


class TestPlanParseFailureIsAbsorbedNotErrored:
    """parse 失敗は classify_step の安全側フォールバックで必ず新規作成へ再解釈される
    ため、Shelver は errors に落とさない（backend.answer() 自体は成功している）。"""

    def test_malformed_json_becomes_new_notebook_assignment_not_error(self):
        backend = FakeAnswerBackend(canned="これはJSONではない壊れたテキスト")
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan([_summary(origin="/a.md")], [])

        assert result.errors == []
        assert len(result.assignments) == 1
        assert result.assignments[0].new_notebook is True
        assert len(result.created) == 1


class TestPlanAggregation:
    def test_returns_shelve_plan_with_assignments_and_created_across_multiple_files(self):
        backend = FakeAnswerBackend(canned=[_NEW_COOKING, _ASSIGN_PHYSICS])
        shelver = Shelver(backend, workdir=Path("/corpus"), notebook_backend="ollama")

        result = shelver.plan(
            [_summary(origin="/a.md"), _summary(origin="/b.md")], [_card(name="physics")]
        )

        assert len(result.assignments) == 2
        assert len(result.created) == 1
        assert {a.origin for a in result.assignments} == {"/a.md", "/b.md"}
