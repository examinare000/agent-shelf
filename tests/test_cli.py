"""cli.build_parser の引数解析テスト(実 Store/Embedder/エンジンを構築しない純粋な部分のみ)。

main() の実行配線(serve/new/add/rm/index/ask の実処理)は実 SQLite ファイル・
ONNX モデル・サブスク CLI に触れるため、単体テストでは検証せずスモークテストで
確認する(recall/tests/test_cli.py・design doc §6 と同じ割り切り)。

例外: _cmd_rm の notebook 不一致拒否(重大指摘#1)は cli._build_store を
Store(":memory:") に差し替えるだけで実 DB・ファイルに触れず検証できるため、
main() 経由でここに含める。
"""
from __future__ import annotations

import json
import os

import pytest

from shelf import cli
from shelf.cli import build_parser
from shelf.doctor import CheckResult
from shelf.indexer import IndexStats
from shelf.service import ShelfService
from shelf.store import Store, UnknownNotebookError
from tests.fakes import FakeEmbedder


class TestServeCommand:
    def test_parses(self):
        args = build_parser().parse_args(["serve"])
        assert args.command == "serve"

    def test_http_defaults_to_none_sentinel(self):
        # None は「CLI 指定なし」を表すサンチネル。実際の既定値解決(フラグ > env > 既定)は
        # resolve_serve_settings が担うため、argparse 自体は env より優先すべきかを
        # 判定できるよう None のままにする。
        args = build_parser().parse_args(["serve"])
        assert args.http is None

    def test_host_defaults_to_none_sentinel(self):
        args = build_parser().parse_args(["serve"])
        assert args.host is None

    def test_port_defaults_to_none_sentinel(self):
        args = build_parser().parse_args(["serve"])
        assert args.port is None

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

    def test_stdio_flag_defaults_to_false(self):
        args = build_parser().parse_args(["serve"])
        assert args.stdio is False

    def test_stdio_flag_can_be_set(self):
        args = build_parser().parse_args(["serve", "--stdio"])
        assert args.stdio is True


class TestBindWarning:
    """--host が全インターフェース bind を意味する値のとき、意図しない公開を
    避けるための警告メッセージを返す純関数(_bind_warning)を検証する。
    """

    def test_none_for_localhost(self):
        assert cli._bind_warning("127.0.0.1") is None

    def test_none_for_arbitrary_single_interface(self):
        assert cli._bind_warning("100.64.0.1") is None

    def test_warns_for_ipv4_all_interfaces(self):
        warning = cli._bind_warning("0.0.0.0")
        assert warning is not None
        assert "全インターフェース" in warning

    def test_warning_message_mentions_ipv6_loopback_alternative(self):
        # IPv4 の 127.0.0.1 だけでなく IPv6 の ::1 にも触れ、IPv6 で bind
        # している利用者にも推奨先が伝わるようにする。
        warning = cli._bind_warning("0.0.0.0")
        assert warning is not None
        assert "::1" in warning

    def test_warns_for_ipv6_all_interfaces(self):
        warning = cli._bind_warning("::")
        assert warning is not None
        assert "全インターフェース" in warning

    def test_none_for_ipv6_loopback(self):
        assert cli._bind_warning("::1") is None

    def test_none_for_ipv6_arbitrary_address(self):
        assert cli._bind_warning("2001:db8::1") is None


class TestResolveServeSettings:
    """resolve_serve_settings の優先順位(CLI フラグ > env)を検証する純関数テスト。

    build_transport_security と同じ流儀で、config モジュールを直接読まず呼び出し元が
    解決済みの値を明示的に渡す(reload 不要でテストできる・呼び出し元の責務を分離する)。
    """

    def test_flags_present_win_over_differing_env(self):
        args = build_parser().parse_args(
            [
                "serve",
                "--http",
                "--host",
                "100.64.0.1",
                "--port",
                "9000",
                "--allowed-host",
                "flaghost:9000",
            ]
        )
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=False,
            env_host="127.0.0.1",
            env_port=8765,
            env_allowed_hosts=["envhost:8765"],
        )
        assert settings.http is True
        assert settings.host == "100.64.0.1"
        assert settings.port == 9000
        assert settings.allowed_hosts == ["flaghost:9000"]

    def test_env_only_is_used_when_no_flags_given(self):
        args = build_parser().parse_args(["serve"])
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=True,
            env_host="192.168.1.5",
            env_port=9100,
            env_allowed_hosts=["envhost:9100"],
        )
        assert settings.http is True
        assert settings.host == "192.168.1.5"
        assert settings.port == 9100
        assert settings.allowed_hosts == ["envhost:9100"]

    def test_conflicting_http_host_port_flags_win_while_unset_allowed_host_falls_back_to_env(
        self,
    ):
        # http/host/port は CLI で明示指定(env と競合)しているため常にフラグが勝つ。
        # 一方 --allowed-host は未指定のため、この項目だけ独立に env 側へフォール
        # バックする(4項目が一括で「フラグ優先」になるわけではないことの固定)。
        args = build_parser().parse_args(
            ["serve", "--http", "--host", "flag.example", "--port", "1111"]
        )
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=False,
            env_host="env.example",
            env_port=2222,
            env_allowed_hosts=["envhost:2222"],
        )
        assert settings.http is True
        assert settings.host == "flag.example"
        assert settings.port == 1111
        assert settings.allowed_hosts == ["envhost:2222"]

    def test_neither_flags_nor_meaningful_env_falls_back_to_env_defaults(self):
        # env_* 自体には config.py 側で既に「未設定時のハードコード既定値」が
        # 解決済みの前提(責務分離)。ここでは resolve_serve_settings がその値を
        # そのまま透過することだけを検証する。
        args = build_parser().parse_args(["serve"])
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=False,
            env_host="127.0.0.1",
            env_port=8765,
            env_allowed_hosts=[],
        )
        assert settings.http is False
        assert settings.host == "127.0.0.1"
        assert settings.port == 8765
        assert settings.allowed_hosts == []

    def test_stdio_flag_wins_over_http_flag_and_env(self):
        # --stdio は MCP クライアント登録(裸の `shelf serve` に依存)が env による
        # 無言のすり替えに巻き込まれないための脱出口。優先順位は --stdio > --http >
        # env > 既定であり、--http フラグと SHELF_HTTP_ENABLED が両方 true でも
        # --stdio が最終的に勝つ。
        args = build_parser().parse_args(["serve", "--http", "--stdio"])
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=True,
            env_host="127.0.0.1",
            env_port=8765,
            env_allowed_hosts=[],
        )
        assert settings.http is False

    def test_stdio_flag_wins_over_env_alone(self):
        args = build_parser().parse_args(["serve", "--stdio"])
        settings = cli.resolve_serve_settings(
            args,
            env_http_enabled=True,
            env_host="127.0.0.1",
            env_port=8765,
            env_allowed_hosts=[],
        )
        assert settings.http is False


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


class TestDoctorCommand:
    def test_parses_with_no_extra_args(self):
        args = build_parser().parse_args(["doctor"])
        assert args.command == "doctor"


class TestDoctorDispatch:
    """main() の doctor ディスパッチが doctor.run_checks() の結果を印字し、
    1件でも ok=False なら SystemExit(1) することを、run_checks を差し替えて検証する
    (実PATH/実DB/実ネットワークには一切触れない)。
    """

    def test_prints_ok_and_ng_marks_with_detail(self, monkeypatch, capsys):
        results = [
            CheckResult(name="engine:codex", ok=True, detail="codex コマンドが見つかりました"),
            CheckResult(name="ollama", ok=False, detail="http://x へ疎通できません"),
        ]
        monkeypatch.setattr(cli.doctor, "run_checks", lambda: results)

        with pytest.raises(SystemExit):
            cli.main(["doctor"])

        out = capsys.readouterr().out
        assert "✓ engine:codex: codex コマンドが見つかりました" in out
        assert "✗ ollama: http://x へ疎通できません" in out

    def test_exits_zero_when_all_ok(self, monkeypatch):
        results = [CheckResult(name="engine:codex", ok=True, detail="ok")]
        monkeypatch.setattr(cli.doctor, "run_checks", lambda: results)

        try:
            cli.main(["doctor"])
        except SystemExit as e:
            assert e.code in (0, None)

    def test_exits_one_when_any_check_fails(self, monkeypatch):
        results = [
            CheckResult(name="engine:codex", ok=True, detail="ok"),
            CheckResult(name="ollama", ok=False, detail="ng"),
        ]
        monkeypatch.setattr(cli.doctor, "run_checks", lambda: results)

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["doctor"])

        assert exc_info.value.code == 1


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

    def test_default_dispatch_runs_stdio_without_touching_settings(
        self, monkeypatch, capsys
    ):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve"])

        assert fake_server.run_calls == [None]
        assert fake_server.settings.host == "127.0.0.1"
        assert fake_server.settings.port == 8000
        # stdio ディスパッチは bind 警告の対象外(--host は --http 時のみ使う)
        # なので stderr には何も出ないはずである。
        assert capsys.readouterr().err == ""

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

    def test_shelf_http_enabled_env_triggers_streamable_http_without_http_flag(
        self, monkeypatch
    ):
        """SHELF_HTTP_ENABLED=true(--http フラグ未指定)でも streamable-http が
        選ばれることを、config 属性を直接差し替えて検証する(env→config 解決自体は
        test_config.py が担当するため、ここでは cli 側の分岐のみを見る)。
        """
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)
        monkeypatch.setattr(cli.config, "HTTP_ENABLED", True)

        cli.main(["serve"])

        assert fake_server.run_calls == ["streamable-http"]

    def test_stdio_flag_forces_stdio_even_when_shelf_http_enabled_env_is_true(
        self, monkeypatch
    ):
        """MCP クライアント登録は裸の `shelf serve`(暗黙 stdio 前提)に依存するため、
        SHELF_HTTP_ENABLED=true が環境に立っていても `--stdio` で明示的に脱出できる
        ことを検証する(レビュー指摘: env による無言のすり替えへの脱出口)。
        """
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)
        monkeypatch.setattr(cli.config, "HTTP_ENABLED", True)

        cli.main(["serve", "--stdio"])

        assert fake_server.run_calls == [None]

    def test_shelf_http_enabled_env_dispatch_uses_env_host_port_and_allowed_hosts(
        self, monkeypatch
    ):
        """SHELF_HTTP_ENABLED=true と併せて SHELF_HTTP_HOST/SHELF_HTTP_PORT/
        SHELF_ALLOWED_HOSTS も env のみで指定された場合(CLI フラグなし)に、
        resolve_serve_settings の kwarg 取り違えなく main() まで正しく配線される
        ことを E2E で検証する(レビュー指摘(b): env→config→dispatch の一気通貫)。
        """
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)
        monkeypatch.setattr(cli.config, "HTTP_ENABLED", True)
        monkeypatch.setattr(cli.config, "HTTP_HOST", "192.168.1.9")
        monkeypatch.setattr(cli.config, "HTTP_PORT", 9200)
        monkeypatch.setattr(cli.config, "ALLOWED_HOSTS", ["envhost:9200"])

        cli.main(["serve"])

        assert fake_server.run_calls == ["streamable-http"]
        assert fake_server.settings.host == "192.168.1.9"
        assert fake_server.settings.port == 9200
        security = fake_server.settings.transport_security
        assert security.allowed_hosts == [
            "192.168.1.9:9200",
            "192.168.1.9",
            "envhost:9200",
        ]

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

    def test_http_dispatch_warns_on_stderr_when_binding_all_interfaces(
        self, monkeypatch, capsys
    ):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve", "--http", "--host", "0.0.0.0", "--port", "8765"])

        assert "全インターフェース" in capsys.readouterr().err
        # 警告があってもサーバ起動自体は妨げない
        assert fake_server.run_calls == ["streamable-http"]

    def test_http_dispatch_does_not_warn_for_localhost(self, monkeypatch, capsys):
        fake_server = _FakeMcpServer()
        monkeypatch.setattr(cli, "_build_service", lambda: object())
        monkeypatch.setattr(cli, "create_server", lambda service: fake_server)

        cli.main(["serve", "--http", "--host", "127.0.0.1", "--port", "8765"])

        assert capsys.readouterr().err == ""


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

    def test_display_only_path_does_not_build_service(self, monkeypatch, capsys):
        """表示のみの分岐(--set/--clear なし)は store だけで完結すべきで、
        実 FastEmbedEmbedder を構築する _build_service を呼んではいけない
        (呼ぶとモデル未キャッシュ環境で実ネットワークダウンロードが走り、
        テストが恒久ハングするバグの再発防止)。
        """
        store = Store(":memory:")
        store.create_notebook("physics", description="物理")
        monkeypatch.setattr(cli, "_build_store", lambda: store)

        def _fail_if_called():
            raise AssertionError("_build_service は表示のみの分岐で呼ばれてはならない")

        monkeypatch.setattr(cli, "_build_service", _fail_if_called)

        cli.main(["persona", "physics"])

        captured = capsys.readouterr().out
        assert "未設定" in captured


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


class TestIngestCommand:
    """ingest サブコマンドの parser 解析テスト。"""

    def test_parses_paths_as_list(self):
        args = build_parser().parse_args(["ingest", "a.md", "b.md"])
        assert args.command == "ingest"
        assert args.paths == ["a.md", "b.md"]

    def test_requires_at_least_one_path(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["ingest"])

    def test_notebook_defaults_to_none(self):
        args = build_parser().parse_args(["ingest", "a.md"])
        assert args.notebook is None

    def test_notebook_can_be_specified(self):
        args = build_parser().parse_args(["ingest", "a.md", "--notebook", "physics"])
        assert args.notebook == "physics"

    def test_auto_shelve_defaults_to_false(self):
        args = build_parser().parse_args(["ingest", "a.md"])
        assert args.auto_shelve is False

    def test_auto_shelve_can_be_set(self):
        args = build_parser().parse_args(["ingest", "a.md", "--auto-shelve"])
        assert args.auto_shelve is True

    def test_digest_defaults_to_false(self):
        args = build_parser().parse_args(["ingest", "a.md"])
        assert args.digest is False

    def test_digest_can_be_set(self):
        args = build_parser().parse_args(["ingest", "a.md", "--digest"])
        assert args.digest is True

    def test_yes_defaults_to_false(self):
        args = build_parser().parse_args(["ingest", "a.md"])
        assert args.yes is False

    def test_yes_can_be_set(self):
        args = build_parser().parse_args(["ingest", "a.md", "--yes"])
        assert args.yes is True


class TestIngestOrchestrationRealAddAndIndex:
    """LLM を一切呼ばずに add→index までを実データ(実 .md ファイル)で検証する。

    ingest は各 add を auto_summary=False で呼ぶ設計にし(要約自動生成には LLM が
    要るため)、backend_factory が一度でも呼ばれたら AssertionError で即座に
    検出する。convert.py の raw コンバータ(.md はネットワーク・重依存なしで
    そのまま読める)+ FakeEmbedder のみで完結する現実的な統合テスト。
    """

    @staticmethod
    def _never_call_backend(name):
        raise AssertionError(f"backend_factory は呼ばれてはいけない (name={name})")

    def _build_real_service(self, tmp_path):
        store = Store(":memory:")
        corpus_dir = tmp_path / "corpus"
        service = ShelfService(
            store,
            FakeEmbedder(),
            self._never_call_backend,
            corpus_dir,
        )
        return store, service

    def test_creates_notebook_adds_files_and_indexes(self, monkeypatch, tmp_path, capsys):
        store, service = self._build_real_service(tmp_path)
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: service)

        doc1 = tmp_path / "doc1.md"
        doc1.write_text("# タイトル1\n" + "本文1" * 60, encoding="utf-8")
        doc2 = tmp_path / "doc2.md"
        doc2.write_text("# タイトル2\n" + "本文2" * 60, encoding="utf-8")

        cli.main(["ingest", str(doc1), str(doc2), "--notebook", "test", "--yes"])

        assert store.get_notebook("test") is not None
        assert len(store.list_documents("test")) == 2
        captured = capsys.readouterr().out
        assert "test" in captured
        assert "indexed=" in captured

    def test_reuses_existing_notebook_without_recreating(self, monkeypatch, tmp_path, capsys):
        store, service = self._build_real_service(tmp_path)
        store.create_notebook("test")
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: service)

        doc1 = tmp_path / "doc1.md"
        doc1.write_text("本文" * 60, encoding="utf-8")

        cli.main(["ingest", str(doc1), "--notebook", "test", "--yes"])

        assert len(store.list_documents("test")) == 1

    def test_yes_without_notebook_and_without_auto_shelve_reports_error(
        self, monkeypatch, tmp_path, capsys
    ):
        store, service = self._build_real_service(tmp_path)
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: service)

        doc1 = tmp_path / "doc1.md"
        doc1.write_text("本文" * 60, encoding="utf-8")

        cli.main(["ingest", str(doc1), "--yes"])

        assert store.list_notebooks() == []
        captured = capsys.readouterr().out
        assert "--notebook" in captured


class TestIngestDigestDispatch:
    """--digest が index の後に service.digest(notebook) を1回呼ぶことを検証する。"""

    def test_digest_flag_calls_service_digest_after_index(self, monkeypatch, tmp_path, capsys):
        calls = []

        class _FakeService:
            def create_notebook(self, name, description=None, backend=None):
                calls.append(("create_notebook", name))

            def add_source(self, notebook, origin, *, description=None, auto_summary=True):
                calls.append(("add_source", notebook, origin, auto_summary))
                return {"doc_id": "doc1"}

            def index(self, notebook, full=False):
                calls.append(("index", notebook))
                return IndexStats(indexed=1, skipped=0, pruned=0, chunks_written=1, errors=0)

            def digest(self, notebook, doc_id=None, *, force=False):
                calls.append(("digest", notebook))
                return {"result": "ok"}

        store = Store(":memory:")
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        doc1 = tmp_path / "doc1.md"
        doc1.write_text("本文" * 60, encoding="utf-8")

        cli.main(["ingest", str(doc1), "--notebook", "test", "--yes", "--digest"])

        kinds = [c[0] for c in calls]
        assert kinds == ["create_notebook", "add_source", "index", "digest"]
        assert calls[1] == ("add_source", "test", str(doc1), False)


class TestIngestAutoShelveDispatch:
    """--auto-shelve は既存 shelve() を各 path に対して呼び、add/index を経由しない。"""

    def test_auto_shelve_calls_service_shelve_per_path_and_skips_add_index(
        self, monkeypatch, tmp_path, capsys
    ):
        calls = []

        class _FakeService:
            def shelve(self, directory, *, dry_run=False):
                calls.append(("shelve", directory, dry_run))
                return {"directory": directory, "dry_run": dry_run, "added": [], "errors": []}

        store = Store(":memory:")
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["ingest", str(tmp_path), "--auto-shelve", "--yes"])

        assert calls == [("shelve", str(tmp_path), False)]

    def test_auto_shelve_with_digest_digests_each_affected_notebook(
        self, monkeypatch, tmp_path, capsys
    ):
        calls = []

        class _FakeService:
            def shelve(self, directory, *, dry_run=False):
                calls.append(("shelve", directory))
                return {
                    "directory": directory,
                    "dry_run": dry_run,
                    "added": [{"doc_id": "d1", "origin": "a.md", "notebook": "nb_a"}],
                    "errors": [],
                }

            def digest(self, notebook, doc_id=None, *, force=False):
                calls.append(("digest", notebook))
                return {"result": "ok"}

        store = Store(":memory:")
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        cli.main(["ingest", str(tmp_path), "--auto-shelve", "--yes", "--digest"])

        kinds = [c[0] for c in calls]
        assert kinds.count("shelve") == 1
        assert ("digest", "nb_a") in calls

    def test_auto_shelve_with_file_path_reports_error_and_skips_shelve(
        self, monkeypatch, tmp_path, capsys
    ):
        """--auto-shelve の path にファイルを渡すと、shelve() は directory を
        rglob するためファイルには空(投入0件/エラー0件)を返し無処理のまま
        正常終了して見えてしまう(重大指摘)。ファイルパスは shelve() に渡さず
        明示エラーを出す。
        """

        class _FakeService:
            def shelve(self, directory, *, dry_run=False):
                raise AssertionError("ファイルパスは shelve() に渡されてはいけない")

        store = Store(":memory:")
        monkeypatch.setattr(cli, "_build_store", lambda: store)
        monkeypatch.setattr(cli, "_build_service", lambda: _FakeService())

        file_path = tmp_path / "doc.md"
        file_path.write_text("本文", encoding="utf-8")

        cli.main(["ingest", str(file_path), "--auto-shelve", "--yes"])

        captured = capsys.readouterr().out
        assert "--auto-shelve" in captured
        assert "ディレクトリ単位" in captured


class TestSelectNotebookInteractively:
    """_select_notebook_interactively(既存一覧を提示し対話選択・新規作成も可)。"""

    def test_selects_existing_notebook_by_number(self):
        scripted = iter(["2"])
        name = cli._select_notebook_interactively(
            ["physics", "biology"], input_func=lambda _p: next(scripted)
        )
        assert name == "biology"

    def test_creates_new_notebook_when_zero_chosen(self):
        scripted = iter(["0", "chemistry"])
        name = cli._select_notebook_interactively(
            ["physics"], input_func=lambda _p: next(scripted)
        )
        assert name == "chemistry"

    def test_typed_name_is_used_directly_when_no_existing_notebooks(self):
        scripted = iter(["newname"])
        name = cli._select_notebook_interactively([], input_func=lambda _p: next(scripted))
        assert name == "newname"


class TestEmitMcpCommand:
    """emit-mcp サブコマンドの parser 解析テスト。"""

    def test_parses_with_defaults(self):
        args = build_parser().parse_args(["emit-mcp"])
        assert args.command == "emit-mcp"
        assert args.host == "all"
        assert args.transport == "stdio"
        assert args.url is None
        assert args.output_dir == "./mcp-config"

    def test_host_choices(self):
        for host in ("claude", "codex", "gemini", "all"):
            args = build_parser().parse_args(["emit-mcp", "--host", host])
            assert args.host == host

    def test_rejects_unknown_host(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["emit-mcp", "--host", "unknown"])

    def test_transport_choices(self):
        for transport in ("stdio", "http"):
            args = build_parser().parse_args(["emit-mcp", "--transport", transport])
            assert args.transport == transport

    def test_url_and_output_dir_can_be_specified(self):
        args = build_parser().parse_args(
            [
                "emit-mcp",
                "--transport",
                "http",
                "--url",
                "http://100.64.0.1:8765/mcp",
                "-o",
                "/tmp/out",
            ]
        )
        assert args.url == "http://100.64.0.1:8765/mcp"
        assert args.output_dir == "/tmp/out"


class TestEmitMcpDispatch:
    """emit-mcp コマンドの main() が shelf.emit_mcp.emit へ正しく橋渡しすることを検証する。"""

    def test_dispatches_all_hosts_by_default(self, monkeypatch, tmp_path, capsys):
        calls = []

        def fake_emit(**kwargs):
            calls.append(kwargs)
            return {"claude": tmp_path / "claude.sh"}

        monkeypatch.setattr(cli.emit_mcp, "emit", fake_emit)

        cli.main(["emit-mcp", "-o", str(tmp_path)])

        assert len(calls) == 1
        assert set(calls[0]["hosts"]) == {"claude", "codex", "gemini"}
        assert calls[0]["transport"] == "stdio"
        assert calls[0]["url"] is None
        assert calls[0]["output_dir"] == tmp_path

    def test_dispatches_single_host(self, monkeypatch, tmp_path, capsys):
        calls = []

        def fake_emit(**kwargs):
            calls.append(kwargs)
            return {"codex": tmp_path / "codex.toml"}

        monkeypatch.setattr(cli.emit_mcp, "emit", fake_emit)

        cli.main(["emit-mcp", "--host", "codex", "-o", str(tmp_path)])

        assert calls[0]["hosts"] == ["codex"]

    def test_http_requires_url_reports_friendly_error_without_calling_emit(
        self, monkeypatch, tmp_path, capsys
    ):
        calls = []
        monkeypatch.setattr(cli.emit_mcp, "emit", lambda **kwargs: calls.append(kwargs))

        cli.main(["emit-mcp", "--transport", "http", "-o", str(tmp_path)])

        assert calls == []
        captured = capsys.readouterr().out
        assert "--url" in captured

    def test_prints_written_file_paths(self, monkeypatch, tmp_path, capsys):
        written = {"claude": tmp_path / "claude.sh", "readme": tmp_path / "README.md"}
        monkeypatch.setattr(cli.emit_mcp, "emit", lambda **kwargs: written)

        cli.main(["emit-mcp", "--host", "claude", "-o", str(tmp_path)])

        captured = capsys.readouterr().out
        assert str(tmp_path / "claude.sh") in captured
        assert str(tmp_path / "README.md") in captured


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


class TestSetupCommand:
    """setup サブコマンドの parser 解析テスト。"""

    def test_parses_with_no_args(self):
        args = build_parser().parse_args(["setup"])
        assert args.command == "setup"
        assert args.yes is False
        assert args.answers_file is None

    def test_yes_flag_can_be_set(self):
        args = build_parser().parse_args(["setup", "--yes"])
        assert args.yes is True

    def test_answers_file_can_be_specified(self):
        args = build_parser().parse_args(["setup", "--answers-file", "answers.json"])
        assert args.answers_file == "answers.json"


class TestSetupDispatch:
    """setup コマンドの main() が shelf.setup の関数群を正しく橋渡しすることを検証する。

    実 config.env には一切触れず、config.resolve_config_path を tmp_path 配下へ
    差し替えて検証する(他コマンドと同じ「実ファイル/実DBに触れない」流儀)。
    """

    def test_answers_file_takes_precedence_and_writes_config_env(
        self, monkeypatch, tmp_path, capsys
    ):
        answers_path = tmp_path / "answers.json"
        answers_path.write_text(json.dumps({"provider": "gemini"}), encoding="utf-8")
        config_path = tmp_path / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        cli.main(["setup", "--answers-file", str(answers_path), "--yes"])

        written = config_path.read_text(encoding="utf-8")
        assert "SHELF_DEFAULT_BACKEND=gemini" in written
        captured = capsys.readouterr().out
        assert str(config_path) in captured

    def test_yes_without_answers_file_writes_hardcoded_defaults(
        self, monkeypatch, tmp_path, capsys
    ):
        config_path = tmp_path / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        cli.main(["setup", "--yes"])

        written = config_path.read_text(encoding="utf-8")
        assert f"SHELF_DEFAULT_BACKEND={cli.config.DEFAULT_BACKEND}" in written

    def test_interactive_path_used_when_neither_yes_nor_answers_file(
        self, monkeypatch, tmp_path, capsys
    ):
        config_path = tmp_path / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)
        calls = []
        monkeypatch.setattr(
            cli.setup,
            "collect_answers_interactively",
            lambda: (calls.append("called") or cli.setup.default_answers()),
        )

        cli.main(["setup"])

        assert calls == ["called"]
        assert config_path.exists()


class TestSetupDispatchErrors:
    """setup の異常系(不正JSON・存在しないanswers-file・書込み不可)を、persona
    コマンドの既存作法(cli.py:501付近)に合わせて日本語エラーメッセージへ整形し、
    生の例外をユーザーに露出させないことを検証する。
    """

    def test_invalid_json_answers_file_reports_japanese_error(
        self, monkeypatch, tmp_path, capsys
    ):
        answers_path = tmp_path / "answers.json"
        answers_path.write_text("{ この JSON は壊れている", encoding="utf-8")
        config_path = tmp_path / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        cli.main(["setup", "--answers-file", str(answers_path), "--yes"])

        captured = capsys.readouterr().out
        assert "エラー" in captured
        assert not config_path.exists()

    def test_missing_answers_file_reports_japanese_error(self, monkeypatch, tmp_path, capsys):
        answers_path = tmp_path / "does-not-exist.json"
        config_path = tmp_path / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        cli.main(["setup", "--answers-file", str(answers_path), "--yes"])

        captured = capsys.readouterr().out
        assert "エラー" in captured
        assert not config_path.exists()

    def test_config_dir_path_collision_reports_japanese_error(self, monkeypatch, tmp_path, capsys):
        """設定ディレクトリの親パスが既にファイルとして存在する場合、書込み失敗を報告する。

        chmod によるディレクトリ書込み禁止は Windows では効果がない（chmod は
        読み取り専用属性のトグルのみで、ディレクトリへのファイル作成は妨げない）
        ため、親コンポーネントを「ディレクトリではなくファイル」にする方式で OS
        非依存に書込み失敗を再現する。setup.write_config_env は書込み前に
        path.parent.mkdir(parents=True, exist_ok=True) を呼ぶため、親が既存の
        非ディレクトリだと mkdir 自体が FileExistsError（OSError のサブクラス）を
        送出する（実測: NotADirectoryError ではない）。POSIX 限定の権限拒否
        （chmod 0o000）シナリオは test_unwritable_config_dir_reports_japanese_error
        を参照（Windows では本テストがその代替）。
        """
        not_a_dir = tmp_path / "not-a-directory"
        not_a_dir.write_text("this is a file, not a directory", encoding="utf-8")
        config_path = not_a_dir / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        cli.main(["setup", "--yes"])

        captured = capsys.readouterr().out
        assert "エラー" in captured

    @pytest.mark.skipif(
        os.name == "nt",
        reason="chmod 0o000 は Windows のディレクトリ書込み可否に影響しないため無効"
        "（Windows での同種シナリオは test_config_dir_path_collision_reports_japanese_error"
        "のパス衝突版が代替する）",
    )
    def test_unwritable_config_dir_reports_japanese_error(self, monkeypatch, tmp_path, capsys):
        """書込み権限のないディレクトリ（chmod 0o000）への書込みはエラーを報告する（POSIX 限定）。"""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o000)
        config_path = readonly_dir / "config.env"
        monkeypatch.setattr(cli.config, "resolve_config_path", lambda: config_path)

        try:
            cli.main(["setup", "--yes"])
        finally:
            readonly_dir.chmod(0o755)

        captured = capsys.readouterr().out
        assert "エラー" in captured


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


class TestBuildServiceWiring:
    """_build_service() が config.py の値を ShelfService コンストラクタへ正しく
    橋渡しすることを検証する。実 ONNX モデルの読み込み(FastEmbedEmbedder)だけを
    軽量フェイクに差し替え、DB は _build_store を ":memory:" に差し替えて実ファイルに
    触れない（TestRmDocNotebookMismatch と同じ「_build_store 差し替え」作法）。
    ShelfService 自体は cli.ShelfService を fake に差し替えて kwargs を記録する
    （backend_factory/mask/convert 等は呼び出し時まで実行されないクロージャ/軽量な
    純粋関数のため差し替え不要）。
    """

    def test_wires_hybrid_search_from_config(self, monkeypatch):
        import shelf.embedder as embedder_module

        captured_kwargs: dict = {}

        class _FakeEmbedder:
            def __init__(self, model_name: str) -> None:
                self.model_name = model_name

        class _FakeShelfService:
            def __init__(self, *args, **kwargs) -> None:
                captured_kwargs.update(kwargs)

        monkeypatch.setattr(embedder_module, "FastEmbedEmbedder", _FakeEmbedder)
        monkeypatch.setattr(cli, "_build_store", lambda: Store(":memory:"))
        monkeypatch.setattr(cli, "ShelfService", _FakeShelfService)
        monkeypatch.setattr(cli.config, "HYBRID_SEARCH", False)

        cli._build_service()

        assert captured_kwargs["hybrid_search"] is False

    def test_wires_digest_settings_from_config(self, monkeypatch):
        # コードレビュー指摘#7: _build_service() が config.py の digest 関連設定
        # (digest_map_notes/digest_map_window_chars/digest_backend) を ShelfService
        # へ正しく橋渡ししていることのテスト補完(既存の hybrid_search 配線テストと
        # 同じ「_build_store/_build_service 差し替え」作法)。
        import shelf.embedder as embedder_module

        captured_kwargs: dict = {}

        class _FakeEmbedder:
            def __init__(self, model_name: str) -> None:
                self.model_name = model_name

        class _FakeShelfService:
            def __init__(self, *args, **kwargs) -> None:
                captured_kwargs.update(kwargs)

        monkeypatch.setattr(embedder_module, "FastEmbedEmbedder", _FakeEmbedder)
        monkeypatch.setattr(cli, "_build_store", lambda: Store(":memory:"))
        monkeypatch.setattr(cli, "ShelfService", _FakeShelfService)
        monkeypatch.setattr(cli.config, "DIGEST_MAP_NOTES", 7)
        monkeypatch.setattr(cli.config, "DIGEST_MAP_WINDOW_CHARS", 1234)
        monkeypatch.setattr(cli.config, "DIGEST_BACKEND", "gemini")

        cli._build_service()

        assert captured_kwargs["digest_map_notes"] == 7
        assert captured_kwargs["digest_map_window_chars"] == 1234
        assert captured_kwargs["digest_backend"] == "gemini"

    def test_wires_max_file_mb_from_config(self, monkeypatch):
        import shelf.embedder as embedder_module

        captured_kwargs: dict = {}

        class _FakeEmbedder:
            def __init__(self, model_name: str) -> None:
                self.model_name = model_name

        class _FakeShelfService:
            def __init__(self, *args, **kwargs) -> None:
                captured_kwargs.update(kwargs)

        monkeypatch.setattr(embedder_module, "FastEmbedEmbedder", _FakeEmbedder)
        monkeypatch.setattr(cli, "_build_store", lambda: Store(":memory:"))
        monkeypatch.setattr(cli, "ShelfService", _FakeShelfService)
        monkeypatch.setattr(cli.config, "MAX_FILE_MB", 42)

        cli._build_service()

        assert captured_kwargs["max_file_mb"] == 42
