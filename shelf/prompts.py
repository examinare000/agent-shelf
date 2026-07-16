"""ask フロー用のプロンプト構成・エンジン出力パース（純粋関数のみ）。

外部 SDK・DB・subprocess を一切知らない。RetrievedChunk（ports.py）と
str/dict のみを扱うことで、エンジン実装（engines/*.py）を差し替えても
このモジュールは変更不要という境界を保つ（設計書 §3 依存方向）。
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from shelf.ports import RetrievedChunk

# codex --output-schema に渡す厳格 JSON スキーマ。
# {answer, citations:[{s}], confident} 以外の出力を許さないための強制力として使う。
# insights は required に含めない（省略時 []）。他バックエンド（codex/gemini/agy）や
# 学びノート未整備 notebook でも従来通り検証が通り、ask 互換が保たれる（設計書 §5-C）。
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"s": {"type": "integer"}},
                "required": ["s"],
                "additionalProperties": False,
            },
        },
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"l": {"type": "integer"}},
                "required": ["l"],
                "additionalProperties": False,
            },
        },
        "confident": {"type": "boolean"},
    },
    "required": ["answer", "citations", "confident"],
    "additionalProperties": False,
}

# persona=None かつ digest チャンクなしのプロンプトは既存実装と byte 単位で同一でなければ
# ならない（ask 互換・設計書 §10 R5）ため、このヒントは変更しない。
_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"answer": "...", "citations": [{"s": 1}], "confident": true}'
)

# digest チャンク（insights）が retrieved に含まれる場合のみ使うヒント。
# 常時この文言を使うと persona=None・digest なしの互換 golden が壊れるため、
# build_ask_prompt が insight_chunks の有無で出し分ける。
_JSON_FORMAT_HINT_WITH_INSIGHTS = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"answer": "...", "citations": [{"s": 1}], "insights": [{"l": 1}], "confident": true}'
)

# 資料要約の入力に使う先頭文字数の上限。
# タイトル・目次・導入部分だけで主題は判定できるため全文を渡す必要はなく、
# codex への入力コスト・レイテンシを抑える目的で切り詰める。
SUMMARY_INPUT_MAX_CHARS = 4000

# codex --output-schema に渡す厳格 JSON スキーマ（ANSWER_SCHEMA と同様の制約）。
SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_SUMMARY_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"summary": "..."}'
)


@dataclass(frozen=True)
class ParsedAnswer:
    answer: str
    citation_ids: list[int]
    confident: bool
    parse_ok: bool
    insight_ids: list[int] = field(default_factory=list)


def build_ask_prompt(
    question: str,
    chunks: Sequence[RetrievedChunk],
    deep_dive: bool = False,
    persona: str | None = None,
) -> str:
    """質問文 + 番号付きチャンク抜粋 + grounding 指示からエンジン投入プロンプトを組み立てる。

    kind='digest' のチャンクは学びノート（insights・[L番号]）、それ以外
    （body/summary）は抜粋（citations・[S番号]）として分離する（設計書 §5-C/§7-C）。
    persona が None かつ digest チャンクが無い場合、出力は persona/insights 拡張前の
    実装と byte 単位で完全一致する（`ask` 互換の保証・設計書 §10 R5）。
    """
    citation_chunks = [chunk for chunk in chunks if chunk.kind != "digest"]
    insight_chunks = [chunk for chunk in chunks if chunk.kind == "digest"]

    instructions = []
    if persona is not None:
        instructions.append(f"あなたは{persona}である。")
    instructions.append(
        "以下の抜粋のみを根拠に日本語で回答してください。各主張に [S番号] を付してください。"
    )
    if insight_chunks:
        instructions.append(
            "学びノートを参考にした場合は各主張に [L番号] を付してください。"
        )
    instructions.append(
        "抜粋に根拠が無い場合は推測せず「資料からは分からない」と答え、"
        "confident は false としてください。"
    )
    if deep_dive:
        instructions.append("必要なら作業ディレクトリ内の引用元ファイルを開いて確認してよい。")
    instructions.append(_JSON_FORMAT_HINT_WITH_INSIGHTS if insight_chunks else _JSON_FORMAT_HINT)

    excerpts = "\n\n".join(
        _format_chunk(index, chunk, label="S")
        for index, chunk in enumerate(citation_chunks, start=1)
    )

    sections = [excerpts] if excerpts else []
    if insight_chunks:
        notes = "\n\n".join(
            _format_chunk(index, chunk, label="L")
            for index, chunk in enumerate(insight_chunks, start=1)
        )
        sections.append(notes)

    body = "\n\n".join(sections)
    return "\n".join(instructions) + f"\n\n質問: {question}\n\n{body}"


def _format_chunk(index: int, chunk: RetrievedChunk, *, label: str = "S") -> str:
    meta = [f"source: {chunk.source_path}"]
    if chunk.section is not None:
        meta.append(f"節: {chunk.section}")
    if chunk.page is not None:
        meta.append(f"p.{chunk.page}")
    header = f"[{label}{index}] ({', '.join(meta)})"
    return f"{header}\n{chunk.text}"


def build_summary_prompt(markdown: str, *, title: str | None = None) -> str:
    """資料が何について書かれたものかを短く要約するプロンプトを組み立てる。

    冒頭（タイトル・目次・導入）を読めば主題は判定できるため、
    先頭 SUMMARY_INPUT_MAX_CHARS 字までに切り詰めて渡す。
    """
    instructions = [
        "以下の資料が何について書かれたものかを、200字以内の日本語で要約してください。",
        "本文からは分からない内容を推測で補わないでください。",
        _SUMMARY_JSON_FORMAT_HINT,
    ]

    body = markdown[:SUMMARY_INPUT_MAX_CHARS]
    title_line = f"\n\nタイトル: {title}" if title is not None else ""

    return "\n".join(instructions) + title_line + f"\n\n資料:\n{body}"


def parse_answer(text: str) -> ParsedAnswer:
    """エンジンの生出力テキストを厳格 JSON として解釈する。

    素の JSON / ```json フェンス付き / 前後に余計な文章が付いた出力のいずれも、
    最初の `{` から最後の `}` までを候補として json.loads を試みる。
    それでもパースできない場合は劣化返却（parse_ok=False・原文をそのまま answer に）にし、
    呼び出し側（service.py）がエラーで潰さず warning 付きで返せるようにする。
    """
    payload = _extract_json_payload(text)
    data = None
    if payload is not None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        return ParsedAnswer(
            answer=text, citation_ids=[], confident=False, parse_ok=False, insight_ids=[]
        )

    return ParsedAnswer(
        answer=data["answer"],
        citation_ids=_normalize_marker_ids(data.get("citations"), "s"),
        insight_ids=_normalize_marker_ids(data.get("insights"), "l"),
        confident=bool(data.get("confident", False)),
        parse_ok=True,
    )


def _extract_json_payload(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_summary(text: str) -> str | None:
    """エンジンの生出力テキストから summary 文字列を取り出す。

    parse_answer と同じ _extract_json_payload を再利用し、
    JSON が取れない・dict でない・summary が str でない・
    strip 後に空文字列になる場合はいずれも None を返し、呼び出し側で失敗扱いにする。
    """
    payload = _extract_json_payload(text)
    if payload is None:
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None

    summary = data["summary"].strip()
    return summary if summary else None


def _normalize_marker_ids(raw: object, key: str) -> list[int]:
    """citations の "s" / insights の "l" いずれも同じ規則で正規化する（重複除去・
    非正整数除外・出現順維持）。citation_ids と insight_ids で共有するための一般化。"""
    if not isinstance(raw, list):
        return []
    seen: set[int] = set()
    result: list[int] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        s = item.get(key)
        # bool は int のサブクラスだが JSON 上は真偽値であり番号ではないため除外。
        if not isinstance(s, int) or isinstance(s, bool) or s <= 0:
            continue
        if s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result
