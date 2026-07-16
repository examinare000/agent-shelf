"""
OpenAI codex エンジン（codex exec 経由）。

--output-schema で厳格 JSON 強制、--ephemeral で副作用なし、read-only サンドボックス。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from shelf.engines.runner import run_command, summarize_stderr
from shelf.ports import RawAnswer


def build_codex_cmd(
    workdir: Path,
    out_path: Path,
    schema_path: Path | None = None,
) -> list[str]:
    """codex exec コマンド引数を組み立てる（純粋関数）。

    Args:
        workdir: 作業ディレクトリ（-C で指定）
        out_path: 出力ファイルパス（-o で指定）
        schema_path: JSON スキーマファイルパス。None ならば --output-schema を省く

    Returns:
        コマンドと引数のリスト
    """
    cmd = [
        "codex",
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(workdir),
        "-o",
        str(out_path),
    ]

    if schema_path is not None:
        cmd.extend(["--output-schema", str(schema_path)])

    # stdin で prompt を読む（- で stdin 指定）
    cmd.append("-")

    return cmd


class CodexBackend:
    """codex exec CLI を使用するエンジン実装。"""

    name = "codex"

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout

    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer:
        """codex に質問を投げ、出力ファイルまたは stdout から結果を抽出する。

        Args:
            prompt: エンジンに投入するプロンプト
            workdir: 読み取り専用サンドボックスのディレクトリ
            schema: JSON スキーマ（codex --output-schema 用）

        Returns:
            RawAnswer（text, ok, error）
        """
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                out_file = tmpdir_path / "answer.txt"

                # schema ファイルを一時ディレクトリに保存
                schema_path = None
                if schema is not None:
                    schema_path = tmpdir_path / "schema.json"
                    schema_path.write_text(json.dumps(schema, ensure_ascii=False))

                # build_codex_cmd で統一的にコマンドを構築
                cmd = build_codex_cmd(
                    workdir=workdir,
                    out_path=out_file,
                    schema_path=schema_path,
                )

                # コマンド実行
                result = run_command(
                    cmd,
                    stdin_text=prompt,
                    timeout=self.timeout,
                    workdir=None,  # codex は -C で workdir を指定済み
                )

                # 出力ファイルから結果を取得
                if out_file.exists() and out_file.stat().st_size > 0:
                    text = out_file.read_text(encoding="utf-8")
                else:
                    # フォールバック: stdout を使用
                    text = result.stdout

                # ok/error を判定
                if result.timed_out:
                    return RawAnswer(
                        text="",
                        ok=False,
                        error=f"codex timed out after {self.timeout}s",
                    )
                elif result.returncode == 127:
                    return RawAnswer(
                        text="",
                        ok=False,
                        error="command not found",
                    )
                elif result.returncode != 0:
                    # stderr 先頭1行を含めることで診断可能にする(中位指摘#6: 現状
                    # "codex exited 1" のみでは認証切れ・quota超過等の原因が
                    # 分からなかった)。
                    detail = summarize_stderr(result.stderr)
                    suffix = f": {detail}" if detail else ""
                    return RawAnswer(
                        text="",
                        ok=False,
                        error=f"codex exited {result.returncode}{suffix}",
                    )
                else:
                    return RawAnswer(text=text, ok=True, error=None)

        except Exception as e:
            return RawAnswer(
                text="",
                ok=False,
                error=f"codex error: {type(e).__name__}",
            )
