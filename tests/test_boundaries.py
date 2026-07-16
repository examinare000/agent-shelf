"""shelf パッケージ内の import 境界を静的に強制する（docs/design-shelf-mcp.md §3, §6）。

なぜ実行時の sys.modules 検査ではなく AST 静的走査か: sqlite3/subprocess/fastembed/
pymupdf4llm/markitdown/mcp/fastmcp を実際に import すると、ONNX モデルのロードや
未インストールの外部 CLI への依存など重い・環境依存の副作用が走る。ast.parse による
静的走査なら、対象パッケージを一切 import せずに「どのファイルが何を import する
文を書いているか」だけを軽量・決定論的に検証できる。関数内・条件分岐内で行われる
lazy import（convert.py の pymupdf4llm/markitdown が典型）も ast.walk で全ノードを
辿ることで検出できる。
"""
from __future__ import annotations

import ast
from pathlib import Path

_SHELF_PACKAGE_DIR = Path(__file__).resolve().parent.parent / "shelf"

# モジュール名(トップレベル) -> それを import してよい唯一のファイル名。
# mcp/fastmcp は server.py 専用。
_RESTRICTED_TO_OWNER: dict[str, str] = {
    "sqlite3": "store.py",
    "subprocess": "runner.py",
    "fastembed": "embedder.py",
    "pymupdf4llm": "convert.py",
    "pymupdf": "convert.py",
    "markitdown": "convert.py",
    "mcp": "server.py",
    "fastmcp": "server.py",
}

# ドメイン層: 外部SDK・DB・subprocessを一切知らず、ポートと純粋関数だけに依存するべき
# モジュール群（docs/design-shelf-mcp.md §3）。routing.py/librarian.py/digests.py は
# 2層レファレンスサービス増分設計（docs/design-shelf-reference-service.md §3/§9-C）が
# 追加した司書・専門家ロジックで、外部依存ゼロの契約を静的に強制する対象へ加える。
# shelving.py/shelver.py は自動分類投入増分設計（同 §13.3/§13.10 V7）が追加した
# 分類判断（純粋）・配線層で、同じ契約を静的に強制する対象へ加える。
_DOMAIN_LAYER_FILES = {
    "service.py", "prompts.py", "ports.py", "names.py", "chunker.py", "search.py",
    "routing.py", "librarian.py", "digests.py", "shelving.py", "shelver.py",
}


def _iter_shelf_python_files() -> list[Path]:
    return sorted(_SHELF_PACKAGE_DIR.rglob("*.py"))


def _imported_top_level_modules(source: str, filename: str) -> set[str]:
    """ファイル内の import 文(関数内含む全ノード)からトップレベルモジュール名を集める。"""
    tree = ast.parse(source, filename=filename)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.add(node.module.split(".")[0])
    return modules


def test_restricted_modules_are_imported_only_by_their_owner_file() -> None:
    violations: list[str] = []
    for path in _iter_shelf_python_files():
        modules = _imported_top_level_modules(path.read_text(encoding="utf-8"), str(path))
        for module_name, owner_filename in _RESTRICTED_TO_OWNER.items():
            if module_name in modules and path.name != owner_filename:
                violations.append(
                    f"{path.relative_to(_SHELF_PACKAGE_DIR.parent)} が "
                    f"{module_name!r} を import している（許可されるのは {owner_filename} のみ）"
                )

    assert violations == [], "\n".join(violations)


def test_domain_layer_modules_have_no_external_dependency_imports() -> None:
    forbidden_modules = set(_RESTRICTED_TO_OWNER)
    violations: list[str] = []
    for path in _iter_shelf_python_files():
        if path.name not in _DOMAIN_LAYER_FILES:
            continue
        modules = _imported_top_level_modules(path.read_text(encoding="utf-8"), str(path))
        hit = modules & forbidden_modules
        if hit:
            violations.append(f"{path.name} が禁止依存を import している: {sorted(hit)}")

    assert violations == [], "\n".join(violations)


def test_domain_layer_files_are_present() -> None:
    """検証対象のファイル名集合が typo でごっそり空にならないことの保険。"""
    present = {path.name for path in _iter_shelf_python_files()}
    missing = _DOMAIN_LAYER_FILES - present
    assert missing == set(), f"ドメイン層ファイルが見つからない: {missing}"


# urllib.request の import 元は engines/ollama.py（新規・Ollama /api/chat 呼び出し）と
# convert.py（既存・shelf add の URL 投入 fetch。design doc §1「URL: http/https のみ許可」）
# の2ファイルに限定する。_RESTRICTED_TO_OWNER は「トップレベルモジュール名 -> 唯一の
# 所有ファイル」しか表現できず、split(".")[0] で "urllib.request" と "urllib.parse"
# がどちらも "urllib" に潰れてしまう。service.py は urllib.parse を正当に import して
# いるため、"urllib" を単純に _RESTRICTED_TO_OWNER へ追加すると誤検知になる。
# urllib.request だけをフルパスで区別する専用チェッカーをここに置く。
_URLLIB_REQUEST_OWNERS = {"ollama.py", "convert.py"}


def _imports_urllib_request(source: str, filename: str) -> bool:
    """`urllib.request`（フルパス）を import しているかを判定する（純粋関数）。"""
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "urllib.request" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "urllib.request":
                return True
    return False


class TestImportsUrllibRequestDetector:
    """_imports_urllib_request（純粋関数）自体の判定ロジックを検証する。"""

    def test_detects_plain_import_statement(self):
        assert _imports_urllib_request("import urllib.request\n", "x.py") is True

    def test_detects_import_from_statement(self):
        source = "from urllib.request import urlopen\n"
        assert _imports_urllib_request(source, "x.py") is True

    def test_ignores_urllib_parse(self):
        source = "from urllib.parse import urlparse\n"
        assert _imports_urllib_request(source, "x.py") is False

    def test_ignores_unrelated_import(self):
        assert _imports_urllib_request("import json\n", "x.py") is False


def test_urllib_request_is_imported_only_by_its_designated_owners() -> None:
    violations: list[str] = []
    for path in _iter_shelf_python_files():
        if not _imports_urllib_request(path.read_text(encoding="utf-8"), str(path)):
            continue
        if path.name not in _URLLIB_REQUEST_OWNERS:
            violations.append(
                f"{path.relative_to(_SHELF_PACKAGE_DIR.parent)} が urllib.request を "
                f"import している（許可されるのは {sorted(_URLLIB_REQUEST_OWNERS)} のみ）"
            )

    assert violations == [], "\n".join(violations)
