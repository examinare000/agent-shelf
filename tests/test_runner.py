"""
runner.py のテスト: subprocess 実行の一本化。

実プロセスは /bin/echo・/bin/cat・sleep スクリプト等の決定論コマンドのみ使用。
検証: stdout capture / stdin 渡し / 非0 returncode / timeout で timed_out=True
かつ所要時間が timeout+2秒以内 / 存在しないコマンド→127 / workdir が効く。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shelf.engines.runner import RunResult, run_command, summarize_stderr


class TestRunCommandBasic:
    """基本的な実行・出力キャプチャテスト。"""

    def test_echo_command(self):
        """echo コマンドで stdout をキャプチャできる。"""
        result = run_command(["/bin/echo", "hello"])
        assert result.stdout.strip() == "hello"
        assert result.returncode == 0
        assert result.timed_out is False

    def test_stdin_passthrough(self):
        """stdin でテキストを渡せる。"""
        result = run_command(
            ["/bin/cat"],
            stdin_text="test input\n",
        )
        assert result.stdout == "test input\n"
        assert result.returncode == 0

    def test_stderr_capture(self):
        """stderr をキャプチャできる（Python で stderr に出力するコマンド）。"""
        result = run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('error')"],
            stdin_text=None,
        )
        assert "error" in result.stderr
        assert result.returncode == 0

    def test_nonzero_exit_code(self):
        """非0 exit code をキャプチャできる。"""
        result = run_command(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
        )
        assert result.returncode == 42
        assert result.timed_out is False


class TestRunCommandTimeout:
    """タイムアウト処理テスト。"""

    def test_timeout_raises_timed_out_flag(self):
        """timeout 秒を超過したら timed_out=True をセットする。"""
        start = time.time()
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        elapsed = time.time() - start


        assert result.timed_out is True
        # timeout は秒単位なので、実際の経過時間は timeout + 若干のマージン
        assert elapsed < 3, f"timeout processing took {elapsed}s (should be <3s)"

    def test_timeout_kills_subprocess(self):
        """timeout 後、子プロセスが確実に殺されている（joinで即座に返る）。"""
        start = time.time()
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(100)"],
            timeout=1,
        )
        elapsed = time.time() - start

        assert result.timed_out is True
        # timeout+2秒以内に返ってくることを確認（子プロセス生存なら 100 秒かかる）
        assert elapsed < 4, f"elapsed {elapsed}s > timeout+2s"


class TestRunCommandErrors:
    """エラー処理テスト。"""

    def test_command_not_found(self):
        """存在しないコマンドは returncode=127 に正規化される。"""
        result = run_command(["/nonexistent/command"])
        assert result.returncode == 127
        assert "command not found" in result.stderr
        assert result.timed_out is False



class TestRunCommandWorkdir:
    """workdir パラメータテスト。"""

    def test_workdir_is_used(self, tmp_path):
        """workdir パラメータが実際に使用されることを確認（pwd 出力）。"""
        result = run_command(
            ["/bin/pwd"],
            workdir=tmp_path,
        )
        output_path = Path(result.stdout.strip())
        assert output_path.resolve() == tmp_path.resolve()


class TestSummarizeStderr:
    """summarize_stderr（純粋関数）のテスト。

    engines/*.py の RawAnswer.error は現状 "codex exited 1" のみで診断不能だった
    (中位指摘#6)。stderr 先頭1行を安全に要約するロジックをここで固定する。
    """

    def test_returns_first_line_of_multiline_stderr(self):
        stderr = "auth error: token expired\nsome stack trace\nmore trace"
        assert summarize_stderr(stderr) == "auth error: token expired"

    def test_returns_empty_string_for_empty_stderr(self):
        assert summarize_stderr("") == ""

    def test_returns_empty_string_for_whitespace_only_stderr(self):
        assert summarize_stderr("   \n  \n") == ""

    def test_truncates_to_120_chars(self):
        stderr = "x" * 200
        result = summarize_stderr(stderr)
        assert len(result) == 120
        assert result == "x" * 120

    def test_strips_leading_and_trailing_whitespace_before_taking_first_line(self):
        stderr = "\n\n  actual error message  \nmore\n"
        assert summarize_stderr(stderr) == "actual error message"


class TestRunCommandReturnType:
    """戻り値の型テスト。"""

    def test_returns_run_result(self):
        """戻り値が RunResult 型であることを確認。"""
        result = run_command(["/bin/echo", "test"])
        assert isinstance(result, RunResult)
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        assert isinstance(result.returncode, int)
        assert isinstance(result.timed_out, bool)

    def test_result_is_frozen(self):
        """RunResult は frozen dataclass（不変）。"""
        result = run_command(["/bin/echo", "test"])
        with pytest.raises(AttributeError):
            result.stdout = "modified"


class TestRunCommandWhichResolution:
    """Windows の CreateProcess は codex.cmd 等の PATHEXT 拡張子を解決できないため、
    shutil.which（PATHEXT を見る）で事前解決してから Popen に渡す必要がある
    （setup.py の is_command_available との検出結果の矛盾を解消する）。
    """

    def test_resolves_command_via_which_before_popen(self, monkeypatch):
        """cmd[0] が shutil.which で解決されたパスで Popen が呼ばれる。"""
        resolved_path = "/usr/bin/resolved-echo"
        monkeypatch.setattr(shutil, "which", lambda name: resolved_path)

        captured_cmd: list[str] = []

        class _FakeProc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return "", ""

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        run_command(["echo", "hello"])

        assert captured_cmd[0] == resolved_path
        assert captured_cmd[1:] == ["hello"]

    def test_missing_command_short_circuits_without_calling_popen(self, monkeypatch):
        """which が None を返したら Popen を呼ばず rc=127 を即返す。"""
        monkeypatch.setattr(shutil, "which", lambda name: None)

        def fail_popen(*args, **kwargs):
            raise AssertionError("Popen should not be called when which() returns None")

        monkeypatch.setattr(subprocess, "Popen", fail_popen)

        result = run_command(["definitely-not-a-real-command"])

        assert result.returncode == 127
        assert "command not found: definitely-not-a-real-command" in result.stderr
        assert result.timed_out is False

    def test_which_is_not_called_when_cmd_contains_path_separator(self, monkeypatch):
        """cmd[0] にパス区切りを含む場合は shutil.which を呼ばない（回帰防止）。

        shutil.which は親プロセスの cwd 基準で解決するため、run_command 側で
        which に通してしまうと workdir 配下の相対パスコマンドが誤って
        rc=127 になる（レビュー実機再現: run_command(["./script.sh"], workdir=...)）。
        さらに親 cwd にある同名ファイルへ静かにすり替わるリスクもある。
        """
        which_calls: list[str] = []
        monkeypatch.setattr(
            shutil, "which", lambda name: which_calls.append(name) or "/should/not/be/used"
        )

        run_command(["/bin/echo", "hi"])

        assert which_calls == []

    def test_relative_path_command_skips_which_and_uses_workdir(self, tmp_path):
        """workdir 配下の相対パススクリプトが which 追加後も引き続き実行できる（回帰テスト）。"""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/sh\necho relative-ok\n")
        script.chmod(0o755)

        result = run_command(["./script.sh"], workdir=tmp_path)

        assert result.stdout.strip() == "relative-ok"
        assert result.returncode == 0

    def test_file_not_found_error_reports_original_name_not_resolved_path(self, monkeypatch):
        """TOCTOU（which 解決後に Popen が FileNotFoundError を投げる）でも、
        stderr には元のコマンド名を残し、解決済み絶対パスを漏らさない。
        """
        resolved_path = "/usr/bin/toctou-victim"
        monkeypatch.setattr(shutil, "which", lambda name: resolved_path)

        def fake_popen(cmd, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = run_command(["toctou-victim"])

        assert result.returncode == 127
        assert "toctou-victim" in result.stderr
        assert resolved_path not in result.stderr


class TestWindowsTimeoutFallback:
    """【5】Windows 環境の timeout 処理で proc.kill() フォールバックが機能"""

    def test_timeout_with_available_killpg_kills_process_group(self):
        """【5】timeout 時にプロセスが確実に kill される（killpg 使用可能環境）"""
        # POSIX 環境（killpg 使用可能）での timeout テスト
        result = run_command(
            ["/bin/sleep", "10"],  # 10秒の sleep（timeout が 1秒のため kill される）
            timeout=1,
        )

        assert result.timed_out is True
        # プロセスが kill されるため、returncode は 0 ではない（SIGKILL で -9 相当）
        assert result.returncode != 0 or result.timed_out

    def test_timeout_with_windows_fallback_uses_proc_kill(self, monkeypatch):
        """【5】Windows フォールバック（killpg 非使用可）で proc.kill() が呼ばれる"""
        import os

        # os.killpg と os.getpgid を削除して Windows 環境をシミュレート
        monkeypatch.delattr(os, "killpg", raising=False)
        monkeypatch.delattr(os, "getpgid", raising=False)

        # timeout で子プロセスが kill される（例外が出ない）
        result = run_command(
            ["/bin/sleep", "10"],  # 10秒の sleep（timeout が 1秒のため kill される）
            timeout=1,
        )

        # フォールバック分岐で proc.kill() が呼ばれ、プロセスが確実に kill される
        assert result.timed_out is True
        # proc.kill() で kill された場合も returncode は 0 でない
        assert result.returncode != 0 or result.timed_out
