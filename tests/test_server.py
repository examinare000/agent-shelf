"""MCP ツール ask / list_notebooks / consult の単体テスト。

FastMCP.call_tool() は (content_blocks, {"result": <戻り値>}) を返すため、
"result" 側で戻り値の型・中身を検証する（recall/tests/test_server.py と同型）。
ロジックは server.py に一切持たせず ShelfService へ委譲するだけなので、ここでは
「委譲が正しく配線されているか」「公開ツールが3つだけか」だけを検証する。
Store(":memory:") + FakeEmbedder + FakeAnswerBackend を注入し、実DB・実埋め込み
モデル・実サブスクCLIには一切触れない。
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from shelf.indexer import index_notebook
from shelf.ports import RawAnswer
from shelf.server import build_transport_security, create_server
from shelf.service import ShelfService
from shelf.store import Store
from tests.fakes import FakeAnswerBackend, FakeEmbedder

_CHUNK_TEXT = "distinctive chunk about whales"
_QUERY_TEXT = "what do whales eat?"
_KNOWN_VEC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _grounded_raw_answer() -> RawAnswer:
    payload = {
        "answer": "whales eat krill [S1]",
        "citations": [{"s": 1}],
        "confident": True,
    }
    return RawAnswer(text=json.dumps(payload), ok=True, error=None)


def _service_with_one_chunk(tmp_path: Path) -> ShelfService:
    store = Store(":memory:")
    embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
    store.create_notebook("physics", description="物理の論文", backend="codex")
    nb_dir = tmp_path / "physics"
    nb_dir.mkdir()
    (nb_dir / "doc.md").write_text(f"# Doc\n\n{_CHUNK_TEXT}\n", encoding="utf-8")
    index_notebook(tmp_path, "physics", store, embedder)
    backend = FakeAnswerBackend(canned=_grounded_raw_answer())
    return ShelfService(store, embedder, lambda name: backend, tmp_path)


def _call(server, name: str, args: dict):
    return asyncio.run(server.call_tool(name, args))


class _BlockingAnswerBackend:
    """backend.answer() を threading.Event で任意に停止させる決定論的ダブル。

    FakeAnswerBackend は即座に応答を返すため並行性の検証に使えない。ask/consult が
    バックエンド呼び出し中にイベントループを専有していないかを確かめるには、応答を
    明示的に保留できるダブルが要る。
    """

    name = "fake"

    def __init__(self, release_event: threading.Event, response: RawAnswer) -> None:
        self._release_event = release_event
        self._response = response
        self.calls: list[dict] = []

    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer:
        self.calls.append({"prompt": prompt, "workdir": workdir, "schema": schema})
        self._release_event.wait(timeout=5)
        return self._response


def _ask_result(server, args: dict) -> dict:
    """ask の戻り値注釈は design doc 通り素の `dict`。素の dict には FastMCP が
    structured output スキーマを生成しないため、call_tool は TextContent 1件のみを
    返す(list_notebooks の list[dict] とは挙動が異なる)。JSON 本文をパースして比較する。
    """
    content = _call(server, "ask", args)
    return json.loads(content[0].text)


class TestAsk:
    def test_returns_grounded_answer_with_citations(self, tmp_path):
        service = _service_with_one_chunk(tmp_path)
        server = create_server(service)

        result = _ask_result(server, {"notebook": "physics", "question": _QUERY_TEXT})

        assert result["notebook"] == "physics"
        assert result["backend"] == "codex"
        assert result["grounded"] is True
        assert result["citations"][0]["source"] == "physics/doc.md"
        # 生チャンク全文をそのまま返さない(トークン削減という目的を崩さないため)。
        assert "citations" in result and "text" not in result

    def test_unknown_notebook_returns_safe_error_dict(self, tmp_path):
        service = _service_with_one_chunk(tmp_path)
        server = create_server(service)

        result = _ask_result(server, {"notebook": "unknown", "question": "何か"})

        assert "error" in result

    def test_slow_ask_does_not_block_concurrent_list_notebooks(self, tmp_path):
        """ask がバックエンド応答待ちでもイベントループを専有せず、他ツール呼び出しを
        並行して処理できることを検証する。sync def のままだと FastMCP はイベントループ
        上で素呼びするため、backend.answer() の threading.Event.wait() がループ全体を
        止め、list_notebooks は ask の完了(Event解放)後まで一切進行できない
        (Red確認: pytest.fail に到達する)。async def + anyio.to_thread.run_sync 化後は
        ask がワーカースレッドへ逃げ、list_notebooks は ask 完了を待たず先に完了できる
        (Green)。
        """
        store = Store(":memory:")
        embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
        store.create_notebook("physics", description="物理の論文", backend="codex")
        nb_dir = tmp_path / "physics"
        nb_dir.mkdir()
        (nb_dir / "doc.md").write_text(f"# Doc\n\n{_CHUNK_TEXT}\n", encoding="utf-8")
        index_notebook(tmp_path, "physics", store, embedder)

        release_event = threading.Event()
        backend = _BlockingAnswerBackend(release_event, _grounded_raw_answer())
        service = ShelfService(store, embedder, lambda name: backend, tmp_path)
        server = create_server(service)

        async def scenario() -> None:
            ask_task = asyncio.create_task(
                server.call_tool("ask", {"notebook": "physics", "question": _QUERY_TEXT})
            )
            # ask_task が backend.answer() の Event.wait() まで進む猶予を与える。
            await asyncio.sleep(0.05)

            list_result = await asyncio.wait_for(server.call_tool("list_notebooks", {}), timeout=2.0)
            assert list_result is not None
            # list_notebooks が完了した時点で ask はまだ backend 応答待ちのはず。
            assert not ask_task.done()

            release_event.set()
            await asyncio.wait_for(ask_task, timeout=2.0)

        # sync 実装ではイベントループ自体が backend.answer() の Event.wait() でブロック
        # されるため、上記 scenario 内の asyncio.wait_for はタイムアウトすら発火できない
        # (タイマー自体もそのイベントループに依存するため)。テストプロセスを永久に
        # 止めないよう、scenario の実行を別スレッドに切り出し、外側から join タイムアウト
        # で観測する。
        outcome: dict[str, BaseException] = {}

        def run() -> None:
            try:
                asyncio.run(scenario())
            except BaseException as exc:  # noqa: BLE001 - スレッド越しに再送出するため捕捉
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=3.0)

        # スレッドが生きたまま残っていても Event を解放し、バックエンド呼び出しに
        # 使ったワーカースレッドが後始末されるようにする(テスト間のリーク防止)。
        release_event.set()

        if thread.is_alive():
            pytest.fail(
                "list_notebooks が ask の完了を待たされた(イベントループがブロックされた)"
            )
        if "error" in outcome:
            raise outcome["error"]


class TestListNotebooks:
    def test_returns_notebook_summaries(self, tmp_path):
        service = _service_with_one_chunk(tmp_path)
        server = create_server(service)

        _, structured = _call(server, "list_notebooks", {})
        result = structured["result"]

        # _service_with_one_chunk は corpus に md ファイルを直接置いて index_notebook
        # を呼ぶだけで documents テーブルへの upsert(add_source 経由)は行わないため、
        # sources は 0 のままになる(chunks のみ 1 件索引化される)。
        assert result == [
            {
                "notebook": "physics",
                "description": "物理の論文",
                "backend": "codex",
                "sources": 0,
                "chunks": 1,
            }
        ]


class TestConsult:
    def test_delegates_to_service_consult_and_returns_dict(self, tmp_path):
        """consult が service.consult へ委譲され、返却値を素通しすることを確認。

        consult は Librarian.route + 専門家推論を並行実行するため、FakeAnswerBackend は
        複数回呼ばれる。設計書 §9-B に従い、canned に複数の JSON を list で渡すと
        呼び出し順に消費される。
        """
        store = Store(":memory:")
        embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
        store.create_notebook("physics", description="物理の論文", backend="codex")
        nb_dir = tmp_path / "physics"
        nb_dir.mkdir()
        (nb_dir / "doc.md").write_text(f"# Doc\n\n{_CHUNK_TEXT}\n", encoding="utf-8")
        index_notebook(tmp_path, "physics", store, embedder)

        # routing と answer の 2 つの backend 呼び出しを用意
        routing_payload = {
            "answerable": True,
            "targets": [{"notebook": "physics", "score": 0.9, "subquery": _QUERY_TEXT, "reason": "一致"}],
        }
        routing_raw = RawAnswer(text=json.dumps(routing_payload), ok=True, error=None)

        answer_payload = {
            "answer": "whales eat krill [S1]",
            "citations": [{"s": 1}],
            "confident": True,
        }
        answer_raw = RawAnswer(text=json.dumps(answer_payload), ok=True, error=None)

        backend = FakeAnswerBackend(canned=[routing_raw, answer_raw])
        service = ShelfService(
            store, embedder, lambda name: backend, tmp_path,
            router_backend="codex", route_top_n=1, route_fallback="",
        )
        server = create_server(service)

        content = _call(server, "consult", {"question": _QUERY_TEXT})
        result = json.loads(content[0].text)

        # service.consult() の返却形式を確認
        assert "question" in result
        assert "answered" in result
        assert "routed" in result
        assert "warning" in result
        assert result["question"] == _QUERY_TEXT
        assert result["answered"] is True
        assert len(result["routed"]) > 0

    def test_slow_consult_does_not_block_concurrent_list_notebooks(self, tmp_path):
        """consult がバックエンド応答待ちでもイベントループを専有せず、他ツール呼び出しを
        並行して処理できることを検証する。TestAsk の同名テストと同型（sync def のままだと
        FastMCP がイベントループ上で素呼びするため、司書ルーティング呼び出しの
        threading.Event.wait() でループ全体が止まり list_notebooks は consult の完了まで
        進行できない）。consult は本 PR（MCP ツールの async 化）の主目的のツールであり、
        ask だけでなく consult 自身についても非専有性を直接証明する必要がある。
        """
        store = Store(":memory:")
        embedder = FakeEmbedder(dim=8, known={_CHUNK_TEXT: _KNOWN_VEC, _QUERY_TEXT: _KNOWN_VEC})
        store.create_notebook("physics", description="物理の論文", backend="codex")
        nb_dir = tmp_path / "physics"
        nb_dir.mkdir()
        (nb_dir / "doc.md").write_text(f"# Doc\n\n{_CHUNK_TEXT}\n", encoding="utf-8")
        index_notebook(tmp_path, "physics", store, embedder)

        # 司書ルーティング呼び出し(backend.answer の1回目)をここでブロックすれば、
        # consult は専門家呼び出しにすら到達できない。それで十分: 目的は「consult 実行中は
        # イベントループが専有されない」ことの証明であり、どの内部呼び出しで止めるかは
        # 本質ではない。
        release_event = threading.Event()
        backend = _BlockingAnswerBackend(release_event, _grounded_raw_answer())
        service = ShelfService(
            store, embedder, lambda name: backend, tmp_path,
            router_backend="codex", route_top_n=1, route_fallback="",
        )
        server = create_server(service)

        async def scenario() -> None:
            consult_task = asyncio.create_task(server.call_tool("consult", {"question": _QUERY_TEXT}))
            # consult_task が backend.answer() の Event.wait() まで進む猶予を与える。
            await asyncio.sleep(0.05)

            list_result = await asyncio.wait_for(server.call_tool("list_notebooks", {}), timeout=2.0)
            assert list_result is not None
            # list_notebooks が完了した時点で consult はまだ backend 応答待ちのはず。
            assert not consult_task.done()

            release_event.set()
            await asyncio.wait_for(consult_task, timeout=2.0)

        # sync 実装ではイベントループ自体がブロックされタイムアウトすら発火できないため、
        # TestAsk の同名テストと同じくデーモンスレッド + join タイムアウトで外側から観測する。
        outcome: dict[str, BaseException] = {}

        def run() -> None:
            try:
                asyncio.run(scenario())
            except BaseException as exc:  # noqa: BLE001 - スレッド越しに再送出するため捕捉
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=3.0)

        release_event.set()

        if thread.is_alive():
            pytest.fail(
                "list_notebooks が consult の完了を待たされた(イベントループがブロックされた)"
            )
        if "error" in outcome:
            raise outcome["error"]


@pytest.mark.parametrize("tool_name", ["ask", "list_notebooks", "consult"])
def test_only_the_three_expected_tools_are_registered(tool_name, tmp_path):
    service = _service_with_one_chunk(tmp_path)
    server = create_server(service)

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}

    assert names == {"ask", "list_notebooks", "consult"}
    assert tool_name in names


class TestHealthRoute:
    """GET /health は Task Scheduler 等の死活監視用の無認証エンドポイント
    (design判断: 認証・Host 検査の外にあるため公開情報は status/version の2つのみ)。
    FastMCP.streamable_http_app() が実際に組み立てる Starlette app に対して
    starlette.testclient.TestClient で疎通を検証する(register済みcustom_routeの
    実体を確認するため。実ネットワークは使わない、単一プロセス内ASGI呼び出し)。
    """

    def test_returns_200_with_status_ok_and_version(self, tmp_path):
        service = _service_with_one_chunk(tmp_path)
        server = create_server(service)
        app = server.streamable_http_app()

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == importlib.metadata.version("shelf")

    def test_response_contains_only_status_and_version_fields(self, tmp_path):
        service = _service_with_one_chunk(tmp_path)
        server = create_server(service)
        app = server.streamable_http_app()

        with TestClient(app) as client:
            response = client.get("/health")

        assert set(response.json().keys()) == {"status", "version"}


class TestBuildTransportSecurity:
    """build_transport_security（純粋関数）のテスト。

    cli.py は import ガード（mcp は server.py のみ import 可）に抵触するため
    TransportSecuritySettings を直接構築できない。この関数が唯一の構築窓口になる。
    """

    def test_enables_dns_rebinding_protection(self):
        security = build_transport_security(["100.113.69.62:8765"])

        assert security.enable_dns_rebinding_protection is True

    def test_allowed_hosts_matches_input_list(self):
        hosts = ["100.113.69.62:8765", "100.113.69.62", "avalon.tailxxxx.ts.net:8765"]

        security = build_transport_security(hosts)

        assert security.allowed_hosts == hosts

    def test_allowed_origins_are_http_prefixed_hosts(self):
        hosts = ["100.113.69.62:8765", "100.113.69.62"]

        security = build_transport_security(hosts)

        assert security.allowed_origins == [
            "http://100.113.69.62:8765",
            "http://100.113.69.62",
        ]
