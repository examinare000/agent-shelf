"""
各エンジンの CLI 引数組み立てテスト。

build_*_cmd（純粋関数）が正しい引数を生成すること、
create_backend のレジストリと未知名エラーを検証。
**実 CLI は起動しない**。
"""
from __future__ import annotations

import json

import pytest

from shelf import config
from shelf.engines import agy as agy_module
from shelf.engines import codex as codex_module
from shelf.engines import create_backend
from shelf.engines import gemini_cli as gemini_cli_module
from shelf.engines import ollama as ollama_module
from shelf.engines.agy import build_agy_cmd
from shelf.engines.codex import build_codex_cmd
from shelf.engines.gemini_cli import build_gemini_cmd
from shelf.engines.ollama import build_payload
from shelf.engines.runner import RunResult


class TestBuildCodexCmd:
    """codex 引数組み立てテスト。"""

    def test_codex_cmd_basic_structure(self, tmp_path):
        """基本的なコマンド構造を確認（-s read-only, --skip-git-repo-check, etc）。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=None)

        assert "codex" in cmd
        assert "exec" in cmd
        assert "-s" in cmd
        assert "read-only" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--ephemeral" in cmd
        assert "-" in cmd  # stdin 読み指定
        # exec サブコマンドは -a/--ask-for-approval をサポートしない（トップレベル専用）
        assert "-a" not in cmd
        assert "never" not in cmd

    def test_codex_cmd_includes_workdir(self, tmp_path):
        """workdir が -C で指定されている。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=None)

        idx = cmd.index("-C")
        assert cmd[idx + 1] == str(workdir)

    def test_codex_cmd_includes_output_file(self, tmp_path):
        """出力ファイルが -o で指定されている。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=None)

        idx = cmd.index("-o")
        assert cmd[idx + 1] == str(out_path)

    def test_codex_cmd_without_schema_path(self, tmp_path):
        """schema_path=None の場合 --output-schema が含まれない。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=None)

        assert "--output-schema" not in cmd

    def test_codex_cmd_with_schema_path(self, tmp_path):
        """schema_path が指定された場合 --output-schema が含まれる。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"
        schema_path = tmp_path / "schema.json"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=schema_path)

        assert "--output-schema" in cmd
        idx = cmd.index("--output-schema")
        assert cmd[idx + 1] == str(schema_path)

    def test_codex_cmd_return_type(self, tmp_path):
        """戻り値が list[str] であることを確認。"""
        workdir = tmp_path
        out_path = tmp_path / "output.txt"

        cmd = build_codex_cmd(workdir=workdir, out_path=out_path, schema_path=None)

        assert isinstance(cmd, list)
        assert all(isinstance(arg, str) for arg in cmd)


class TestBuildGeminiCmd:
    """gemini 引数組み立てテスト。

    gemini CLI にタイムアウト指定オプションは存在しないため、build_gemini_cmd は
    timeout 引数を受け取らない(中位指摘#6: 以前は agy からのコピペで死引数
    timeout を持っていた)。
    """

    def test_gemini_cmd_basic_structure(self):
        """基本的なコマンド構造を確認。"""
        prompt = "test question"

        cmd = build_gemini_cmd(prompt=prompt)

        assert "gemini" in cmd
        assert "-p" in cmd
        assert "--approval-mode" in cmd
        assert "plan" in cmd
        assert "-o" in cmd
        assert "json" in cmd

    def test_gemini_cmd_includes_prompt(self):
        """プロンプトが -p で指定されている。"""
        prompt = "what is 2+2?"

        cmd = build_gemini_cmd(prompt=prompt)

        idx = cmd.index("-p")
        assert cmd[idx + 1] == prompt

    def test_gemini_cmd_with_long_prompt(self):
        """長いプロンプトも正しく指定される。"""
        prompt = "This is a very long prompt. " * 100

        cmd = build_gemini_cmd(prompt=prompt)

        idx = cmd.index("-p")
        assert cmd[idx + 1] == prompt

    def test_gemini_cmd_return_type(self):
        """戻り値が list[str] であることを確認。"""
        cmd = build_gemini_cmd(prompt="test")

        assert isinstance(cmd, list)
        assert all(isinstance(arg, str) for arg in cmd)


class TestBuildAgyCmd:
    """agy 引数組み立てテスト。"""

    def test_agy_cmd_basic_structure(self):
        """基本的なコマンド構造を確認。"""
        prompt = "test question"
        timeout = 60

        cmd = build_agy_cmd(prompt=prompt, timeout=timeout)

        assert "agy" in cmd
        assert "-p" in cmd
        assert "--print-timeout" in cmd

    def test_agy_cmd_includes_prompt(self):
        """プロンプトが -p で指定されている。"""
        prompt = "what is 2+2?"
        timeout = 60

        cmd = build_agy_cmd(prompt=prompt, timeout=timeout)

        idx = cmd.index("-p")
        assert cmd[idx + 1] == prompt

    def test_agy_cmd_includes_timeout(self):
        """タイムアウトが --print-timeout で指定されている。"""
        prompt = "test"
        timeout = 42

        cmd = build_agy_cmd(prompt=prompt, timeout=timeout)

        idx = cmd.index("--print-timeout")
        assert cmd[idx + 1] == "42s"

    def test_agy_cmd_timeout_format(self):
        """タイムアウトが '{timeout}s' 形式で指定される。"""
        cmd = build_agy_cmd(prompt="test", timeout=120)

        idx = cmd.index("--print-timeout")
        timeout_str = cmd[idx + 1]
        assert timeout_str.endswith("s")
        assert timeout_str[:-1].isdigit()

    def test_agy_cmd_return_type(self):
        """戻り値が list[str] であることを確認。"""
        cmd = build_agy_cmd(prompt="test", timeout=60)

        assert isinstance(cmd, list)
        assert all(isinstance(arg, str) for arg in cmd)


class TestBuildPayload:
    """Ollama /api/chat ペイロード組立テスト（純粋関数・決定論）。"""

    def test_basic_structure(self):
        """model・messages・stream: false を含む基本構造。"""
        payload = build_payload("2+2は?", "qwen3:8b", None)

        assert payload["model"] == "qwen3:8b"
        assert payload["stream"] is False
        assert payload["messages"] == [{"role": "user", "content": "2+2は?"}]

    def test_think_false_is_always_present(self):
        """qwen3系のthinkingモードを抑止するため think: false を常に付与する
        (JSON出力との干渉防止。古い Ollama は未知フィールドを無視するため無害)。
        """
        payload = build_payload("prompt", "qwen3:8b", None)

        assert payload["think"] is False

    def test_temperature_zero_is_always_present(self):
        """分類・ルーティング等の構造化判断で実行ごとに結果が揺れ、理由文と
        action が自己矛盾する事象を実機で観測したため、温度0で決定論化する。
        """
        payload = build_payload("prompt", "qwen3:8b", None)

        assert payload["options"] == {"temperature": 0}

    def test_schema_none_omits_format_key(self):
        """schema=None の場合 format キーを含まない。"""
        payload = build_payload("prompt", "qwen3:8b", None)

        assert "format" not in payload

    def test_schema_given_sets_format_key(self):
        """schema が指定された場合、structured outputs 用の format キーに渡す。"""
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

        payload = build_payload("prompt", "qwen3:8b", schema)

        assert payload["format"] == schema

    def test_return_type_is_dict(self):
        payload = build_payload("prompt", "qwen3:8b", None)

        assert isinstance(payload, dict)

    def test_keep_alive_default_is_omitted(self):
        """keep_alive 省略時（未指定）は既定 None としてキーを含めない
        （呼び出し元 OllamaBackend が常に明示的に渡す設計だが、純粋関数単体としても
        安全なデフォルトにしておく）。
        """
        payload = build_payload("prompt", "qwen3:8b", None)

        assert "keep_alive" not in payload

    def test_keep_alive_given_sets_key(self):
        """keep_alive が空でない文字列で指定された場合、payload にそのまま渡す。"""
        payload = build_payload("prompt", "qwen3:8b", None, keep_alive="60m")

        assert payload["keep_alive"] == "60m"

    def test_keep_alive_empty_string_omits_key(self):
        """keep_alive が空文字列（Ollama 既定へ委ねる脱出ハッチ）の場合、
        キー自体を省いて Ollama 側の既定挙動に任せる。
        """
        payload = build_payload("prompt", "qwen3:8b", None, keep_alive="")

        assert "keep_alive" not in payload


class _FakeHTTPResponse:
    """urllib.request.urlopen の戻り値（with 文で使うコンテキストマネージャ）を模す。"""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


class TestOllamaBackendAnswer:
    """OllamaBackend.answer() のテスト。urllib.request.urlopen をモンキーパッチし、
    実ネットワーク・実 Ollama デーモンには一切触れない。
    """

    def test_answer_returns_text_from_message_content(self, monkeypatch, tmp_path):
        def fake_urlopen(req, timeout=None):
            body = json.dumps({"message": {"content": "answer text"}}).encode("utf-8")
            return _FakeHTTPResponse(body)

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is True
        assert raw.text == "answer text"
        assert raw.error is None

    def test_answer_sends_payload_built_by_build_payload(self, monkeypatch, tmp_path):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            body = json.dumps({"message": {"content": "ok"}}).encode("utf-8")
            return _FakeHTTPResponse(body)

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=42, url="http://host:11434", model="qwen3:8b"
        )
        schema = {"type": "object"}

        backend.answer("hello", tmp_path, schema=schema)

        assert captured["data"] == build_payload(
            "hello", "qwen3:8b", schema, keep_alive=config.OLLAMA_KEEP_ALIVE
        )
        assert captured["timeout"] == 42

    def test_answer_sends_keep_alive_from_config(self, monkeypatch, tmp_path):
        """モデルアンロードによる再ロードレイテンシを避けるため、answer() は
        config.OLLAMA_KEEP_ALIVE を payload に載せて送る。
        """
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            body = json.dumps({"message": {"content": "ok"}}).encode("utf-8")
            return _FakeHTTPResponse(body)

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        monkeypatch.setattr(ollama_module.config, "OLLAMA_KEEP_ALIVE", "30m")
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        backend.answer("hello", tmp_path, schema=None)

        assert captured["data"]["keep_alive"] == "30m"

    def test_connection_error_returns_safe_failure(self, monkeypatch, tmp_path):
        def fake_urlopen(req, timeout=None):
            raise OSError("connection refused to http://internal-host:11434")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert raw.error is not None
        # 安全な要約のみ: 内部詳細（接続文字列全文）を含めない
        assert "internal-host" not in raw.error

    def test_timeout_returns_safe_failure(self, monkeypatch, tmp_path):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert raw.error is not None

    def test_json_parse_failure_returns_safe_failure(self, monkeypatch, tmp_path):
        def fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(b"not json")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert raw.error is not None

    def test_http_error_returns_safe_failure(self, monkeypatch, tmp_path):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:11434/api/chat", 500, "Internal Server Error", {}, None
            )

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)
        backend = ollama_module.OllamaBackend(
            timeout=5, url="http://127.0.0.1:11434", model="qwen3:8b"
        )

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert raw.error is not None
        # URL 全文をエラーに含めない（安全な要約のみ）
        assert "127.0.0.1:11434" not in raw.error

    def test_defaults_url_and_model_from_config_when_omitted(self):
        from shelf import config

        backend = ollama_module.OllamaBackend(timeout=5)

        assert backend.url == config.OLLAMA_URL
        assert backend.model == config.OLLAMA_MODEL

    def test_name_is_ollama(self):
        backend = ollama_module.OllamaBackend(timeout=5)
        assert backend.name == "ollama"


class TestIsReachable:
    """is_reachable(url)（shelf setup のローカル LLM 自動検出が使う）のテスト。

    urlopen をモンキーパッチし、実ネットワークには一切触れない
    （TestOllamaBackendAnswer と同じ作法）。urllib.request の import は本ファイル
    （ollama.py）に限定されるため（test_boundaries.py）、shelf/setup.py はここを
    経由してのみ HTTP 疎通を確認できる。
    """

    def test_returns_true_when_urlopen_succeeds(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(b"{}")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        assert ollama_module.is_reachable("http://127.0.0.1:11434") is True

    def test_returns_false_when_urlopen_raises(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        assert ollama_module.is_reachable("http://127.0.0.1:11434") is False

    def test_passes_timeout_and_tags_endpoint_to_urlopen(self, monkeypatch):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _FakeHTTPResponse(b"{}")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        ollama_module.is_reachable("http://127.0.0.1:11434", timeout=2.5)

        assert captured["url"] == "http://127.0.0.1:11434/api/tags"
        assert captured["timeout"] == 2.5


class TestPreload:
    """engines/ollama.preload（serve 起動時のモデルプリロード用モジュール関数）のテスト。
    urllib.request.urlopen をモンキーパッチし、実ネットワーク・実 Ollama デーモンには
    一切触れない（TestOllamaBackendAnswer と同じ作法）。
    """

    def test_sends_model_and_empty_messages(self, monkeypatch):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeHTTPResponse(b'{"message": {"content": ""}}')

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        ollama_module.preload("http://127.0.0.1:11434", "qwen3:8b", "60m", timeout=5)

        assert captured["data"]["model"] == "qwen3:8b"
        assert captured["data"]["messages"] == []
        assert captured["data"]["stream"] is False
        assert captured["timeout"] == 5

    def test_sends_keep_alive_when_non_empty(self, monkeypatch):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return _FakeHTTPResponse(b'{"message": {"content": ""}}')

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        ollama_module.preload("http://127.0.0.1:11434", "qwen3:8b", "60m", timeout=5)

        assert captured["data"]["keep_alive"] == "60m"

    def test_omits_keep_alive_when_empty(self, monkeypatch):
        """空文字列（Ollama 既定へ委ねる脱出ハッチ）の場合、キー自体を省く
        （build_payload の空文字列の扱いと一貫させる）。
        """
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return _FakeHTTPResponse(b'{"message": {"content": ""}}')

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        ollama_module.preload("http://127.0.0.1:11434", "qwen3:8b", "", timeout=5)

        assert "keep_alive" not in captured["data"]

    def test_returns_true_on_success(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(b'{"message": {"content": ""}}')

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        result = ollama_module.preload("http://127.0.0.1:11434", "qwen3:8b", "60m", timeout=5)

        assert result is True

    def test_returns_false_and_swallows_exception_on_failure(self, monkeypatch):
        """Ollama 不在・接続失敗等は起動をブロックしてはならないため、例外を
        送出せず False を返す（fail-soft）。
        """

        def fake_urlopen(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(ollama_module, "urlopen", fake_urlopen)

        result = ollama_module.preload("http://127.0.0.1:11434", "qwen3:8b", "60m", timeout=5)

        assert result is False


class TestCreateBackend:
    """create_backend（factory）のテスト。"""

    def test_create_backend_codex(self):
        """codex バックエンドが生成される。"""
        backend = create_backend("codex", timeout=60)
        assert backend.name == "codex"

    def test_create_backend_gemini(self):
        """gemini バックエンドが生成される。"""
        backend = create_backend("gemini", timeout=60)
        assert backend.name == "gemini"

    def test_create_backend_agy(self):
        """agy バックエンドが生成される。"""
        backend = create_backend("agy", timeout=60)
        assert backend.name == "agy"

    def test_create_backend_ollama(self):
        """ollama バックエンドが生成される（design doc §10-4: ローカル LLM 拡張）。"""
        backend = create_backend("ollama", timeout=60)
        assert backend.name == "ollama"

    def test_create_backend_unknown_raises(self):
        """未知のバックエンド名で ValueError が投げられる。"""
        with pytest.raises(ValueError) as exc_info:
            create_backend("unknown_backend", timeout=60)

        # エラーメッセージに利用可能なバックエンド名が含まれる
        msg = str(exc_info.value)
        assert "codex" in msg or "Available" in msg

    def test_create_backend_timeout_is_set(self):
        """create_backend で指定した timeout がバックエンドに設定される。"""
        timeout = 123
        backend = create_backend("codex", timeout=timeout)
        assert backend.timeout == timeout

    def test_create_backend_available_names_in_error(self):
        """エラーメッセージに利用可能なバックエンド名が列挙されている。"""
        with pytest.raises(ValueError) as exc_info:
            create_backend("invalid", timeout=60)

        msg = str(exc_info.value)
        assert "codex" in msg
        assert "gemini" in msg
        assert "agy" in msg


class TestEngineErrorIncludesStderrSummary:
    """engines 3種の returncode!=0 時の error 文字列は、現状 "codex exited 1" のような
    returncode のみで診断不能だった(中位指摘#6)。stderr 先頭1行が含まれることを、
    各エンジンモジュールの run_command をモンキーパッチして検証する(実CLIは起動しない)。
    """

    _FAILING_STDERR = "auth error: token expired\nsome internal trace\nmore trace"

    def test_codex_answer_error_includes_stderr_first_line(self, monkeypatch, tmp_path):
        def fake_run_command(cmd, *, stdin_text=None, timeout=300, workdir=None):
            return RunResult(
                stdout="", stderr=self._FAILING_STDERR, returncode=1, timed_out=False
            )

        monkeypatch.setattr(codex_module, "run_command", fake_run_command)
        backend = codex_module.CodexBackend(timeout=5)

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert "auth error: token expired" in raw.error

    def test_gemini_answer_error_includes_stderr_first_line(self, monkeypatch, tmp_path):
        def fake_run_command(cmd, *, stdin_text=None, timeout=300, workdir=None):
            return RunResult(
                stdout="", stderr=self._FAILING_STDERR, returncode=1, timed_out=False
            )

        monkeypatch.setattr(gemini_cli_module, "run_command", fake_run_command)
        backend = gemini_cli_module.GeminiCliBackend(timeout=5)

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert "auth error: token expired" in raw.error

    def test_agy_answer_error_includes_stderr_first_line(self, monkeypatch, tmp_path):
        def fake_run_command(cmd, *, stdin_text=None, timeout=300, workdir=None):
            return RunResult(
                stdout="", stderr=self._FAILING_STDERR, returncode=1, timed_out=False
            )

        monkeypatch.setattr(agy_module, "run_command", fake_run_command)
        backend = agy_module.AgyBackend(timeout=5)

        raw = backend.answer("prompt", tmp_path, schema=None)

        assert raw.ok is False
        assert "auth error: token expired" in raw.error
