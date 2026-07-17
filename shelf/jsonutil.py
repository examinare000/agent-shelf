"""LLM 生出力から JSON を抜き出す共通ヘルパー（stdlib のみに依存する依存ゼロの leaf）。

prompts.py/shelving.py/digests.py がそれぞれ複製していた JSON 抽出ロジック（コード
レビュー指摘 P12: 3 定義 4 利用箇所の重複）を集約する。json/re 以外の何も import し
ないため、外部 SDK ゼロ・stdlib のみという各ドメイン層モジュールの import 境界
契約（設計書 §9-C・shelving.py の §13.3 「json + ports.py + names.py だけ」制約を
含む）を壊さずにどこからでも安全に import できる（test_boundaries.py の
_RESTRICTED_TO_OWNER は sqlite3/subprocess/fastembed 等の外部 SDK のみを対象とし、
json という stdlib への依存は制約しない）。
"""
from __future__ import annotations

import json


def extract_json_payload(text: str) -> str | None:
    """エンジンの生出力テキストから JSON 候補の部分文字列を取り出す。

    素の JSON / ```json フェンス付き / 前後に余計な文章が付いた出力のいずれも、
    最初の `{` から最後の `}` までを候補として返す。呼び出し側が json.loads を
    試みてパース可否を判定する（この関数自体は妥当性を検証しない）。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_json_object(text: str) -> dict | None:
    """エンジンの生出力テキストから厳格 JSON オブジェクトを抜き出す。

    extract_json_payload + json.loads の組。パース失敗・トップレベルが dict で
    ない場合はいずれも None へ劣化させ、呼び出し側が一律に空リスト等へ
    フォールバックできるようにする（digests.py の旧 _parse_json_object と同一挙動）。
    """
    payload = extract_json_payload(text)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
