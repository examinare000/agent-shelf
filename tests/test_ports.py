"""ports.py の中立 DTO テスト。frozen dataclass の構築・既定値・不変性のみを検証する
（外部依存ゼロ・ネットワーク/実DB/実プロセス不使用）。
"""
from __future__ import annotations

import dataclasses

import pytest

from shelf.ports import (
    ClassificationDecision,
    FileSummary,
    NewNotebookSpec,
    NotebookCard,
    RetrievedChunk,
    RouteTarget,
    RoutingDecision,
    ShelfAssignment,
    ShelvePlan,
    StudyNote,
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


class TestRetrievedChunkKind:
    def test_kind_defaults_to_body_for_existing_call_sites(self):
        """既存呼び出し箇所（service._load_chunks 等）は kind を渡さない。
        後方互換のため既定値 'body' が付くことを固定する（design §10 R1 検証観点）。
        """
        chunk = _chunk()

        assert chunk.kind == "body"

    def test_kind_can_be_set_explicitly_to_digest(self):
        chunk = _chunk(kind="digest")

        assert chunk.kind == "digest"

    def test_kind_can_be_set_explicitly_to_summary(self):
        chunk = _chunk(kind="summary")

        assert chunk.kind == "summary"

    def test_is_frozen(self):
        chunk = _chunk()

        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.kind = "digest"


class TestNotebookCard:
    def test_constructs_with_name_description_persona_doc_count(self):
        card = NotebookCard(
            name="quantum-mechanics",
            description="量子力学の教材集",
            persona="量子力学の専門家",
            doc_count=3,
        )

        assert card.name == "quantum-mechanics"
        assert card.description == "量子力学の教材集"
        assert card.persona == "量子力学の専門家"
        assert card.doc_count == 3

    def test_description_and_persona_accept_none(self):
        card = NotebookCard(name="nb", description=None, persona=None, doc_count=0)

        assert card.description is None
        assert card.persona is None

    def test_is_frozen(self):
        card = NotebookCard(name="nb", description=None, persona=None, doc_count=0)

        with pytest.raises(dataclasses.FrozenInstanceError):
            card.name = "other"

    def test_tags_defaults_to_empty_tuple(self):
        """既存呼び出し箇所（旧 _build_catalog）は tags を渡さない。
        空タプル既定で後方互換を保つ（additive 拡張）。
        """
        card = NotebookCard(name="nb", description=None, persona=None, doc_count=0)

        assert card.tags == ()

    def test_tags_can_be_set_explicitly(self):
        card = NotebookCard(
            name="nb", description=None, persona=None, doc_count=0,
            tags=("量子力学", "スピン"),
        )

        assert card.tags == ("量子力学", "スピン")


class TestRouteTarget:
    def test_constructs_with_notebook_score_subquery_reason(self):
        target = RouteTarget(
            notebook="quantum-mechanics",
            score=0.9,
            subquery="スピン角運動量の交換関係",
            reason="スピンは量子力学ノートの主題",
        )

        assert target.notebook == "quantum-mechanics"
        assert target.score == 0.9
        assert target.subquery == "スピン角運動量の交換関係"
        assert target.reason == "スピンは量子力学ノートの主題"

    def test_is_frozen(self):
        target = RouteTarget(notebook="nb", score=0.5, subquery="q", reason="r")

        with pytest.raises(dataclasses.FrozenInstanceError):
            target.score = 0.1


class TestRoutingDecision:
    def test_constructs_with_answerable_targets_parse_ok(self):
        target = RouteTarget(notebook="nb", score=0.9, subquery="q", reason="r")

        decision = RoutingDecision(answerable=True, targets=[target], parse_ok=True)

        assert decision.answerable is True
        assert decision.targets == [target]
        assert decision.parse_ok is True

    def test_targets_defaults_to_empty_list(self):
        decision = RoutingDecision(answerable=False, parse_ok=False)

        assert decision.targets == []

    def test_default_targets_are_independent_between_instances(self):
        """mutable default (list) が dataclass インスタンス間で共有されないことを保証する。"""
        first = RoutingDecision(answerable=False, parse_ok=False)
        second = RoutingDecision(answerable=False, parse_ok=False)

        first.targets.append(
            RouteTarget(notebook="nb", score=0.1, subquery="q", reason="r")
        )

        assert second.targets == []

    def test_is_frozen(self):
        decision = RoutingDecision(answerable=True, parse_ok=True)

        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.answerable = False


class TestStudyNote:
    def test_constructs_with_text_and_span(self):
        note = StudyNote(text="スピンは角運動量の一種である", span="§2.1")

        assert note.text == "スピンは角運動量の一種である"
        assert note.span == "§2.1"

    def test_span_defaults_to_none(self):
        note = StudyNote(text="学び本文")

        assert note.span is None

    def test_is_frozen(self):
        note = StudyNote(text="学び本文")

        with pytest.raises(dataclasses.FrozenInstanceError):
            note.text = "別の本文"

    def test_chunk_ids_defaults_to_empty_tuple(self):
        """span のみを組み立てる呼び出し箇所（chunk_ids 未対応の場面）は
        chunk_ids を渡さない。空タプル既定で後方互換を保つ（additive 拡張）。
        """
        note = StudyNote(text="学び本文")

        assert note.chunk_ids == ()

    def test_chunk_ids_can_be_set_explicitly(self):
        note = StudyNote(text="学び本文", chunk_ids=("nb/doc#0", "nb/doc#1"))

        assert note.chunk_ids == ("nb/doc#0", "nb/doc#1")

    def test_section_defaults_to_none(self):
        note = StudyNote(text="学び本文")

        assert note.section is None

    def test_section_can_be_set_explicitly(self):
        note = StudyNote(text="学び本文", section="§2.1")

        assert note.section == "§2.1"

    def test_page_defaults_to_none(self):
        note = StudyNote(text="学び本文")

        assert note.page is None

    def test_page_can_be_set_explicitly(self):
        note = StudyNote(text="学び本文", page=42)

        assert note.page == 42


class TestFileSummary:
    def test_constructs_with_origin_and_summary(self):
        summary = FileSummary(origin="/abs/a.pdf", summary="量子力学の講義ノート")

        assert summary.origin == "/abs/a.pdf"
        assert summary.summary == "量子力学の講義ノート"

    def test_is_frozen(self):
        summary = FileSummary(origin="/abs/a.pdf", summary="要約")

        with pytest.raises(dataclasses.FrozenInstanceError):
            summary.summary = "別の要約"


class TestClassificationDecision:
    def test_constructs_with_assign_action(self):
        decision = ClassificationDecision(
            action="assign",
            notebook="quantum-mechanics",
            reason="スピンは量子力学ノートの主題",
            parse_ok=True,
        )

        assert decision.action == "assign"
        assert decision.notebook == "quantum-mechanics"
        assert decision.reason == "スピンは量子力学ノートの主題"
        assert decision.parse_ok is True

    def test_description_defaults_to_none_for_assign(self):
        """CLASSIFY_SCHEMA は description を required に含めない
        （§13.4: assign では不要）。既定 None で assign 判断を組み立てられることを固定する。
        """
        decision = ClassificationDecision(
            action="assign", notebook="nb", reason="r", parse_ok=True
        )

        assert decision.description is None

    def test_description_can_be_set_for_new(self):
        decision = ClassificationDecision(
            action="new",
            notebook="cooking-recipes",
            reason="既存 notebook に合致なし",
            parse_ok=True,
            description="料理レシピ集",
        )

        assert decision.description == "料理レシピ集"

    def test_parse_ok_false_represents_unparseable_response(self):
        """不正 JSON・非 dict・enum 外などのパース失敗を parse_ok=False で表す
        （RoutingDecision.parse_ok と同じ安全側フォールバック契機）。
        """
        decision = ClassificationDecision(
            action="", notebook="", reason="", parse_ok=False
        )

        assert decision.parse_ok is False

    def test_is_frozen(self):
        decision = ClassificationDecision(
            action="assign", notebook="nb", reason="r", parse_ok=True
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.action = "new"


class TestShelfAssignment:
    def test_constructs_resolved_entry(self):
        assignment = ShelfAssignment(
            origin="/abs/a.pdf",
            notebook="quantum-mechanics",
            new_notebook=False,
            summary="量子力学の講義ノート",
            reason="既存 notebook に合致",
        )

        assert assignment.origin == "/abs/a.pdf"
        assert assignment.notebook == "quantum-mechanics"
        assert assignment.new_notebook is False
        assert assignment.summary == "量子力学の講義ノート"
        assert assignment.reason == "既存 notebook に合致"

    def test_new_notebook_true_for_created_notebook(self):
        assignment = ShelfAssignment(
            origin="/abs/b.md",
            notebook="cooking-recipes",
            new_notebook=True,
            summary="料理レシピ集",
            reason="既存 notebook に合致なし",
        )

        assert assignment.new_notebook is True

    def test_is_frozen(self):
        assignment = ShelfAssignment(
            origin="/abs/a.pdf",
            notebook="nb",
            new_notebook=False,
            summary="s",
            reason="r",
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            assignment.notebook = "other"


class TestNewNotebookSpec:
    def test_constructs_with_name_description_backend(self):
        spec = NewNotebookSpec(
            name="cooking-recipes", description="料理レシピ集", backend="ollama"
        )

        assert spec.name == "cooking-recipes"
        assert spec.description == "料理レシピ集"
        assert spec.backend == "ollama"

    def test_is_frozen(self):
        spec = NewNotebookSpec(name="nb", description="d", backend="ollama")

        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.backend = "codex"


class TestShelvePlan:
    def test_constructs_with_assignments_created_skipped_errors(self):
        assignment = ShelfAssignment(
            origin="/abs/a.pdf",
            notebook="quantum-mechanics",
            new_notebook=False,
            summary="s",
            reason="r",
        )
        spec = NewNotebookSpec(name="nb", description="d", backend="ollama")

        plan = ShelvePlan(
            assignments=[assignment],
            created=[spec],
            skipped=[{"origin": "/abs/c.png", "reason": "未対応の形式です"}],
            errors=[{"origin": "/abs/e.pdf", "error": "テキストを抽出できませんでした"}],
        )

        assert plan.assignments == [assignment]
        assert plan.created == [spec]
        assert plan.skipped == [{"origin": "/abs/c.png", "reason": "未対応の形式です"}]
        assert plan.errors == [
            {"origin": "/abs/e.pdf", "error": "テキストを抽出できませんでした"}
        ]

    def test_all_fields_default_to_empty_list(self):
        plan = ShelvePlan()

        assert plan.assignments == []
        assert plan.created == []
        assert plan.skipped == []
        assert plan.errors == []

    def test_default_lists_are_independent_between_instances(self):
        """mutable default (list) が dataclass インスタンス間で共有されないことを保証する
        （RoutingDecision.targets と同じ回帰ガード）。"""
        first = ShelvePlan()
        second = ShelvePlan()

        first.assignments.append(
            ShelfAssignment(
                origin="/abs/a.pdf",
                notebook="nb",
                new_notebook=False,
                summary="s",
                reason="r",
            )
        )
        first.created.append(NewNotebookSpec(name="nb", description="d", backend="ollama"))
        first.skipped.append({"origin": "x", "reason": "y"})
        first.errors.append({"origin": "x", "error": "y"})

        assert second.assignments == []
        assert second.created == []
        assert second.skipped == []
        assert second.errors == []

    def test_is_frozen(self):
        plan = ShelvePlan()

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.assignments = []
