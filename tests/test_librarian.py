"""librarian.py（Librarian クラス）の配線テスト（設計書 §6-D / §9-A）。

FakeAnswerBackend のみを外部境界とし、実 DB・実プロセス・ネットワークには
一切触れない。route() の配線（prompt 構成・backend 呼び出し・パース・
apply_fallback 委譲）と、backend 呼び出し失敗時の安全側フォールバックを検証する。
"""
from __future__ import annotations

from pathlib import Path

from shelf.librarian import Librarian
from shelf.ports import NotebookCard, RawAnswer, RouteTarget
from shelf.routing import ROUTING_SCHEMA

from tests.fakes import FakeAnswerBackend


def _card(**overrides) -> NotebookCard:
    base = dict(
        name="quantum-mechanics",
        description="量子力学の教材集",
        persona="量子力学の専門家",
        doc_count=3,
    )
    base.update(overrides)
    return NotebookCard(**base)


class TestRouteWiring:
    def test_calls_backend_with_routing_prompt_and_schema(self):
        backend = FakeAnswerBackend(canned='{"answerable": true, "targets": []}')
        librarian = Librarian(
            backend, workdir=Path("/corpus"), top_n=1, fallback="conservative"
        )

        librarian.route("スピンとは何ですか", [_card()])

        assert len(backend.calls) == 1
        call = backend.calls[0]
        assert "スピンとは何ですか" in call["prompt"]
        assert call["schema"] == ROUTING_SCHEMA
        assert call["workdir"] == Path("/corpus")

    def test_calls_backend_exactly_once_even_with_empty_catalog(self):
        """厚くしすぎない配線: 空カタログの特別扱いは routing.apply_fallback 側の
        責務（分岐1）であり、Librarian 自身は分岐せず素直に backend を呼ぶ。"""
        backend = FakeAnswerBackend(canned='{"answerable": true, "targets": []}')
        librarian = Librarian(backend, workdir=Path("/corpus"), top_n=1, fallback="all")

        result = librarian.route("質問", [])

        assert len(backend.calls) == 1
        assert result == []


class TestRouteHappyPath:
    def test_returns_targets_from_successful_routing(self):
        canned = (
            '{"answerable": true, "targets": [{"notebook": "quantum-mechanics", '
            '"score": 0.9, "subquery": "スピン角運動量", "reason": "主題が一致"}]}'
        )
        backend = FakeAnswerBackend(canned=canned)
        librarian = Librarian(
            backend, workdir=Path("/corpus"), top_n=1, fallback="conservative"
        )

        result = librarian.route("スピンとは何ですか", [_card(name="quantum-mechanics")])

        assert result == [
            RouteTarget(
                notebook="quantum-mechanics",
                score=0.9,
                subquery="スピン角運動量",
                reason="主題が一致",
            )
        ]

    def test_not_answerable_returns_no_targets_without_calling_expert(self):
        backend = FakeAnswerBackend(canned='{"answerable": false, "targets": []}')
        librarian = Librarian(backend, workdir=Path("/corpus"), top_n=1, fallback="all")

        result = librarian.route("質問", [_card()])

        assert result == []

    def test_hallucinated_notebook_names_are_filtered_via_apply_fallback(self):
        canned = (
            '{"answerable": true, "targets": ['
            '{"notebook": "real-notebook", "score": 0.9, "subquery": "q", "reason": "r"}, '
            '{"notebook": "hallucinated-notebook", "score": 0.99, "subquery": "q", "reason": "r"}'
            "]}"
        )
        backend = FakeAnswerBackend(canned=canned)
        librarian = Librarian(
            backend, workdir=Path("/corpus"), top_n=2, fallback="conservative"
        )

        result = librarian.route("質問", [_card(name="real-notebook")])

        assert [t.notebook for t in result] == ["real-notebook"]

    def test_top_n_is_threaded_through_to_apply_fallback(self):
        canned = (
            '{"answerable": true, "targets": ['
            '{"notebook": "nb-a", "score": 0.5, "subquery": "q", "reason": "r"}, '
            '{"notebook": "nb-b", "score": 0.9, "subquery": "q", "reason": "r"}'
            "]}"
        )
        backend = FakeAnswerBackend(canned=canned)
        librarian = Librarian(
            backend, workdir=Path("/corpus"), top_n=1, fallback="conservative"
        )

        result = librarian.route(
            "質問", [_card(name="nb-a"), _card(name="nb-b")]
        )

        assert [t.notebook for t in result] == ["nb-b"]


class TestRouteBackendFailure:
    """backend 呼び出し失敗（RawAnswer.ok=False）はエラーで潰さず、パース失敗
    （parse_ok=False）と同じフォールバック経路（routing.apply_fallback 分岐3）へ流す
    （設計書 §6-D・タスク申し送り事項）。"""

    def test_backend_failure_with_conservative_fallback_returns_no_targets(self):
        backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="timeout"))
        librarian = Librarian(
            backend, workdir=Path("/corpus"), top_n=1, fallback="conservative"
        )

        result = librarian.route("質問", [_card()])

        assert result == []

    def test_backend_failure_with_all_fallback_routes_across_catalog(self):
        backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="500"))
        librarian = Librarian(backend, workdir=Path("/corpus"), top_n=2, fallback="all")
        cards = [_card(name="nb-a"), _card(name="nb-b")]

        result = librarian.route("元の質問", cards)

        assert [t.notebook for t in result] == ["nb-a", "nb-b"]
        assert all(t.subquery == "元の質問" for t in result)

    def test_backend_failure_does_not_raise(self):
        backend = FakeAnswerBackend(canned=RawAnswer(text="", ok=False, error="boom"))
        librarian = Librarian(backend, workdir=Path("/corpus"), top_n=1, fallback="all")

        # 例外を送出しないこと自体がテスト対象（呼び出せれば成功）。
        librarian.route("質問", [_card()])


class TestRouteMalformedResponse:
    def test_malformed_json_returns_no_targets_even_with_all_fallback(self):
        """backend 呼び出し自体は成功 (ok=True) したが応答が JSON として解釈でき
        ない場合、parse_routing は answerable=False も同時に立てる（routing.py
        の実装）。これは apply_fallback 分岐2（専門家を絶対に呼ばない）に合流する
        ため、fallback="all" でも常に対象ゼロになる。backend 呼び出し自体が失敗
        （ok=False）した場合の安全側フォールバック（TestRouteBackendFailure）とは
        非対称であることに注意（Librarian は backend 失敗のみ answerable=True を
        補って parse 失敗経路（分岐3）へ流す）。"""
        backend = FakeAnswerBackend(canned="これはJSONではない壊れたテキスト")
        librarian = Librarian(backend, workdir=Path("/corpus"), top_n=1, fallback="all")

        result = librarian.route("質問", [_card(name="nb-a")])

        assert result == []
