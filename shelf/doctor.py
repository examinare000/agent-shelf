"""`shelf doctor`: 起動前の環境プリフライト診断（読み取り専用）。

なぜこのモジュールを分けるか: ヘッドレス Windows サーバ運用（Tailscale 経由 MCP）
では「エンジン CLI が見つからない」「ollama が落ちている」「DB パスが書けない」
といった環境不備を、実際に `shelf serve`/`shelf ask` を叩くまで発見できなかった
（Task Scheduler 等の無人運用ではその発見が遅れるほど障害が長引く）。本モジュールは
各種検査を list[CheckResult] という純データへ集約するだけの層に留め、LLM 呼び出し・
モデルダウンロード・カタログへの書込みは一切行わない。DB 到達性チェック
（check_db_open）は「DB ファイルが既に存在する場合のみ」開いて close する。
Store(db_path) は未作成パスに対して親ディレクトリの mkdir・全スキーマの
CREATE TABLE・WAL 化まで行うため、無条件に開くと setup 前の既定パスに孤立した
本番カタログ DB を生成してしまい「読み取り専用」の宣言と矛盾する
（レビュー指摘 must-1 対応。詳細は check_db_open の docstring 参照）。

boundaries（tests/test_boundaries.py の import ガード）との関係: sqlite3/
subprocess/mcp/fastembed/pymupdf 系トップレベルモジュールは本ファイルでは一切
import しない。DB 到達性チェックは sqlite3 を直接触る store.py（Store の唯一の
所有者）へ委譲し、本ファイルは Store クラスを注入可能な store_factory として
受け取るだけ（既定値は shelf.store.Store をそのまま使う）。CLI 検出・ollama 疎通確認は、
それぞれ shutil.which・urllib.request を単独で所有する shelf.setup /
shelf.engines.ollama の既存公開関数（is_command_available / is_reachable）を
そのまま再利用する。所有ファイルはそれぞれ既に閉じているため、本ファイルからの
再 import は boundaries 違反にならない（test_boundaries.py はモジュール名の
トップレベル import だけを見るため、"shelf.setup"/"shelf.engines.ollama" の
import は "sqlite3"/"urllib.request" の import としてカウントされない）。
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from shelf import config
from shelf.engines.ollama import is_reachable
from shelf.setup import is_command_available
from shelf.store import Store


class CheckResult(NamedTuple):
    """1件の診断結果。plain tuple (name, ok, detail) とも等価に比較できる。"""

    name: str
    ok: bool
    detail: str


def check_engine_cli(
    display_name: str, command: str, *, which: Callable[[str], bool]
) -> CheckResult:
    """`command` が PATH 上に見つかるかを検査する（`shutil.which` 相当を注入）。"""
    ok = which(command)
    detail = (
        f"{command} コマンドが見つかりました" if ok else f"{command} コマンドが見つかりません"
    )
    return CheckResult(name=f"engine:{display_name}", ok=ok, detail=detail)


def check_ollama(url: str, *, reachable: Callable[[str], bool]) -> CheckResult:
    """ollama デーモンへの HTTP 疎通を検査する（`shelf.engines.ollama.is_reachable` を注入）。"""
    ok = reachable(url)
    detail = f"{url} へ疎通できました" if ok else f"{url} へ疎通できません"
    return CheckResult(name="ollama", ok=ok, detail=detail)


def _can_write_to(directory: Path) -> bool:
    """ディレクトリへ実際に一時ファイルを作成・削除できるかで書込み可否を判定する。

    os.access(path, os.W_OK) は POSIX の権限ビットしか見ない実装のため、Windows
    では NTFS ACL による拒否を検出できず「書込み不可なのに os.access は True を
    返す」既知の制約がある（本ツールの主対象がヘッドレス Windows サーバである
    ため無視できない。レビュー指摘 should-3 対応）。実際に書込みを試みる方が
    プラットフォームを問わず正確なため、こちらを採用する。tempfile の
    delete=True により検査後にファイルは残らない。
    """
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=True):
            pass
        return True
    except OSError:
        return False


def check_db_parent_dir(db_path: str | Path) -> CheckResult:
    """DB パスの書込み先が書込み可能かを検査する。

    親ディレクトリ自体が未作成でも Store 構築時に mkdir(parents=True) される
    ため即失敗にはせず、実在する最も近い祖先ディレクトリまで遡って書込み権限を
    確認する（初回起動でまだ .catalog/ が存在しない状態を誤検知しないため）。
    """
    parent = Path(db_path).parent
    existing = parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent

    if not existing.exists():
        return CheckResult(
            name="db_parent_dir", ok=False, detail=f"祖先ディレクトリが見つかりません: {parent}"
        )
    if not _can_write_to(existing):
        return CheckResult(
            name="db_parent_dir", ok=False, detail=f"書込み権限がありません: {existing}"
        )
    return CheckResult(name="db_parent_dir", ok=True, detail=f"書込み可能です: {parent}")


def check_db_open(
    db_path: str | Path, *, store_factory: Callable[[str | Path], object] = Store
) -> CheckResult:
    """DB ファイルを実際に開いて閉じられるかを検査する。

    DB ファイルがまだ存在しない場合は store_factory を一切呼ばない。Store(db_path)
    は未作成パスに対して親ディレクトリの mkdir・全スキーマの CREATE TABLE・WAL 化
    まで行うため、無条件に開くと「読み取り専用の診断」であるはずの doctor が
    setup 前の既定パスに孤立した本番カタログ DB を生成してしまう
    （レビュー指摘 must-1 対応）。ファイルが存在する場合のみ、そのまま開いて
    close する（open 自体が失敗しうる唯一のケース = 権限・破損DB・ロックを検出）。

    sqlite3 を直接 import してよいのは store.py のみ（boundaries）のため、
    Store 相当のコンストラクタを store_factory として注入させる（既定は
    shelf.store.Store）。例外詳細はクラス名のみに留めパス以外の内部情報を
    漏らさない（engines/ollama.py の RawAnswer.error と同じ「安全な要約」方針）。
    """
    if not Path(db_path).exists():
        return CheckResult(
            name="db_open",
            ok=True,
            detail=f"DB は未作成です(初回 index/setup 時に作成されます): {db_path}",
        )
    try:
        store = store_factory(db_path)
    except Exception as e:  # noqa: BLE001 - 想定外の失敗も安全に要約して返す診断のため
        return CheckResult(
            name="db_open", ok=False, detail=f"DB を開けませんでした: {type(e).__name__}"
        )
    store.close()
    return CheckResult(name="db_open", ok=True, detail=f"DB を開けました: {db_path}")


def check_corpus_dir(corpus_dir: str | Path) -> CheckResult:
    """corpus ディレクトリ（資料本体の置き場）が存在するかを検査する。

    存在しないと索引化・検索の対象がそもそも無いため、config.env の存在有無
    （後述 check_config_env）とは異なり ok/ng を直接反映する。
    """
    ok = Path(corpus_dir).is_dir()
    detail = f"corpus ディレクトリが見つかりました: {corpus_dir}" if ok else (
        f"corpus ディレクトリが見つかりません: {corpus_dir}"
    )
    return CheckResult(name="corpus_dir", ok=ok, detail=detail)


def resolve_fastembed_cache_dir() -> Path:
    """fastembed が実際にモデルを保存するキャッシュディレクトリを解決する。

    fastembed.common.utils.define_cache_dir と同じ優先順位（env
    FASTEMBED_CACHE_PATH > 既定 <tempdir>/fastembed_cache）をここで再現する。
    fastembed 自体は本ファイルで import できない（boundaries: embedder.py 専用）ため、
    ロジックだけを複製する（fastembed 側の既定値が変わらない限り安全な複製）。
    """
    default_cache_dir = Path(tempfile.gettempdir()) / "fastembed_cache"
    return Path(os.environ.get("FASTEMBED_CACHE_PATH", str(default_cache_dir)))


def check_fastembed_cache(cache_dir: str | Path) -> CheckResult:
    """fastembed キャッシュディレクトリの存在有無を報告する（情報提供のみ）。

    未作成でも初回起動時は正常な状態（初回 embed 実行時に自動作成・DL される）
    であり、モデルロード自体はここでは行わないためドクター診断としては ok=True
    に固定する（config_env と同じ「存在有無を知らせるが失敗にはしない」方針）。
    """
    exists = Path(cache_dir).is_dir()
    detail = (
        f"fastembed キャッシュが見つかりました: {cache_dir}"
        if exists
        else f"fastembed キャッシュはまだ未作成です(初回embed時に自動作成): {cache_dir}"
    )
    return CheckResult(name="fastembed_cache", ok=True, detail=detail)


def check_config_env(config_path: str | Path) -> CheckResult:
    """config.env（`shelf setup` が書き出す永続設定）の場所と存在有無を報告する。

    config.env は任意設定で、無くてもハードコード既定値で継続動作する
    （shelf/config.py の parse_config_file と同じフェイルソフト方針）ため、
    fastembed_cache と同様に ok=True 固定の情報提供チェックとする。
    """
    exists = Path(config_path).is_file()
    detail = (
        f"config.env が見つかりました: {config_path}"
        if exists
        else f"config.env はまだ未作成です(既定値で動作): {config_path}"
    )
    return CheckResult(name="config_env", ok=True, detail=detail)


# エンジンCLI検出の対象・順序。ollama は subcommand として独立の check_ollama
# （HTTP疎通）で扱うためここには含めない（setup.py の PROVIDER_CHOICES とは
# 別関心事: setup は「セットアップ対話で提示する選択肢」、doctor は「疎通確認」）。
_ENGINE_CLI_COMMANDS: dict[str, str] = {"codex": "codex", "gemini": "gemini", "agy": "agy"}


def run_checks(
    *,
    which: Callable[[str], bool] = is_command_available,
    ollama_url: str | None = None,
    reachable: Callable[[str], bool] = is_reachable,
    db_path: str | Path | None = None,
    store_factory: Callable[[str | Path], object] = Store,
    corpus_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    fastembed_cache_dir: str | Path | None = None,
) -> list[CheckResult]:
    """全診断項目を実行し list[CheckResult] にまとめる(`shelf doctor` CLI の唯一の窓口)。

    各パス/依存は省略時 config.py の実値・実環境（shutil.which・ollama疎通・実DB
    open/close）を見るが、全て kwargs で注入できるためテストは実環境に一切触れない。
    """
    resolved_db_path = db_path if db_path is not None else config.DB_PATH
    resolved_corpus_dir = corpus_dir if corpus_dir is not None else config.CORPUS_DIR
    resolved_config_path = (
        config_path if config_path is not None else config.resolve_config_path()
    )
    resolved_fastembed_cache_dir = (
        fastembed_cache_dir if fastembed_cache_dir is not None else resolve_fastembed_cache_dir()
    )
    resolved_ollama_url = ollama_url if ollama_url is not None else config.OLLAMA_URL

    results = [
        check_engine_cli(name, command, which=which)
        for name, command in _ENGINE_CLI_COMMANDS.items()
    ]
    results.append(check_ollama(resolved_ollama_url, reachable=reachable))
    results.append(check_db_parent_dir(resolved_db_path))
    results.append(check_db_open(resolved_db_path, store_factory=store_factory))
    results.append(check_corpus_dir(resolved_corpus_dir))
    results.append(check_config_env(resolved_config_path))
    results.append(check_fastembed_cache(resolved_fastembed_cache_dir))
    return results
