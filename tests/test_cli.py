"""cli.build_parser の引数解析テスト(実 Store/Embedder/エンジンを構築しない純粋な部分のみ)。

main() の実行配線(serve/new/add/rm/index/ask の実処理)は実 SQLite ファイル・
ONNX モデル・サブスク CLI に触れるため、単体テストでは検証せずスモークテストで
確認する(recall/tests/test_cli.py・design doc §6 と同じ割り切り)。

例外: _cmd_rm の notebook 不一致拒否(重大指摘#1)は cli._build_store を
Store(":memory:") に差し替えるだけで実 DB・ファイルに触れず検証できるため、
main() 経由でここに含める。
"""
from __future__ import annotations

import pytest

from shelf import cli
from shelf.cli import build_parser
from shelf.store import Store, UnknownNotebookError


class TestServeCommand:
    def test_parses(self):
        args = build_parser().parse_args(["serve"])
        assert args.command == "serve"

    def test_http_defaults_to_false(self):
        args = build_parser().parse_args(["serve"])
        assert args.http is False

    def test_host_defaults_to_localhost(self):
        args = build_parser().parse_args(["serve"])
        assert args.host == "127.0.0.1"

    def test_port_defaults_to_8765(self):
        args = build_parser().parse_args(["serve"])
        assert args.port == 8765

    def test_http_flag_can_be_set(self):
        args = build_parser().parse_args(["serve", "--http"])
        assert args.http is True

    def test_host_and_port_can_be_overridden(self):
        """Tailscale 経由のクロスデバイス接続を想定し、host/port を上書きできる
        (design doc §1「Tailscale 内 bind 前提・認証は VPN 境界に委ねる」)。
        """
        args = build_parser().parse_args(
            ["serve", "--http", "--host", "100.64.0.1", "--port", "9000"]
        )
        assert args.host == "100.64.0.1"
        assert args.port == 9000

    def test_allowed_host_defaults_to_none(self):
        args = build_parser().parse_args(["serve"])
        assert args.allowed_host is None

    def test_allowed_host_can_be_specified_multiple_times(self):
        """DNS リバインディング保護の許可 Host を Tailscale MagicDNS 名等で
        追加できる(繰り返し指定可)。
        """
        args = build_parser().parse_args(
            [
                "serve",
                "--http",
                "--allowed-host",
                "avalon.tailxxxx.ts.net:8765",
                "--allowed-host",
                "otherhost:8765",
            ]
        )
        assert args.allowed_host == ["avalon.tailxxxx.ts.net:8765", "otherhost:8765"]


class TestLsCommand:
    def test_notebook_defaults_to_none(self):
        args = build_parser().parse_args(["ls"])
        assert args.command == "ls"
        assert args.notebook is None

    def test_notebook_can_be_specified(self):
        args = build_parser().parse_args(["ls", "physics"])
        assert args.notebook == "physics"


class TestNewCommand:
    def test_parses_required_notebook_only(self):
        args = build_parser().parse_args(["new", "physics"])
        assert args.command == "new"
        assert args.notebook == "physics"
        assert args.description is None
        assert args.backend is None

    def test_parses_desc_and_backend(self):
        args = build_parser().parse_args(
            ["new", "physics", "--desc", "物理の論文", "--backend", "gemini"]
        )
        assert args.description == "物理の論文"
        assert args.backend == "gemini"

    def test_rejects_unknown_backend(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["new", "physics", "--backend", "unknown"])

    def test_accepts_ollama_backend(self):
        """ローカル LLM バックエンド追加（design doc §10-4）。"""
        args = build_parser().parse_args(["new", "physics", "--backend", "ollama"])
        assert args.backend == "ollama"


class TestAddCommand:
    def test_parses_notebook_and_origin(self):
        args = build_parser().parse_args(["add", "physics", "paper.pdf"])
        assert args.command == "add"
        assert args.notebook == "physics"
        assert args.origin == "paper.pdf"


def test_add_parser_accepts_desc_and_no_summary():
    args = build_parser().parse_args(
        ["add", "physics", "paper.pdf", "--desc", "物理の論文", "--no-summary"]
    )
    assert args.description == "物理の論文"
    assert args.no_summary is True


def test_add_parser_defaults_desc_none_and_summary_enabled():
    args = build_parser().parse_args(["add", "physics", "paper.pdf"])
    assert args.description is None
    assert args.no_summary is False


class TestAddDispatch:
    """add コマンドの main() が service.add_source へ description/auto_summary を
    正しく橋渡しすることを、_build_service を fake に差し替えて検証する
    (TestRmDocNotebookMismatch と同じ「_build_store/_build_service 差し替え」作法)。
    """

    def test_passes_description_and_auto_summary_false_when_no_summary_given(
        self, monkeypatch
    ):
        calls = []

        class _FakeService:
            def add_source(self, notebook, origin, *, description=None, auto_summary=True):
                calls.append((notebook, origin, description, auto_summary))
                return {"doc_id": "doc1"}

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["add", "nb", "file.txt", "--desc", "説明", "--no-summary"])

        assert calls == [("nb", "file.txt", "説明", False)]

    def test_defaults_description_none_and_auto_summary_true(self, monkeypatch):
        calls = []

        class _FakeService:
            def add_source(self, notebook, origin, *, description=None, auto_summary=True):
                calls.append((notebook, origin, description, auto_summary))
                return {"doc_id": "doc1"}

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["add", "nb", "file.txt"])

        assert calls == [("nb", "file.txt", None, True)]


class TestRmCommand:
    def test_doc_and_yes_default_to_none_and_false(self):
        args = build_parser().parse_args(["rm", "physics"])
        assert args.command == "rm"
        assert args.notebook == "physics"
        assert args.doc_id is None
        assert args.yes is False

    def test_parses_doc_and_yes(self):
        args = build_parser().parse_args(
            ["rm", "physics", "--doc", "doc-abc123", "--yes"]
        )
        assert args.doc_id == "doc-abc123"
        assert args.yes is True


class TestIndexCommand:
    def test_all_flag_defaults_to_false(self):
        args = build_parser().parse_args(["index", "physics"])
        assert args.command == "index"
        assert args.notebook == "physics"
        assert args.all is False

    def test_all_flag_can_be_set(self):
        args = build_parser().parse_args(["index", "physics", "--all"])
        assert args.all is True


class TestAskCommand:
    def test_parses_notebook_and_question(self):
        args = build_parser().parse_args(["ask", "physics", "何が書いてある?"])
        assert args.command == "ask"
        assert args.notebook == "physics"
        assert args.question == "何が書いてある?"


class TestConsultCommand:
    def test_parses_question(self):
        args = build_parser().parse_args(["consult", "これは何ですか?"])
        assert args.command == "consult"
        assert args.question == "これは何ですか?"


class TestDigestCommand:
    def test_parses_notebook_only(self):
        args = build_parser().parse_args(["digest", "physics"])
        assert args.command == "digest"
        assert args.notebook == "physics"
        assert args.doc_id is None
        assert args.force is False

    def test_parses_doc_id_and_force(self):
        args = build_parser().parse_args(["digest", "physics", "--doc-id", "doc1", "--force"])
        assert args.doc_id == "doc1"
        assert args.force is True


class TestPersonaCommand:
    def test_parses_notebook_only(self):
        args = build_parser().parse_args(["persona", "physics"])
        assert args.command == "persona"
        assert args.notebook == "physics"
        assert args.set_persona is None
        assert args.clear is False

    def test_parses_set_persona(self):
        args = build_parser().parse_args(["persona", "physics", "--set", "物理学の専門家"])
        assert args.set_persona == "物理学の専門家"
        assert args.clear is False

    def test_parses_clear_persona(self):
        args = build_parser().parse_args(["persona", "physics", "--clear"])
        assert args.set_persona is None
        assert args.clear is True

    def test_set_and_clear_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["persona", "physics", "--set", "text", "--clear"])


class _FakeMcpSettings:
    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 8000
        self.transport_security = None


class _FakeMcpServer:
    """FastMCP の代役。settings.host/port の書き換えと run(transport=...) の
    呼び出され方だけを記録する(実サーバは起動しない)。
    """

    def __init__(self) -> None:
        self.settings = _FakeMcpSettings()
        self.run_calls: list[str | None] = []

    def run(self, transport: str | None = None) -> None:
        self.run_calls.append(transport)


class TestServeDispatch:
    """serve コマンドの main() が stdio/streamable-http を正しく配線することを、
    create_server を fake に差し替えて検証する(TestAddDispatch と同じ「_build_service
    差し替え」作法の応用。実サーバ・実ソケットには一切触れない)。
    """

    def test_default_dispatch_runs_stdio_without_touching_settings(self, monkeypatch):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve"])

        assert fake_server.run_calls == [None]
        assert fake_server.settings.host == "127.0.0.1"
        assert fake_server.settings.port == 8000

    def test_http_dispatch_sets_host_port_and_streamable_http_transport(
        self, monkeypatch
    ):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve", "--http", "--host", "100.64.0.1", "--port", "9000"])

        assert fake_server.run_calls == ["streamable-http"]
        assert fake_server.settings.host == "100.64.0.1"
        assert fake_server.settings.port == 9000

    def test_default_dispatch_does_not_touch_transport_security(self, monkeypatch):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve"])

        assert fake_server.settings.transport_security is None

    def test_http_dispatch_sets_transport_security_to_bind_target_by_default(
        self, monkeypatch
    ):
        """実機検証: --http --host 100.113.69.62 --port 8765 で bind した際に mcp SDK
        の DNS リバインディング保護が既定の localhost 許可リストしか持たず
        「Invalid Host header」になった不具合の再発防止(coordinator 追加増分依頼)。
        既定の許可リストは bind 先そのもの(host:port と host)。
        """
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve", "--http", "--host", "100.113.69.62", "--port", "8765"])

        security = fake_server.settings.transport_security
        assert security.enable_dns_rebinding_protection is True
        assert security.allowed_hosts == ["100.113.69.62:8765", "100.113.69.62"]
        assert security.allowed_origins == [
            "http://100.113.69.62:8765",
            "http://100.113.69.62",
        ]

    def test_http_dispatch_appends_allowed_host_entries(self, monkeypatch):
        """--allowed-host は Tailscale MagicDNS 名等を bind 先の既定リストに追加する
        (置き換えではない)。
        """
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(
            [
                "serve",
                "--http",
                "--host",
                "100.113.69.62",
                "--port",
                "8765",
                "--allowed-host",
                "avalon.tailxxxx.ts.net:8765",
            ]
        )

        security = fake_server.settings.transport_security
        assert security.allowed_hosts == [
            "100.113.69.62:8765",
            "100.113.69.62",
            "avalon.tailxxxx.ts.net:8765",
        ]
        assert security.allowed_origins == [
            "http://100.113.69.62:8765",
            "http://100.113.69.62",
            "http://avalon.tailxxxx.ts.net:8765",
        ]


class TestConsultDispatch:
    """consult コマンドの main() が service.consult へ question を正しく
    橋渡しすることを、_build_service を fake に差し替えて検証する
    (TestAddDispatch と同じ「_build_service 差し替え」作法)。
    """

    def test_passes_question_to_service(self, monkeypatch, capsys):
        calls = []

        class _FakeService:
            def consult(self, question):
                calls.append(question)
                return {"question": question, "answered": False, "routed": []}

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["consult", "これは何ですか?"])

        assert calls == ["これは何ですか?"]
        captured = capsys.readouterr().out
        assert "answered" in captured


class TestDigestDispatch:
    """digest コマンドの main() が service.digest へ notebook/doc_id/force を
    正しく橋渡しすることを、_build_service を fake に差し替えて検証する。
    """

    def test_passes_notebook_doc_id_and_force_to_service(self, monkeypatch, capsys):
        calls = []

        class _FakeService:
            def digest(self, notebook, doc_id=None, *, force=False):
                calls.append((notebook, doc_id, force))
                return {"result": "ok"}

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["digest", "physics", "--doc-id", "doc1", "--force"])

        assert calls == [("physics", "doc1", True)]
        captured = capsys.readouterr().out
        assert "result" in captured

    def test_defaults_doc_id_none_and_force_false(self, monkeypatch):
        calls = []

        class _FakeService:
            def digest(self, notebook, doc_id=None, *, force=False):
                calls.append((notebook, doc_id, force))
                return {"result": "ok"}

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["digest", "physics"])

        assert calls == [("physics", None, False)]


class TestPersonaDispatch:
    """persona コマンドの main() が service.set_persona へ notebook/persona を
    正しく橋渡しすることを、_build_service/_build_store を fake に差し替えて検証する。
    """

    def test_set_persona_passes_text_to_service(self, monkeypatch, capsys):
        calls = []

        class _FakeService:
            def set_persona(self, notebook, persona):
                calls.append((notebook, persona))

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["persona", "physics", "--set", "物理学の専門家"])

        assert calls == [("physics", "物理学の専門家")]
        captured = capsys.readouterr().out
        assert "設定しました" in captured

    def test_clear_persona_passes_none_to_service(self, monkeypatch, capsys):
        calls = []

        class _FakeService:
            def set_persona(self, notebook, persona):
                calls.append((notebook, persona))

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["persona", "physics", "--clear"])

        assert calls == [("physics", None)]
        captured = capsys.readouterr().out
        assert "クリア" in captured

    def test_displays_current_persona_when_no_flags(self, monkeypatch, capsys):
        store = Store(":memory:")
        store.create_notebook("physics", description="物理")
        store.set_persona("physics", "物理学の専門家")
        monkeypatch.setattr(cli, "_build_store", lambda: store)

        cli.main(["persona", "physics"])

        captured = capsys.readouterr().out
        assert "物理学の専門家" in captured

    def test_displays_unset_when_persona_is_none(self, monkeypatch, capsys):
        store = Store(":memory:")
        store.create_notebook("physics")
        monkeypatch.setattr(cli, "_build_store", lambda: store)

        cli.main(["persona", "physics"])

        captured = capsys.readouterr().out
        assert "未設定" in captured

    def test_handles_unknown_notebook_error_on_set(self, monkeypatch, capsys):
        class _FakeService:
            def set_persona(self, notebook, persona):
                raise UnknownNotebookError(f"notebook '{notebook}' does not exist")

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["persona", "unknown", "--set", "text"])

        captured = capsys.readouterr().out
        assert "見つかりません" in captured

    def test_handles_valueerror_on_set(self, monkeypatch, capsys):
        class _FakeService:
            def set_persona(self, notebook, persona):
                raise ValueError("invalid notebook name")

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["persona", "invalid!", "--set", "text"])

        captured = capsys.readouterr().out
        assert "エラー" in captured


class TestRmDocNotebookMismatch:
    """rm --doc は doc_id をグローバル検索するため、positional の notebook と対象
    document の所属 notebook が食い違っていても削除できてしまっていた(重大指摘#1)。
    cli._build_store を Store(":memory:") に差し替えるだけで実 DB・実ファイルに
    一切触れずに検証する。
    """

    def test_rejects_deletion_when_doc_belongs_to_a_different_notebook(
        self, monkeypatch, capsys
    ):
        store = Store(":memory:")
        store.create_notebook("nb_a")
        store.create_notebook("nb_b")
        store.upsert_document(
            id="doc1",
            notebook="nb_a",
            origin="a.pdf",
            origin_type="pdf",
            normalized_path="nb_a/doc1.md",
            converter="raw",
            added_at="2026-01-01T00:00:00Z",
        )
        monkeypatch.setattr(cli, "_build_store", lambda: store)

        cli.main(["rm", "nb_b", "--doc", "doc1", "--yes"])

        assert store.get_document("doc1") is not None
        assert "nb_b" in capsys.readouterr().out

    def test_allows_deletion_when_doc_belongs_to_the_specified_notebook(
        self, monkeypatch, capsys
    ):
        store = Store(":memory:")
        store.create_notebook("nb_a")
        store.upsert_document(
            id="doc1",
            notebook="nb_a",
            origin="a.pdf",
            origin_type="pdf",
            normalized_path="nb_a/doc1.md",
            converter="raw",
            added_at="2026-01-01T00:00:00Z",
        )
        monkeypatch.setattr(cli, "_build_store", lambda: store)

        cli.main(["rm", "nb_a", "--doc", "doc1", "--yes"])

        assert store.get_document("doc1") is None


class TestShelveCommand:
    """shelve サブコマンドの parser 解析テスト。"""

    def test_parses(self):
        """shelve コマンドが解析される。"""
        args = build_parser().parse_args(["shelve", "/tmp/dir"])
        assert args.command == "shelve"
        assert args.directory == "/tmp/dir"

    def test_directory_is_required(self):
        """位置引数 directory が必須。"""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["shelve"])

    def test_dry_run_defaults_to_false(self):
        """--dry-run が既定 False。"""
        args = build_parser().parse_args(["shelve", "/tmp/dir"])
        assert args.dry_run is False

    def test_dry_run_can_be_set(self):
        """--dry-run フラグが設定可能。"""
        args = build_parser().parse_args(["shelve", "/tmp/dir", "--dry-run"])
        assert args.dry_run is True


class TestShelveDispatch:
    """shelve コマンドの main() が service.shelve へ directory/dry_run を正しく
    橋渡しすることを、_build_service を fake に差し替えて検証する。
    """

    def test_passes_directory_and_dry_run_to_service(self, monkeypatch, capsys):
        """directory と dry_run が service.shelve へ正しく渡される。"""
        calls = []

        class _FakeService:
            def shelve(self, directory, *, dry_run=False):
                calls.append((directory, dry_run))
                return {
                    "directory": directory,
                    "dry_run": dry_run,
                    "plan": [],
                    "errors": [],
                }

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["shelve", "/tmp/dir", "--dry-run"])

        assert calls == [("/tmp/dir", True)]
        captured = capsys.readouterr().out
        assert "dry_run" in captured

    def test_dry_run_defaults_to_false(self, monkeypatch, capsys):
        """--dry-run を省略した場合、dry_run=False で呼ばれる。"""
        calls = []

        class _FakeService:
            def shelve(self, directory, *, dry_run=False):
                calls.append((directory, dry_run))
                return {
                    "directory": directory,
                    "dry_run": dry_run,
                    "plan": [],
                    "errors": [],
                }

        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["shelve", "/tmp/dir"])

        assert calls == [("/tmp/dir", False)]
