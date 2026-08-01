"""
Subprocess 一本化: すべてのエンジン呼び出しの統一インターフェース。

timeout + killpg + TemporaryDirectory で安全・確実なコマンド実行を実現。
プロジェクトで subprocess を import してよい唯一のファイル。
"""
from __future__ import annotations

import os
import shutil
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
        - cmd[0] がパス区切りを含まない bare なコマンド名の場合のみ、事前に
          shutil.which で解決してから Popen に渡す。Windows の CreateProcess は
          npm がインストールする codex.cmd 等の PATHEXT 拡張子を解決できず
          FileNotFoundError になる一方、setup.py の is_command_available は
          shutil.which（PATHEXT を見る）で「検出済み」と判定するため、判定と
          実行の矛盾が起きていた（実証: 設計指示のバグ報告）。cmd[0] が相対・絶対
          パスの場合は which をスキップする: shutil.which は「親プロセスの cwd」
          基準で解決するため、which を通すと run_command(["./script.sh"],
          workdir=...) のような workdir 配下の相対パス実行が rc=127 に回帰する
          （レビューで実機再現）。加えて親 cwd の同名ファイルへ静かにすり替わる
          リスクもあるため、パスが明示された呼び出しは常に無変更で Popen に渡す。
    """
    original_name = cmd[0] if cmd else "command"
    if cmd and os.path.dirname(cmd[0]) == "":
        resolved = shutil.which(cmd[0])
        if resolved is None:
            return RunResult(
                stdout="",
                stderr=f"command not found: {original_name}",
                returncode=127,
                timed_out=False,
            )
        cmd = [resolved, *cmd[1:]]

    try:
        # start_new_session=True: codex など子プロセスを張る CLI を対象。
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # エンジン CLI（codex 等）は stdin を UTF-8 必須で読むため、
            # locale.getpreferredencoding（Windows では既定 cp932）に依存させず
            # 明示的に UTF-8 へ固定する（encoding 指定でテキストモードになるため
            # text=True は不要）。errors="replace" は子プロセスの不正バイト出力で
            # runner 自体が例外落ちしないための頑健化。同ポリシーは stdin 書き込み側
            # にも適用され、エンコード不能文字（lone surrogate 等）は例外にならず
            # 置換される点に注意。
            encoding="utf-8",
            errors="replace",
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
            # 子プロセスを確実に殺す（communicate がタイムアウトした場合）。
            # os.killpg/getpgid は Windows には存在せず AttributeError になり、
            # 外側の except Exception に飲まれて timed_out=False の runner error に
            # 化けて子プロセスが放置されていた（実証: TestRunCommandTimeout の red）。
            if os.name == "nt":
                # Windows: .cmd 経由起動では直接の子が cmd.exe のため、proc.kill() だけ
                # では孫の node.exe が孤児化する。taskkill /T（PID の親子ツリーを辿って
                # kill）/F（強制）でプロセスツリーごと殺す。subprocess.run は check=True
                # を指定しない限り非0 returncode で例外を投げないため、例外発生時だけで
                # なく returncode != 0（未インストール・権限不足等）でも proc.kill() に
                # フォールバックし、直接の子だけは確実に殺す。
                try:
                    result = subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode != 0:
                        proc.kill()
                except Exception:
                    proc.kill()
            elif hasattr(os, "killpg"):
                # POSIX: プロセスグループごと殺す（孫プロセスまで確実に殺すため）。
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    # プロセスがすでに終了している場合など
                    pass
            else:
                # 上記いずれにも該当しない未知環境向けのフォールバック。
                proc.kill()

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
        # コマンドが見つからない（シェルなし直接実行）。cmd[0] は which 解決後の
        # 絶対パスに書き換わっている可能性があるため、TOCTOU（解決直後にファイルが
        # 消える等）で到達した場合でも解決前の original_name を報告する
        # （レビュー指摘: 解決済みパスをエラーメッセージに漏らさない）。
        return RunResult(
            stdout="",
            stderr=f"command not found: {original_name}",
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
