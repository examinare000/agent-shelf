"""notebook 名検証・doc_id 生成の純粋関数群。

なぜ純粋関数として切り出すのか:
notebook 名はメタデータフィルタ（SQL・パス構築）に直接使われるため、境界で
一度だけ検証すればインジェクション/パストラバーサルの経路を塞げる
（docs/design-shelf-mcp.md §5「notebook 命名」）。doc_id は再投入時の重複検出・
更新キーとして store 層が参照する決定論的な識別子である必要がある。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_NOTEBOOK_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")
_ERROR_MESSAGE_MAX_LEN = 64
_SLUG_MAX_LEN = 40
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SHA8_LEN = 8
_NOTEBOOK_NAME_MAX_LEN = 64
_NOTEBOOK_NAME_NON_ALLOWED = re.compile(r"[^a-z0-9_-]+")
_DEFAULT_NOTEBOOK_NAME = "notebook"

# notebook 名は corpus/ 配下のディレクトリ名にそのまま使われるため、Windows の
# 予約デバイス名（大文字小文字不問・拡張子なしの完全一致で予約される）を許すと
# ディレクトリ作成不能・誤動作につながる。
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_RESERVED_NAME_SUFFIX = "-nb"


def _is_reserved_device_name(name: str) -> bool:
    return name.lower() in _RESERVED_DEVICE_NAMES


def validate_notebook_name(name: str) -> str:
    """notebook 名が `[a-z0-9_-]+`・長さ1〜64であることを検証し、そのまま返す。

    Windows 予約デバイス名（con/prn/aux/nul/com1-9/lpt1-9、大文字小文字不問）
    も拒否する。不正な場合は ValueError を送出する。エラーメッセージには
    入力値を含めてよいが、ログ肥大やメッセージ汚染を防ぐため64字で切り詰める。
    """
    if _NOTEBOOK_NAME_PATTERN.match(name) and not _is_reserved_device_name(name):
        return name
    truncated = name[:_ERROR_MESSAGE_MAX_LEN]
    raise ValueError(f"不正な notebook 名です: {truncated!r}")


def doc_id_for(origin: str, stem: str) -> str:
    """元ファイルパス(origin)とファイル名幹(stem)から決定論的な doc_id を生成する。

    doc_id = slug化したstem（最大40字） + "-" + sha256(origin)の先頭8桁。
    同じ (origin, stem) の入力に対して常に同じ doc_id を返すことで、
    再投入時の重複検出・更新（store 層の一意制約）を成立させる。
    """
    slug = _slugify(stem)
    sha8 = hashlib.sha256(origin.encode()).hexdigest()[:_SHA8_LEN]
    return f"{slug}-{sha8}"


def _slugify(stem: str) -> str:
    """stem を小文字化し、英数字以外の連続を単一の "-" に圧縮する。

    先頭・末尾の "-" を除去し、空になれば "doc" にフォールバックする。
    40字への切り詰め後に境界がハイフンで割れる場合があるため、
    切り詰め後にも再度末尾の "-" を除去する。
    """
    lowered = stem.lower()
    compressed = _SLUG_NON_ALNUM.sub("-", lowered).strip("-")
    if not compressed:
        return "doc"
    return compressed[:_SLUG_MAX_LEN].rstrip("-")


def normalize_notebook_name(raw: str) -> str:
    """LLM が提案した notebook 名を決定的に正規化する（docs/design-shelf-reference-service.md §13.5）。

    小文字化 → `[a-z0-9_-]` 以外の連続を単一の "-" へ圧縮 → 前後の "-" を除去
    → 64字へ切り詰め（境界でハイフンが割れたら再度除去）→ 空なら既定名
    "notebook" にフォールバックする。`_slugify` と同思想だが、notebook 名は
    アンダースコアも許容する（doc_id スラグとは別規則）ため専用関数として
    public に新設する（`_slugify` は doc_id 専用のまま private を維持）。

    構成上、返り値は必ず `validate_notebook_name` を通る
    （§13.10 V2 の検証ステップ）。
    """
    lowered = raw.lower()
    compressed = _NOTEBOOK_NAME_NON_ALLOWED.sub("-", lowered).strip("-")
    if not compressed:
        return _DEFAULT_NOTEBOOK_NAME
    truncated = compressed[:_NOTEBOOK_NAME_MAX_LEN].rstrip("-")
    result = truncated or _DEFAULT_NOTEBOOK_NAME
    return _remap_reserved_device_name(result)


def _remap_reserved_device_name(name: str) -> str:
    """Windows 予約デバイス名なら例外を投げず、サフィックスを付けて安全な名前へ
    リマップする（validate_notebook_name を必ず通す構成上の契約を維持する）。

    LLM 提案名の自動正規化（shelve の自動命名経路）は例外を投げず処理を
    続行する必要があるため、validate 側の拒否とは対照的にリマップで対応する。
    """
    if not _is_reserved_device_name(name):
        return name
    max_base_len = _NOTEBOOK_NAME_MAX_LEN - len(_RESERVED_NAME_SUFFIX)
    truncated_base = name[:max_base_len].rstrip("-")
    return f"{truncated_base}{_RESERVED_NAME_SUFFIX}"


def assign_unique_name(base: str, taken: Iterable[str]) -> str:
    """`base` が `taken` と衝突する場合、決定的な連番を付与して空き名を返す（§13.5）。

    衝突しなければ `base` をそのまま返す。衝突時は "base-2", "base-3", ...
    と連番を試し、最初に空いている名前を返す。64字上限を守るため、連番
    サフィックスを付けても収まるよう base 側を切り詰める
    （末尾のハイフンは切り詰め境界で再度除去する）。
    """
    taken_set = set(taken)
    if base not in taken_set:
        return base
    counter = 2
    while True:
        candidate = _with_numeric_suffix(base, counter)
        if candidate not in taken_set:
            return candidate
        counter += 1


def _with_numeric_suffix(base: str, counter: int) -> str:
    suffix = f"-{counter}"
    max_base_len = _NOTEBOOK_NAME_MAX_LEN - len(suffix)
    truncated_base = base[:max_base_len].rstrip("-")
    return f"{truncated_base}{suffix}"
