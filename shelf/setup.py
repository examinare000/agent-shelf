"""`shelf setup`: 対話式（または --yes / --answers-file 非対話）で config.env を
生成する初回セットアップ。

なぜこのモジュールを分けるか: cli.py は既存サブコマンドと同じ「配線だけ」の薄さを
保ちたいため、質問順序・既定値解決・ファイル整形といったロジックはここに集約し、
cli.py 側は「回答一式を集める→KEY=VALUE dict へ変換→書き出す」の3行に留める。

対話プロンプト(collect_answers_interactively)は input_func/print_func を注入
できるようにし、実 stdin を使わずシナリオ台本でテストする（recall/shelf 既存の
「テスト用フック引数」作法。cli.py の _confirm と同じ考え方）。

ローカル ollama の HTTP 疎通確認は shelf.engines.ollama.is_reachable を経由する。
urllib.request の import は同ファイルに限定されており（test_boundaries.py）、
このモジュールが直接 urllib.request を import することは許されないため。
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from shelf import config
from shelf.engines.ollama import is_reachable

# 学びの粒度プリセット（設計書タスク仕様 §1-3）。english キーはJSON/内部表現用、
# 画面表示は日本語ラベルで行う（GRANULARITY_LABELS）。
#
# digest_input_max_chars は旧単発生成パイプライン（digest 1資料を先頭4000字だけ
# 1回のLLM呼び出しで要約）専用の値だった。map-reduce パイプライン化（service.py
# DIGEST_PIPELINE_VERSION=2）に伴い、対応する設定は config.DIGEST_MAP_WINDOW_CHARS
# （env SHELF_DIGEST_MAP_WINDOW_CHARS）に置き換わったが、この2つは意味が異なる:
# 旧は「1資料の先頭から何字を読むか」の入力上限で、値が大きいほど『fine(細かい)』
# （2000→8000で fine ほど大きい）という直感的な対応だった。新の window_chars は
# 「mapフェーズ1ウィンドウの分割単位」で、値が小さいほどウィンドウ数が増えて
# 抽出される学びノート総数が増える＝fine 方向という、旧設定とは逆方向の関係になる。
# 単純に値を差し替えると「fine なのに window_chars が大きい(粗い方向)」という
# 矛盾したプリセットになってしまうため、ここでは書き込みのみを削除し、
# window_chars をプリセットへ組み込むのは見送った（新パイプラインでの適切な
# 既定値・方向性は要別途設計判断・完了報告に明記）。粒度は既存の digest_max_notes
# （文書全体で保持する学びノート数）と top_k で引き続き制御する。
GRANULARITY_PRESETS: dict[str, dict[str, int]] = {
    "coarse": {"digest_max_notes": 10, "top_k": 5},
    "standard": {"digest_max_notes": 20, "top_k": 10},
    "fine": {"digest_max_notes": 40, "top_k": 20},
}
GRANULARITY_LABELS: dict[str, str] = {
    "coarse": "粗い",
    "standard": "標準",
    "fine": "細かい",
}
_DEFAULT_GRANULARITY = "standard"

# LLM プロバイダ選択肢。engines/__init__.py の _BACKENDS と一致。
PROVIDER_CHOICES: tuple[str, ...] = ("codex", "gemini", "agy", "ollama")
_PROVIDER_CLI_COMMAND: dict[str, str] = {
    "codex": "codex",
    "gemini": "gemini",
    "agy": "agy",  # Antigravity CLI
    "ollama": "ollama",
}


def is_command_available(name: str) -> bool:
    """PATH 上に実行可能な `name` コマンドがあるかを確認する（`command -v` 相当）。"""
    return shutil.which(name) is not None


def detect_ollama(url: str) -> bool:
    """ローカル ollama が利用可能かを判定する: SHELF_OLLAMA_URL への HTTP 疎通、
    または `ollama` コマンドの存在のいずれかで真とする（タスク仕様どおり OR 条件。
    デーモン起動前でもコマンドさえ入っていれば「利用可否」の確認に進める）。
    """
    return is_reachable(url) or is_command_available("ollama")


def default_answers() -> dict:
    """--yes（非対話）が使う既定回答一式。config.py のハードコード既定値と一致させ、
    「--yes は今日の既定値をそのまま config.env に明示化するだけ」という設計にする
    （config.env が存在しない状態と機能的に等価）。
    """
    return {
        "use_ollama": True,
        "ollama_url": config.OLLAMA_URL,
        "ollama_model": config.OLLAMA_MODEL,
        "provider": config.DEFAULT_BACKEND,
        "router_backend": config.ROUTER_BACKEND,
        "granularity": _DEFAULT_GRANULARITY,
        # None = 選択したプリセットの値をそのまま使う（resolve_granularity 参照）。
        "digest_max_notes": None,
        "top_k": None,
        "corpus_dir": str(config.CORPUS_DIR),
        "db_path": str(config.DB_PATH),
    }


def resolve_granularity(answers: dict) -> dict:
    """granularity プリセット値に、明示指定された override（digest_max_notes 等）
    があればそれを適用して解決する。未知のプリセット名は "standard" にフォールバック
    する（config.py の *_env ヘルパと同じフェイルソフト方針）。
    """
    preset = GRANULARITY_PRESETS.get(
        answers.get("granularity", _DEFAULT_GRANULARITY), GRANULARITY_PRESETS[_DEFAULT_GRANULARITY]
    )
    resolved = dict(preset)
    for key in ("digest_max_notes", "top_k"):
        override = answers.get(key)
        if override is not None:
            resolved[key] = override
    return resolved


def resolve_shelve_backend(answers: dict) -> str:
    """shelve() 専用バックエンド名を解決する: ローカル ollama を使うと回答していれば
    "ollama"、そうでなければ選択した provider に倒す（config.SHELVE_BACKEND の
    「全体既定とは独立にローカルへ倒す」設計を踏襲しつつ、ollama を使わない選択を
    した利用者には provider 側で統一する）。
    """
    return "ollama" if answers["use_ollama"] else answers["provider"]


def answers_to_config_values(answers: dict) -> dict[str, str]:
    """完全に解決済みの answers dict を config.env の KEY -> str(VALUE) へ変換する。"""
    granularity = resolve_granularity(answers)
    return {
        "SHELF_OLLAMA_URL": answers["ollama_url"],
        "SHELF_OLLAMA_MODEL": answers["ollama_model"],
        "SHELF_SHELVE_BACKEND": resolve_shelve_backend(answers),
        "SHELF_DEFAULT_BACKEND": answers["provider"],
        "SHELF_ROUTER_BACKEND": answers["router_backend"],
        "SHELF_DIGEST_MAX_NOTES": str(granularity["digest_max_notes"]),
        "SHELF_TOP_K": str(granularity["top_k"]),
        "SHELF_CORPUS_DIR": answers["corpus_dir"],
        "SHELF_DB_PATH": answers["db_path"],
    }


def build_config_env_text(values: dict[str, str]) -> str:
    """KEY -> str(VALUE) dict を config.env のテキスト形式に整形する。

    出力は shelf.config.parse_config_file でそのまま読み戻せる形式
    （`KEY=VALUE` 1行ずつ）にする。
    """
    header = "# shelf setup が生成した設定ファイル（手動編集可）\n"
    lines = [f"{key}={value}" for key, value in values.items()]
    return header + "\n".join(lines) + "\n"


def load_answers_file(path: Path) -> dict:
    """--answers-file <json> を読み込み、既定値で不足キーを補って返す（テスト用の
    非対話回答注入経路。JSON に無いキーは default_answers() の値を使う）。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {**default_answers(), **raw}


def write_config_env(path: Path, text: str) -> None:
    """config.env をディスクへ書き出す（親ディレクトリが無ければ作成する）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prompt(
    input_func: Callable[[str], str], print_func: Callable[..., None], prompt: str, default: str
) -> str:
    """1問1答のプロンプトを表示し、空入力（Enter のみ）なら default を返す。"""
    raw = input_func(f"{prompt} [{default}]: ").strip()
    return raw if raw else default


def collect_answers_interactively(
    *,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> dict:
    """4ステップの対話プロンプトで回答一式を集める（タスク仕様 §1-4）。

    各ステップは Enter のみで既定値を採用できる。ollama/各 provider CLI の検出は
    ここで一度だけ行い、結果を選択肢の説明に併記する（利用者が実際の環境を見て
    判断できるように）。
    """
    defaults = default_answers()

    print_func("== 1. ローカル LLM (ollama) ==")
    ollama_detected = detect_ollama(defaults["ollama_url"])
    print_func(f"ollama 検出結果: {'利用可能' if ollama_detected else '検出できませんでした'}")
    use_ollama_raw = _prompt(
        input_func, print_func, "ollama を使いますか? (y/n)", "y" if defaults["use_ollama"] else "n"
    )
    use_ollama = use_ollama_raw.strip().lower() not in ("n", "no")

    ollama_url = defaults["ollama_url"]
    ollama_model = defaults["ollama_model"]
    if use_ollama:
        ollama_url = _prompt(input_func, print_func, "ollama URL", defaults["ollama_url"])
        ollama_model = _prompt(input_func, print_func, "ollama モデル", defaults["ollama_model"])

    print_func("== 2. LLM プロバイダ ==")
    for name in PROVIDER_CHOICES:
        available = is_command_available(_PROVIDER_CLI_COMMAND[name])
        print_func(f"  {name}: {'検出済み' if available else '未検出'}")
    provider = _prompt(
        input_func, print_func, f"プロバイダを選択 ({'/'.join(PROVIDER_CHOICES)})", defaults["provider"]
    )
    router_backend = _prompt(
        input_func,
        print_func,
        "司書ルーティング用バックエンド(空=同じ)",
        defaults["router_backend"],
    )

    print_func("== 3. 学びの粒度 ==")
    print_func("  coarse(粗い) / standard(標準) / fine(細かい) / カスタムは数値で個別指定")
    granularity = _prompt(
        input_func, print_func, "粒度プリセット", defaults["granularity"]
    )
    digest_max_notes = defaults["digest_max_notes"]
    top_k = defaults["top_k"]

    print_func("== 4. 配置 ==")
    if Path(defaults["corpus_dir"]).exists():
        print_func(f"既存の corpus を検出しました: {defaults['corpus_dir']}(再利用を推奨)")
    if Path(defaults["db_path"]).exists():
        print_func(f"既存の DB を検出しました: {defaults['db_path']}(再利用を推奨)")
    corpus_dir = _prompt(input_func, print_func, "corpus ディレクトリ", defaults["corpus_dir"])
    db_path = _prompt(input_func, print_func, "DB パス", defaults["db_path"])

    return {
        "use_ollama": use_ollama,
        "ollama_url": ollama_url,
        "ollama_model": ollama_model,
        "provider": provider,
        "router_backend": router_backend,
        "granularity": granularity,
        "digest_max_notes": digest_max_notes,
        "top_k": top_k,
        "corpus_dir": corpus_dir,
        "db_path": db_path,
    }
