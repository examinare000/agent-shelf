"""shelf emit-mcp（MCP設定ファイル生成）の純粋関数・ファイル出力のテスト。

実 MCP 登録・実 claude/codex/gemini CLI 呼び出しには一切触れない。生成物の
構文検証は json.loads/tomllib.loads と `bash -n`（subprocess は tests/ 配下では
制限対象外。shelf/ パッケージ内 import ガードは test_boundaries.py 参照）で行う。
"""
from __future__ import annotations

import json
import subprocess
import tomllib

import pytest

from shelf import emit_mcp


class TestBuildStdioArgv:
    def test_builds_uv_run_directory_command(self, tmp_path):
        argv = emit_mcp.build_stdio_argv(tmp_path)
        assert argv == ["uv", "run", "--directory", str(tmp_path), "shelf", "serve"]


class TestBuildClaudeShText:
    def test_stdio_includes_uv_run_command(self, tmp_path):
        text = emit_mcp.build_claude_sh_text(transport="stdio", url=None, repo_root=tmp_path)
        assert "claude mcp add shelf -- uv run --directory" in text
        assert str(tmp_path) in text
        assert text.startswith("#!/usr/bin/env bash\n")

    def test_http_includes_transport_flag_and_url(self, tmp_path):
        text = emit_mcp.build_claude_sh_text(
            transport="http", url="http://127.0.0.1:8765/mcp", repo_root=tmp_path
        )
        assert "claude mcp add --transport http shelf" in text
        assert "http://127.0.0.1:8765/mcp" in text

    def test_stdio_script_is_valid_bash_syntax(self, tmp_path):
        text = emit_mcp.build_claude_sh_text(transport="stdio", url=None, repo_root=tmp_path)
        script = tmp_path / "claude.sh"
        script.write_text(text, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_http_script_is_valid_bash_syntax(self, tmp_path):
        text = emit_mcp.build_claude_sh_text(
            transport="http", url="http://127.0.0.1:8765/mcp", repo_root=tmp_path
        )
        script = tmp_path / "claude.sh"
        script.write_text(text, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestBuildCodexTomlText:
    def test_stdio_produces_command_and_args(self, tmp_path):
        text = emit_mcp.build_codex_toml_text(transport="stdio", url=None, repo_root=tmp_path)
        data = tomllib.loads(text)
        server = data["mcp_servers"]["shelf"]
        assert server["command"] == "uv"
        assert server["args"] == ["run", "--directory", str(tmp_path), "shelf", "serve"]

    def test_http_produces_url_form(self, tmp_path):
        text = emit_mcp.build_codex_toml_text(
            transport="http", url="http://127.0.0.1:8765/mcp", repo_root=tmp_path
        )
        data = tomllib.loads(text)
        assert data["mcp_servers"]["shelf"]["url"] == "http://127.0.0.1:8765/mcp"


class TestBuildGeminiJsonText:
    def test_stdio_produces_command_and_args(self, tmp_path):
        text = emit_mcp.build_gemini_json_text(transport="stdio", url=None, repo_root=tmp_path)
        data = json.loads(text)
        server = data["mcpServers"]["shelf"]
        assert server["command"] == "uv"
        assert server["args"] == ["run", "--directory", str(tmp_path), "shelf", "serve"]

    def test_http_produces_url_form(self, tmp_path):
        text = emit_mcp.build_gemini_json_text(
            transport="http", url="http://127.0.0.1:8765/mcp", repo_root=tmp_path
        )
        data = json.loads(text)
        assert data["mcpServers"]["shelf"]["url"] == "http://127.0.0.1:8765/mcp"


class TestBuildReadmeText:
    def test_mentions_only_emitted_host_files(self):
        text = emit_mcp.build_readme_text(["claude", "gemini"])
        assert "claude.sh" in text
        assert "gemini.json" in text
        assert "codex.toml" not in text


class TestEmit:
    """emit() のファイル出力・組み合わせ・バリデーションを検証する。"""

    def test_http_transport_without_url_raises(self, tmp_path):
        with pytest.raises(ValueError):
            emit_mcp.emit(
                hosts=["claude"],
                transport="http",
                url=None,
                output_dir=tmp_path,
                repo_root=tmp_path,
            )

    def test_all_hosts_writes_three_config_files_and_readme(self, tmp_path):
        written = emit_mcp.emit(
            hosts=list(emit_mcp.HOST_CHOICES),
            transport="stdio",
            url=None,
            output_dir=tmp_path,
            repo_root=tmp_path,
        )

        assert (tmp_path / "claude.sh").exists()
        assert (tmp_path / "codex.toml").exists()
        assert (tmp_path / "gemini.json").exists()
        assert (tmp_path / "README.md").exists()
        assert set(written) == {"claude", "codex", "gemini", "readme"}

    def test_single_host_writes_only_that_file_and_readme(self, tmp_path):
        emit_mcp.emit(
            hosts=["codex"],
            transport="stdio",
            url=None,
            output_dir=tmp_path,
            repo_root=tmp_path,
        )

        assert not (tmp_path / "claude.sh").exists()
        assert (tmp_path / "codex.toml").exists()
        assert not (tmp_path / "gemini.json").exists()
        assert (tmp_path / "README.md").exists()

    def test_claude_sh_is_made_executable(self, tmp_path):
        import os

        emit_mcp.emit(
            hosts=["claude"],
            transport="stdio",
            url=None,
            output_dir=tmp_path,
            repo_root=tmp_path,
        )

        assert os.access(tmp_path / "claude.sh", os.X_OK)

    def test_creates_output_directory_if_missing(self, tmp_path):
        target = tmp_path / "nested" / "mcp-config"

        emit_mcp.emit(
            hosts=["gemini"],
            transport="stdio",
            url=None,
            output_dir=target,
            repo_root=tmp_path,
        )

        assert (target / "gemini.json").exists()
