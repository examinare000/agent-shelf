"""env で上書き可能な設定値の解決。

recall/config.py と同じ設計思想: 実運用値をモジュール定数として解決しつつ、
env 変数で上書きできるようにする。これによりテスト時は本物の corpus/DB/モデルへ
副作用を及ぼさず、:memory: DB や一時ディレクトリを指す値へ差し替えて検証できる
（monkeypatch.setenv + importlib.reload で再解決させるテストパターンを想定）。

int/bool への変換に失敗した値（例 SHELF_TOP_K=abc）は例外を送出せず既定値へ
フォールバックする。設定ミスで起動不能になるより、既定動作で継続する方が
このツールの性質（ローカル QA 補助）に合うため（フェイルソフト）。
"""
from __future__ import annotations

import os
from pathlib import Path

# digests.py は json/unicodedata/shelf.ports のみに依存する外部SDKゼロの
# leaf モジュール（config.py を import しない）ため、ここから import しても
# 循環importにはならない。既定値5/20/8000の唯一の情報源とすることで、
# コメントでの目視同期（コードレビュー指摘 P13）を廃止する。
from shelf.digests import MAP_DEFAULT_NOTES, REDUCE_DEFAULT_NOTES, WINDOW_DEFAULT_CHARS

# shelf/shelf/config.py から見て shelf/ プロジェクトルート（.catalog/・corpus/ の基準）。
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true")


def _log_level_env(key: str, default_level: int) -> int:
    """文字列 env ("DEBUG"/"INFO" 等) を logging レベル int へ変換。

    大文字小文字を許容し、不正値は既定値へフォールバック（他の *_env ヘルパと同じ方針）。
    """
    import logging

    raw = os.environ.get(key)
    if raw is None:
        return default_level
    level_name = raw.strip().upper()
    level = logging.getLevelNamesMapping().get(level_name)
    return level if level is not None else default_level


# config.env（`shelf setup` が書き出す永続設定）の場所を上書きする env 変数名。
CONFIG_ENV_VAR = "SHELF_CONFIG"


def resolve_config_path() -> Path:
    """config.env の場所を解決する。既定は ~/.config/agent-shelf/config.env。

    SHELF_CONFIG が設定されていればそれを優先する（テストで一時パスへ差し替える
    ためにも使う）。
    """
    raw = os.environ.get(CONFIG_ENV_VAR)
    return Path(raw) if raw else Path.home() / ".config" / "agent-shelf" / "config.env"


def parse_config_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 形式の config.env をパースする（#コメント・空行は無視）。

    ファイルが存在しない/読み取れない場合は空 dict を返す（config.env は任意の
    永続設定であり、無くても既定値で動作を継続すべきため。他の *_env ヘルパと
    同じフェイルソフト方針）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def _apply_config_file_defaults() -> None:
    """config.env の値を「未設定の環境変数にのみ」適用する。

    os.environ.setdefault を使うことで、この関数を呼んだ時点で既にプロセス環境
    変数として設定済みのキーは一切上書きしない。これにより下の各設定値の解決
    （os.environ.get）より前に一度呼ぶだけで
    「プロセス環境変数 > config.env > ハードコード既定」の優先順位が自然に成立する。
    """
    for key, value in parse_config_file(resolve_config_path()).items():
        os.environ.setdefault(key, value)


_apply_config_file_defaults()

DB_PATH = Path(os.environ.get("SHELF_DB_PATH", PACKAGE_ROOT / ".catalog" / "shelf.db"))
CORPUS_DIR = Path(os.environ.get("SHELF_CORPUS_DIR", PACKAGE_ROOT / "corpus"))
EMBED_MODEL = os.environ.get(
    "SHELF_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
# fastembed のモデルキャッシュ置き場。fastembed の既定は tempfile.gettempdir() 配下
# （$TMPDIR 依存）だが、$TMPDIR は Claude Code のサンドボックス内外で別パスへ解決される
# ため、起動のたびにキャッシュを見失いモデル再ダウンロードを試み、ネットワーク遮断下では
# MCP サーバが起動不能になる。~/.cache 配下は書き込みが許可され消えないので、ここに固定する。
# expanduser().resolve(): SHELF_MODEL_CACHE_DIR に "cache" のような相対値や "~/..." が
# 入ると、起動時の cwd や HOME 展開の有無で別ディレクトリを指してしまい、固定した意味が
# 失われるため、ここで絶対パスへ正規化してから TextEmbedding へ渡す。
MODEL_CACHE_DIR = (
    Path(os.environ.get("SHELF_MODEL_CACHE_DIR", Path.home() / ".cache" / "fastembed"))
    .expanduser()
    .resolve()
)
DEFAULT_BACKEND = os.environ.get("SHELF_DEFAULT_BACKEND", "codex")
TOP_K = _int_env("SHELF_TOP_K", 10)
ANSWER_TIMEOUT = _int_env("SHELF_ANSWER_TIMEOUT", 300)
DEEP_DIVE = _bool_env("SHELF_DEEP_DIVE", False)
# ローカル LLM バックエンド（engines/ollama.py）の接続先。既定は RTX 4060 8GB 実機で
# 動かす想定の Ollama デーモン（同一ホスト）・qwen3:8b（8GB VRAM に収まる量子化モデル）。
OLLAMA_URL = os.environ.get("SHELF_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("SHELF_OLLAMA_MODEL", "qwen3:8b")

# 司書（Librarian）のルーティング推論専用バックエンド名。空文字列 = 未指定を意味し、
# service 側で `config.ROUTER_BACKEND or config.DEFAULT_BACKEND` として解決する
# 前提（設計書 §6-D）。専門家と司書で別バックエンドを使う運用（例: 司書は軽量・速い
# エンジン）へのドアを開けておくための分離であり、config.py 自体はデフォルト値の
# フォールバック方式を知らない（呼び出し側の責務）。
ROUTER_BACKEND = os.environ.get("SHELF_ROUTER_BACKEND", "")

# apply_fallback（routing.py）が実際に採用する notebook 数の上限。既定 1 は
# 「まず単一 notebook へ絞る」保守的な既定（設計書 §6-C 分岐4b）。routing.py の
# HARD_CAP_TOP_N=2 がこの値を上回っても機械的にクランプするため、ここでの既定は
# その hard cap と矛盾しない値であればよい。
ROUTE_TOP_N = _int_env("SHELF_ROUTE_TOP_N", 1)

# ルーティング解析失敗時（parse_ok=false または targets 空）のフォールバック方針。
# routing.FALLBACK_ALL("all") と一致する値を設定した時のみ全 notebook 横断へ切替わり、
# それ以外（既定は空文字列）は保守的に対象ゼロ即答へ倒す（設計書 §6-C 分岐3・
# レイテンシ保護優先のデフォルト）。
ROUTE_FALLBACK = os.environ.get("SHELF_ROUTE_FALLBACK", "")

# shelf digest の reduce フェーズ後に 1 資料あたり保持する学びノート数の既定上限。
# digests.py は config を import しない設計（§3 依存方向）のため呼び出し側の
# service.py が build_reduce_prompt(..., max_notes=config.DIGEST_MAX_NOTES) として
# 明示的に渡す。既定値は digests.REDUCE_DEFAULT_NOTES を唯一の情報源とする
# （env 変数名 SHELF_DIGEST_MAX_NOTES は既存呼び出し・運用設定との互換のため維持）。
DIGEST_MAX_NOTES = _int_env("SHELF_DIGEST_MAX_NOTES", REDUCE_DEFAULT_NOTES)

# shelf digest の map フェーズで 1 ウィンドウ（1 回の map LLM 呼び出し入力）あたり
# 抽出する学びノート数の既定上限。digests.build_map_prompt(..., max_notes=...) へ
# service.py が明示的に渡す。DIGEST_MAX_NOTES（reduce 後・文書全体の上限）とは
# 独立した控えめな値にする（1 ウィンドウから DIGEST_MAX_NOTES 件も学びが出るのは
# 過剰なため）。既定値は digests.MAP_DEFAULT_NOTES を唯一の情報源とする。
DIGEST_MAP_NOTES = _int_env("SHELF_DIGEST_MAP_NOTES", MAP_DEFAULT_NOTES)

# shelf digest の map フェーズで body チャンク列を分割する 1 ウィンドウあたりの
# 既定文字数上限。digests.group_into_windows(..., window_chars=...) へ渡す。
# 既定値は digests.WINDOW_DEFAULT_CHARS を唯一の情報源とする。
DIGEST_MAP_WINDOW_CHARS = _int_env("SHELF_DIGEST_MAP_WINDOW_CHARS", WINDOW_DEFAULT_CHARS)

# shelf digest（map/reduce 両フェーズ）専用の推論バックエンド名。既定は空文字列
# （未指定）で、この場合 service.py は notebook 自体の backend にフォールバックする
# （ROUTER_BACKEND と同じ「空=呼び出し側でフォールバック」流儀）。専門家の回答生成
# （ask/consult）とは別バックエンドで学び抽出だけ回す運用へのドアを開けておく。
DIGEST_BACKEND = os.environ.get("SHELF_DIGEST_BACKEND", "")

# shelf shelve（自動分類投入）の要約・分類推論、および新規作成する notebook の
# backend 列に使うバックエンド名。全体既定 DEFAULT_BACKEND（codex・クラウド）とは
# 独立に、既定をローカル ollama（qwen3:8b）へ倒す。分類は多数回・低単価推論のため
# クラウド課金を避け、かつ実効コンテキストが小さいモデル前提の設計（設計書 §13.1 決定6）。
SHELVE_BACKEND = os.environ.get("SHELF_SHELVE_BACKEND", "ollama")


def _parse_allowed_hosts(raw: str) -> list[str]:
    """カンマ区切りの許可ホスト一覧を list[str] へパースする。

    前後空白を除去し、空要素（連続カンマ・末尾カンマ由来）は捨てる。
    未設定時は空文字列が渡り、空リストになる（= 追加の許可ホストなし）。
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


# `shelf serve --http` の起動設定を env からも解決できるようにする3変数。
# Windows サービス定義（serve-shelf.ps1）が env→CLIフラグの翻訳を自前で行って
# いたため、ここで env を解決することでサービス定義側を薄くできる。CLI フラグは
# 常にこれらの env より優先する（優先順位の解決は cli.resolve_serve_settings の責務、
# ここでは「env→既定値」の解決のみを担う）。
HTTP_ENABLED = _bool_env("SHELF_HTTP_ENABLED", False)
HTTP_HOST = os.environ.get("SHELF_HTTP_HOST", "127.0.0.1")
HTTP_PORT = _int_env("SHELF_HTTP_PORT", 8765)
# 変数名は SHELF_HTTP_ALLOWED_HOSTS ではなく SHELF_ALLOWED_HOSTS（serve-shelf.ps1 の
# 既存 env 名と一致させる必要があるため、HTTP_ プレフィックスを付けない）。
ALLOWED_HOSTS = _parse_allowed_hosts(os.environ.get("SHELF_ALLOWED_HOSTS", ""))

# ask/consult のチャンク検索を、cosine ベクトル検索単体ではなく FTS5 キーワード
# 検索（BM25）との RRF（Reciprocal Rank Fusion）併用にするかどうか。既定 true:
# ベクトル検索は意味的に近いが語彙が一致しない文を拾える一方、固有名詞・型番・
# エラーコードのような表記ゆれの少ない語の完全一致取りこぼしに弱いため、
# キーワード検索を併用したほうが実運用の grounding 精度が高いと判断した。
# fts5/trigram tokenizer が使えない環境では store.fts_enabled=False により
# 自動的にベクトル単体へ劣化する（このフラグは「使うかどうかの意図」のみを表す）。
HYBRID_SEARCH = _bool_env("SHELF_HYBRID_SEARCH", True)

# ローカルファイル投入（add_source/add_directory、および内部で同じ走査規則を
# 共有する shelve）のファイルサイズ上限（MB）。守る対象は誤投入・暴走であって
# 正当な蔵書ではない（スキャン書籍PDFは数百MBになり得る）ため、既定は大きめの
# 300MBとし、運用でより厳しく絞りたい場合は env で調整できるようにする。
# URL 経由の投入（convert.py の 20MB 上限）とは独立した値（ローカルファイルと
# 外部URL取得ではリスクの性質が異なるため）。
# 0以下は「常に拒否」という意図しない全否定になり、かつエラーメッセージに
# 負数/0MBが埋め込まれる違和感を生むため、不正値（int変換失敗）と同様に既定へ
# フォールバックする（他の *_MB/*_NOTES 系と異なり、0以下が意味を持たない値のため
# 共有ヘルパ _int_env 自体は変更せずここだけで個別にクランプする）。
_max_file_mb_raw = _int_env("SHELF_MAX_FILE_MB", 300)
MAX_FILE_MB = _max_file_mb_raw if _max_file_mb_raw > 0 else 300
