"""
runner.py のテスト: subprocess 実行の一本化。

実プロセスは /bin/echo・/bin/cat・sleep スクリプト等の決定論コマンドのみ使用。
検証: stdout capture / stdin 渡し / 非0 returncode / timeout で timed_out=True
かつ所要時間が timeout+2秒以内 / 存在しないコマンド→127 / workdir が効く。
"""
from __future__ import annotations

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
