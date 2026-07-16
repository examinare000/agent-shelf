"""学びノート（study note）生成用のプロンプト構成・エンジン出力パース（純粋関数のみ）。

外部 SDK・DB・subprocess を一切知らない。ports.py の DTO と str/dict のみを扱うことで、
エンジン実装（engines/*.py）・store 実装を差し替えてもこのモジュールは変更不要という
境界を保つ（設計書 §3 依存方向・§9-C import ガード）。
"""
from __future__ import annotations

import json

from shelf.ports import StudyNote

# 学びノート生成の入力に使う先頭文字数の上限。
# prompts.SUMMARY_INPUT_MAX_CHARS と同じ思想（冒頭を読めば要点は抽出できるため
# 全文を渡す必要はなく、エンジンへの入力コスト・レイテンシを抑える）。
# digests.py は json（stdlib）+ ports.py の DTO だけに依存する制約上、
# prompts.py の定数を import せずローカルに同値を定義する。
DIGEST_INPUT_MAX_CHARS = 4000

# 1 資料あたりに保持する学びノート数の既定上限（設計書 §7-B・§12-2「まず資料単位・小 N で
# 開始し反復」）。config.py の SHELF_DIGEST_MAX_NOTES 相当だが、digests.py は json（stdlib）
# + ports.py の DTO だけに依存する制約上、設定は呼び出し側（service.py）が
# parse_digest(..., max_notes=...) として明示的に渡す前提のローカル既定値に留める。
DIGEST_DEFAULT_MAX_NOTES = 5

_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"notes": [{"text": "...", "span": "..."}]}'
)

# codex --output-schema に渡す厳格 JSON スキーマ。
# {notes: [{text, span}]} 以外の出力を許さないための強制力として使う（設計書 §7-B）。
# span は由来（節・ページ範囲等）の任意情報のため required に含めない
# （StudyNote.span のデフォルト None と対称）。
DIGEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "span": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}


def build_digest_prompt(
    markdown: str,
    *,
    persona: str | None = None,
    title: str | None = None,
    max_chars: int = DIGEST_INPUT_MAX_CHARS,
) -> str:
    """資料 markdown から学びノート生成プロンプトを組み立てる。

    persona が与えられれば「あなたは <persona> である」を冒頭に注入し、
    その専門家の視点で学びを抽出させる（設計書 §7-B）。

    max_chars は additive パラメータ（既定値はモジュールのローカル定数
    DIGEST_INPUT_MAX_CHARS のまま = 既存呼び出し元は無変更で従来どおり動く）。
    config.DIGEST_INPUT_MAX_CHARS(env SHELF_DIGEST_INPUT_MAX_CHARS)を
    service.py 経由で渡せるようにする配線のために追加した（digests.py 自体は
    config を import しない設計を維持する）。
    """
    instructions = []
    if persona is not None:
        instructions.append(f"あなたは{persona}である。")
    instructions.append(
        "以下の資料の要点と、そこから得られる学び（洞察）を、日本語で複数件挙げてください。"
    )
    instructions.append("本文にない内容を推測で補わないでください。")
    instructions.append(_JSON_FORMAT_HINT)

    body = markdown[:max_chars]
    title_line = f"\n\nタイトル: {title}" if title is not None else ""

    return "\n".join(instructions) + title_line + f"\n\n資料:\n{body}"


def parse_digest(text: str, *, max_notes: int = DIGEST_DEFAULT_MAX_NOTES) -> list[StudyNote]:
    """エンジンの生出力テキストを厳格 JSON として解釈し、StudyNote のリストへ変換する。

    素の JSON / ```json フェンス付き / 前後に余計な文章が付いた出力のいずれも、
    最初の `{` から最後の `}` までを候補として json.loads を試みる
    （prompts.parse_answer と同じ _extract_json_payload 方式。digests.py は
    prompts.py を import しないためロジックをここに複製する）。
    パース失敗・notes 欠落/非リストはエラーで潰さず空リストへ劣化返却する
    （呼び出し側 service.py が warning 付きで扱えるようにするため）。
    生成件数がモデルの気まぐれで上限を超えても呼び出し側を壊さないよう、
    先頭 max_notes 件にクランプする（設計書 §7-B・§10 R4「ノート数上限のクランプ」）。
    """
    payload = _extract_json_payload(text)
    data = None
    if payload is not None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict):
        return []

    raw_notes = data.get("notes")
    if not isinstance(raw_notes, list):
        return []

    notes = [note for note in (_parse_note(item) for item in raw_notes) if note is not None]
    return notes[:max_notes]


def _parse_note(item: object) -> StudyNote | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    span = item.get("span")
    if not isinstance(span, str):
        span = None
    return StudyNote(text=text, span=span)


def _extract_json_payload(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]
