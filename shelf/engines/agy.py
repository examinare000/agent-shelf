"""
Gemini agy エンジン（agy CLI 経由）。

-p でプロンプト指定、--print-timeout でタイムアウト指定。
テキスト出力のみ（JSON パースなし）。
"""
from __future__ import annotations

from shelf.engines.runner import run_command, summarize_stderr
from shelf.ports import RawAnswer


def build_agy_cmd(prompt: str, timeout: int) -> list[str]:
    """agy コマンド引数を組み立てる（純粋関数）。

    Args:
        prompt: 投入するプロンプト
        timeout: 実行timeout秒（--print-timeout で指定）

    Returns:
        コマンドと引数のリスト
    """
    cmd = [
        "agy",
        "-p",
        prompt,
        "--print-timeout",
        f"{timeout}s",
    ]
    return cmd


class AgyBackend:
    """agy CLI を使用するエンジン実装。"""

    name = "agy"

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout

    def answer(self, prompt: str, workdir, schema: dict | None) -> RawAnswer:
        """agy に質問を投げ、テキスト出力をそのまま返す。

        Args:
            prompt: エンジンに投入するプロンプト
            workdir: 使用されない（agy CLI は -C をサポートしない）
            schema: 使用されない（agy CLI は schema をサポートしない）

        Returns:
            RawAnswer（text, ok, error）
        """
        try:
            cmd = build_agy_cmd(prompt, self.timeout)

            # -p と stdin 併用時の結合挙動は未検証のため、現状は argv 経由でプロンプトを
            # 渡す（実機ヘルプには "-p ... Appended to input on stdin (if any)" とあり、
            # agy 自体が stdin 非対応というわけではない。ARG_MAX 上限・ps 等でプロンプト
            # が可視になる制約は既知の上でこの方式を採用している）。
            result = run_command(
                cmd,
                stdin_text=None,
                timeout=self.timeout,
                workdir=None,
            )

            # 結果を判定
            if result.timed_out:
                return RawAnswer(
                    text="",
                    ok=False,
                    error=f"agy timed out after {self.timeout}s",
                )
            elif result.returncode == 127:
                return RawAnswer(
                    text="",
                    ok=False,
                    error="command not found",
                )
            elif result.returncode != 0:
                # stderr 先頭1行を含めることで診断可能にする(中位指摘#6)。
                detail = summarize_stderr(result.stderr)
                suffix = f": {detail}" if detail else ""
                return RawAnswer(
                    text="",
                    ok=False,
                    error=f"agy exited {result.returncode}{suffix}",
                )

            # stdout をそのまま返す（JSON パースなし）
            return RawAnswer(text=result.stdout, ok=True, error=None)

        except Exception as e:
            return RawAnswer(
                text="",
                ok=False,
                error=f"agy error: {type(e).__name__}",
            )
