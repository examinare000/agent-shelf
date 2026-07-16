"""
Google Gemini エンジン（gemini CLI 経由）。

-p でプロンプト指定、-o json で JSON 出力。response フィールドから応答を抽出。
"""
from __future__ import annotations

import json

from shelf.engines.runner import run_command, summarize_stderr
from shelf.ports import RawAnswer


def build_gemini_cmd(prompt: str) -> list[str]:
    """gemini コマンド引数を組み立てる（純粋関数）。

    Args:
        prompt: 投入するプロンプト

    Returns:
        コマンドと引数のリスト

    Notes:
        gemini CLI にタイムアウト指定オプションは存在しない。以前は agy の
        --print-timeout からの誤コピペで timeout 引数を受け取り、docstring でも
        「--print-timeout で指定」と主張していたが、実際には未使用の死引数
        だった(中位指摘#6)。タイムアウトは呼び出し側の run_command(timeout=...)
        が subprocess レベルで強制する。
    """
    cmd = [
        "gemini",
        "-p",
        prompt,
        "--approval-mode",
        "plan",
        "-o",
        "json",
    ]
    return cmd


class GeminiCliBackend:
    """gemini CLI を使用するエンジン実装。"""

    name = "gemini"

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout

    def answer(self, prompt: str, workdir, schema: dict | None) -> RawAnswer:
        """gemini に質問を投げ、JSON 出力から応答を抽出する。

        Args:
            prompt: エンジンに投入するプロンプト
            workdir: 使用されない（gemini CLI は -C をサポートしない）
            schema: 使用されない（gemini CLI は schema をサポートしない）

        Returns:
            RawAnswer（text, ok, error）
        """
        try:
            cmd = build_gemini_cmd(prompt)

            # -p と stdin 併用時の結合挙動は未検証のため、現状は argv 経由でプロンプトを
            # 渡す（実機ヘルプには "-p ... Appended to input on stdin (if any)" とあり、
            # gemini 自体が stdin 非対応というわけではない。ARG_MAX 上限・ps 等で
            # プロンプトが可視になる制約は既知の上でこの方式を採用している）。
            result = run_command(
                cmd,
                stdin_text=None,
                timeout=self.timeout,
                workdir=None,
            )

            # 出力から応答を抽出
            text = ""
            if result.timed_out:
                return RawAnswer(
                    text="",
                    ok=False,
                    error=f"gemini timed out after {self.timeout}s",
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
                    error=f"gemini exited {result.returncode}{suffix}",
                )

            # JSON パースを試みる
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict) and "response" in data:
                    text = str(data["response"])
                else:
                    # response フィールドがない場合は stdout 全文
                    text = result.stdout
            except json.JSONDecodeError:
                # JSON パース失敗時は stdout 全文を text に
                text = result.stdout

            return RawAnswer(text=text, ok=True, error=None)

        except Exception as e:
            return RawAnswer(
                text="",
                ok=False,
                error=f"gemini error: {type(e).__name__}",
            )
