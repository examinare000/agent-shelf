"""
Subprocess 一本化: すべてのエンジン呼び出しの統一インターフェース。

timeout + killpg + TemporaryDirectory で安全・確実なコマンド実行を実現。
プロジェクトで subprocess を import してよい唯一のファイル。
"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


_STDERR_SUMMARY_MAX_LEN = 120


def summarize_stderr(stderr: str) -> str:
    """stderr 先頭1行を診断用に安全な要約にする(120字切詰・改行除去)。

    engines/*.py の RawAnswer.error に "codex exited 1" のような returncode のみを
    含めると、認証切れ・quota超過等の具体的な原因が診断不能になる。全文ではなく
    先頭1行のみを使うことで、長大なスタックトレース等がエラーメッセージに
    漏れるのを防ぎつつ、最低限の手がかりを残す。
    """
    lines = stderr.strip().splitlines()
    first_line = lines[0].strip() if lines else ""
    return first_line[:_STDERR_SUMMARY_MAX_LEN]


@dataclass(frozen=True)
class RunResult:
    """コマンド実行結果。stdout/stderr キャプチャ・timeout フラグ付き。"""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


def run_command(
    cmd: list[str],
    *,
    stdin_text: str | None = None,
    timeout: int = 300,
    workdir: Path | None = None,
) -> RunResult:
    """
    コマンドを実行し、stdout/stderr/exit code をキャプチャして返す。

    Args:
        cmd: コマンドと引数のリスト
        stdin_text: 標準入力に渡すテキスト（None なら stdin は閉じる）
        timeout: タイムアウト秒（超過時は killpg で子プロセスごと確実に殺す）
        workdir: 作業ディレクトリ（None なら現在のディレクトリ）

    Returns:
        RunResult（stdout, stderr, returncode, timed_out フラグ）

    Notes:
        - FileNotFoundError（コマンド不在）は returncode=127, stderr="command not found: ..."
          に正規化して返す（例外を漏らさない）。
        - start_new_session=True で新しいプロセスグループを作成し、
          TimeoutExpired 時に os.killpg で子プロセスを確実に殺す。
        - すべての例外を catch して安全な RunResult に変換する。
    """
    try:
        # start_new_session=True: codex など子プロセスを張る CLI を対象。
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workdir,
            start_new_session=True,
        )

        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
            return RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            # 子プロセスグループを確実に殺す（communicate がタイムアウトした場合）。
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                # プロセスがすでに終了している場合など
                pass

            # タイムアウト後の残り出力を回収
            try:
                stdout, stderr = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""

            return RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode if proc.returncode is not None else -1,
                timed_out=True,
            )
    except FileNotFoundError:
        # コマンドが見つからない（シェルなし直接実行）
        cmd_name = cmd[0] if cmd else "command"
        return RunResult(
            stdout="",
            stderr=f"command not found: {cmd_name}",
            returncode=127,
            timed_out=False,
        )
    except Exception as e:
        # 予期しない例外も catch して安全に返す（ただし実装バグ対応として stderr に記録）
        return RunResult(
            stdout="",
            stderr=f"runner error: {type(e).__name__}: {str(e)}",
            returncode=-1,
            timed_out=False,
        )
