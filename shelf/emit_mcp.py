"""`shelf emit-mcp`: claude/codex/gemini 向け MCP 設定ファイルを生成する。

設定への直接書込みはしない（ファイル生成のみが本機能の正）。生成されたファイルを
どう取り込むかは利用者の判断に委ね、本モジュールは「正しい形式のファイルを吐く」
ことだけに責務を絞る（実 CLI 登録・実設定ファイル書換えは行わない）。

claude.sh は `claude mcp add` コマンド列（実行権限付き）、codex.toml/gemini.json
はそれぞれの MCP 設定断片。stdio 接続は `uv run --directory <repo_root> shelf serve`
を argv に使う（repo_root は本ファイルの位置から自己解決する。config.PACKAGE_ROOT
と同じ解決規則）。
"""
from __future__ import annotations

import json
from pathlib import Path

HOST_CHOICES: tuple[str, ...] = ("claude", "codex", "gemini")
_FILENAMES: dict[str, str] = {
    "claude": "claude.sh",
    "codex": "codex.toml",
    "gemini": "gemini.json",
}


def default_repo_root() -> Path:
    """shelf/emit_mcp.py から見たプロジェクトルート（config.PACKAGE_ROOT と同じ規則）。"""
    return Path(__file__).resolve().parent.parent


def build_stdio_argv(repo_root: Path) -> list[str]:
    """stdio トランスポート起動コマンドの argv を組み立てる（純粋関数）。

    `uv run --directory <repo_root自己解決>` により、生成されたファイルがどこに
    置かれても shelf パッケージの場所を明示的に指定して起動できる。
    """
    return ["uv", "run", "--directory", str(repo_root), "shelf", "serve"]


def build_claude_sh_text(*, transport: str, url: str | None, repo_root: Path) -> str:
    """`claude mcp add` コマンド列を含む実行可能スクリプトのテキストを組み立てる。"""
    if transport == "http":
        command_line = f'claude mcp add --transport http shelf "{url}"'
    else:
        argv = " ".join(build_stdio_argv(repo_root))
        command_line = f"claude mcp add shelf -- {argv}"
    return (
        "#!/usr/bin/env bash\n"
        "# shelf MCP サーバを Claude Code に登録する(`shelf emit-mcp` が生成)。\n"
        "# 内容を確認のうえ実行してください。\n"
        "set -euo pipefail\n"
        f"{command_line}\n"
    )


def build_codex_toml_text(*, transport: str, url: str | None, repo_root: Path) -> str:
    """codex `[mcp_servers.shelf]` 設定断片を組み立てる。"""
    if transport == "http":
        return f'[mcp_servers.shelf]\nurl = "{url}"\n'
    argv = build_stdio_argv(repo_root)
    args_toml = ", ".join(f'"{a}"' for a in argv[1:])
    return f'[mcp_servers.shelf]\ncommand = "{argv[0]}"\nargs = [{args_toml}]\n'


def build_gemini_json_text(*, transport: str, url: str | None, repo_root: Path) -> str:
    """gemini `mcpServers` 設定断片を組み立てる。"""
    if transport == "http":
        server: dict = {"url": url}
    else:
        argv = build_stdio_argv(repo_root)
        server = {"command": argv[0], "args": argv[1:]}
    return json.dumps({"mcpServers": {"shelf": server}}, ensure_ascii=False, indent=2) + "\n"


_README_INSTRUCTIONS: dict[str, str] = {
    "claude": "- claude.sh: `bash claude.sh` を実行して Claude Code に shelf を登録します。",
    "codex": (
        "- codex.toml: `[mcp_servers.shelf]` の内容を ~/.codex/config.toml へ追記してください。"
    ),
    "gemini": (
        "- gemini.json: `mcpServers.shelf` の内容を gemini CLI の設定ファイルへ"
        "マージしてください。"
    ),
}


def build_readme_text(hosts: list[str]) -> str:
    """実際に生成したファイルのみを案内する README を組み立てる。"""
    lines = ["# shelf MCP 設定ファイル", "", "`shelf emit-mcp` が生成したファイルです。", ""]
    lines.extend(_README_INSTRUCTIONS[host] for host in HOST_CHOICES if host in hosts)
    lines.append("")
    return "\n".join(lines)


def emit(
    *,
    hosts: list[str],
    transport: str,
    url: str | None,
    output_dir: Path,
    repo_root: Path | None = None,
) -> dict[str, Path]:
    """指定 host 向けの MCP 設定ファイル + README を output_dir へ書き出す。

    Raises:
        ValueError: transport="http" なのに url が未指定の場合。
    """
    if transport == "http" and not url:
        raise ValueError("--transport http の場合は --url の指定が必須です")

    root = repo_root if repo_root is not None else default_repo_root()
    output_dir.mkdir(parents=True, exist_ok=True)

    builders = {
        "claude": build_claude_sh_text,
        "codex": build_codex_toml_text,
        "gemini": build_gemini_json_text,
    }

    written: dict[str, Path] = {}
    for host in HOST_CHOICES:
        if host not in hosts:
            continue
        path = output_dir / _FILENAMES[host]
        path.write_text(
            builders[host](transport=transport, url=url, repo_root=root), encoding="utf-8"
        )
        if host == "claude":
            # claude mcp add コマンド列をそのまま実行できるよう +x を付与する。
            path.chmod(path.stat().st_mode | 0o111)
        written[host] = path

    readme_path = output_dir / "README.md"
    readme_path.write_text(build_readme_text(hosts), encoding="utf-8")
    written["readme"] = readme_path

    return written
