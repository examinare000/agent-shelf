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
# 明示的に渡す。map-reduce パイプライン化に伴い、単発生成時代の既定 5
# （digests.py 旧 DIGEST_DEFAULT_MAX_NOTES）から文書全体を俯瞰できる 20 へ拡大した
# （env 変数名 SHELF_DIGEST_MAX_NOTES は既存呼び出し・運用設定との互換のため維持）。
DIGEST_MAX_NOTES = _int_env("SHELF_DIGEST_MAX_NOTES", 20)

# shelf digest が LLM へ渡す資料本文の先頭何文字までを入力とするかの上限。
# digests.py はローカル定数 DIGEST_INPUT_MAX_CHARS=4000 を独立に持つ（config非依存の
# 制約上）が、service.py 側で config 値を渡す運用に備え同値をここにも定義する。
DIGEST_INPUT_MAX_CHARS = _int_env("SHELF_DIGEST_INPUT_MAX_CHARS", 4000)

# shelf digest の map フェーズで 1 ウィンドウ（1 回の map LLM 呼び出し入力）あたり
# 抽出する学びノート数の既定上限。digests.build_map_prompt(..., max_notes=...) へ
# service.py が明示的に渡す。DIGEST_MAX_NOTES（reduce 後・文書全体の上限）とは
# 独立した控えめな値にする（1 ウィンドウから DIGEST_MAX_NOTES 件も学びが出るのは
# 過剰なため）。digests.py 側の呼び出し規定値と同値。
DIGEST_MAP_NOTES = _int_env("SHELF_DIGEST_MAP_NOTES", 5)

# shelf digest の map フェーズで body チャンク列を分割する 1 ウィンドウあたりの
# 既定文字数上限。digests.group_into_windows(..., window_chars=...) へ渡す。
# digests.WINDOW_DEFAULT_CHARS と同値にして矛盾を避ける。
DIGEST_MAP_WINDOW_CHARS = _int_env("SHELF_DIGEST_MAP_WINDOW_CHARS", 8000)

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

# ask/consult のチャンク検索を、cosine ベクトル検索単体ではなく FTS5 キーワード
# 検索（BM25）との RRF（Reciprocal Rank Fusion）併用にするかどうか。既定 true:
# ベクトル検索は意味的に近いが語彙が一致しない文を拾える一方、固有名詞・型番・
# エラーコードのような表記ゆれの少ない語の完全一致取りこぼしに弱いため、
# キーワード検索を併用したほうが実運用の grounding 精度が高いと判断した。
# fts5/trigram tokenizer が使えない環境では store.fts_enabled=False により
# 自動的にベクトル単体へ劣化する（このフラグは「使うかどうかの意図」のみを表す）。
HYBRID_SEARCH = _bool_env("SHELF_HYBRID_SEARCH", True)
