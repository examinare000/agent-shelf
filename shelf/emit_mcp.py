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
    """`claude mcp add` コマンド列を含む実行可能スクリプトのテキストを組み立てる。

    transport="http" の場合 url は必須（build_codex_toml_text/
    build_gemini_json_text と同じ契約・同じ `not url` 判定）。従来はガードが
    無く、url=None/"" のまま素の f-string 補間（エスケープ無し）で
    `claude mcp add --transport http shelf "None"` のような壊れたコマンド列を
    無例外で出力していた。出力先が実行ビット付き claude.sh（emit() が chmod で
    +x を付与）であるため、3 ビルダーの中で最も影響が大きい実穴だった。
    なお url 内容のシェルエスケープ（`"` や `$(...)` の無害化）は本ガードでは
    未対処のまま（shlex.quote 化は別タスク）。
    """
    if transport == "http":
        if not url:
            raise ValueError("--transport http の場合は url が必須です")
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


def _toml_basic_string(value: str) -> str:
    r"""TOML basic string へ埋め込むための最小限エスケープ（`\`→`\\`、`"`→`\"`）。

    WHY: Windows の repo_root は str() がバックスラッシュ区切り（例:
    `C:\Users\...`）になる。TOML の basic string はバックスラッシュを
    エスケープ導入文字として扱うため、無エスケープで埋め込むと `\U`・`\u` 等が
    不正な Unicode エスケープと解釈され tomllib.TOMLDecodeError になる
    （実測: Windows CI で "Invalid hex value"）。TOML 1.0 のエスケープ規則に
    従い、バックスラッシュとダブルクォートのみを最小限エスケープする
    （このモジュールが埋め込む値はコマンド名・パス・URL に限られ、
    制御文字を含む想定はしていない）。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_codex_toml_text(*, transport: str, url: str | None, repo_root: Path) -> str:
    """codex `[mcp_servers.shelf]` 設定断片を組み立てる。

    transport="http" の場合 url は必須（呼び出し元 emit() の事前検証と同じ契約:
    `transport == "http" and not url` で拒否する）。ここでも明示的に検証すること
    で、emit() を経由せず直接呼ばれた場合に url=None/"" を黙って
    `_toml_basic_string` へ渡し `AttributeError`（None の場合）や壊れた
    `url = ""` の無例外書き出し（空文字列の場合）という診断しにくい形で壊れる
    のを防ぐ（pyright: reportArgumentType の指摘を機に、既存の暗黙のクラッシュを
    明示的なエラーへ）。`not url` を使う理由: `url is None` だけでは argparse で
    `--url ""` のように空文字列が渡るケースを見逃す（emit() の判定式と揃える）。
    """
    if transport == "http":
        if not url:
            raise ValueError("--transport http の場合は url が必須です")
        return f'[mcp_servers.shelf]\nurl = "{_toml_basic_string(url)}"\n'
    argv = build_stdio_argv(repo_root)
    args_toml = ", ".join(f'"{_toml_basic_string(a)}"' for a in argv[1:])
    return f'[mcp_servers.shelf]\ncommand = "{_toml_basic_string(argv[0])}"\nargs = [{args_toml}]\n'


def build_gemini_json_text(*, transport: str, url: str | None, repo_root: Path) -> str:
    """gemini `mcpServers` 設定断片を組み立てる。

    transport="http" の場合 url は必須（build_codex_toml_text/
    build_claude_sh_text と同じ契約・同じ `not url` 判定）。従来はガードが無く、
    url=None/"" を無例外で `httpUrl: null`/`httpUrl: ""` として書き出していた。
    build_codex_toml_text の修正時のレビューで、`emit()` の `builders` dict
    （claude/codex/gemini の3エントリ）を悉皆的に grep せず gemini だけを
    見つけて直した結果、兄弟関数が2つ（claude・gemini）あるうち gemini しか
    掃引できず claude 側の同型の穴を1ラウンド見落とした（build_claude_sh_text
    のガードは別途追加）。
    """
    if transport == "http":
        if not url:
            raise ValueError("--transport http の場合は url が必須です")
        # Gemini CLI 公式ドキュメント（settings.json の mcpServers 仕様）では、
        # streamable-http 接続は "httpUrl" キーで指定し、"url" キーは SSE
        # transport 用と区別されている。ただし実機（実 Gemini CLI 起動）では
        # 未検証のため、Gemini CLI のバージョンによっては挙動が異なる可能性がある。
        server: dict = {"httpUrl": url}
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
