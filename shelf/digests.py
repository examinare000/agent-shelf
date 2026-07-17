"""学びノート（study note）生成用のプロンプト構成・エンジン出力パース（純粋関数のみ）。

外部 SDK・DB・subprocess を一切知らない。ports.py の DTO と str/dict のみを扱うことで、
エンジン実装（engines/*.py）・store 実装を差し替えてもこのモジュールは変更不要という
境界を保つ（設計書 §3 依存方向・§9-C import ガード）。
"""
from __future__ import annotations

import json
import unicodedata

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


def _parse_json_object(text: str) -> dict | None:
    """エンジン生出力から厳格 JSON オブジェクトを抜き出す共通ヘルパー。

    _extract_json_payload + json.loads の組（parse_map/parse_reduce の
    2 関数が同じ手順を必要とするため集約した）。パース失敗・トップレベルが dict でない
    場合はいずれも None へ劣化させ、呼び出し側が一律に空リスト等へフォールバックできる
    ようにする。
    """
    payload = _extract_json_payload(text)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# map-reduce 学び抽出パイプラインでの 1 ウィンドウ（1 回の map LLM 呼び出し入力）の
# 既定文字数上限（設計書「先頭4000字を1回のLLM呼び出しで要約」からの置き換え・
# §7-B 拡張）。文書全体を複数ウィンドウに分割して map する前提のため、
# 旧単発生成方式の入力上限（4000字）よりやや広めに取る。
WINDOW_DEFAULT_CHARS = 8000


def group_into_windows(
    chunks: list[dict], *, window_chars: int = WINDOW_DEFAULT_CHARS
) -> list[list[dict]]:
    """store.list_chunks 由来のチャンク dict 列（seq 昇順）を map 入力ウィンドウへ分割する。

    節（section）が変わる位置を優先境界として扱う: ウィンドウ内文字数がまだ
    window_chars 未満でも、節が変わったら新しいウィンドウを開始する（節をまたいだ
    学び抽出はコンテキストが混ざり品質が落ちるため）。同一節内では window_chars を
    超えない範囲で貪欲にパックし、超える直前で新しいウィンドウへ切る。
    単一チャンクが window_chars を超える場合はチャンク自体を分割せず単独ウィンドウにする
    （チャンクは chunker.py が既に上限管理済みという既存契約を尊重する）。
    """
    windows: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    current_section: str | None = None

    for chunk in chunks:
        text_len = len(chunk["text"])
        section = chunk.get("section")
        section_changed = current and section != current_section
        exceeds_limit = current and current_chars + text_len > window_chars
        if section_changed or exceeds_limit:
            windows.append(current)
            current = []
            current_chars = 0

        current.append(chunk)
        current_chars += text_len
        current_section = section

    if current:
        windows.append(current)

    return windows


_MAP_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"notes": [{"text": "...", "chunks": [1, 3]}]}'
)

# codex --output-schema に渡す厳格 JSON スキーマ（map フェーズ）。
# 各 note に根拠チャンク番号 chunks を必須で持たせる
# （設計書: 学びノートを具体チャンク id に接地するため・parse_map が id へ解決する）。
MAP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "chunks": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "chunks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}


def build_map_prompt(
    window: list[dict],
    *,
    persona: str | None = None,
    title: str | None = None,
    max_notes: int = 5,
) -> str:
    """1 ウィンドウ（group_into_windows の要素）から map フェーズのプロンプトを組み立てる。

    チャンクを [C1]..[Cn] で番号提示し（prompts.build_ask_prompt の [S番号] 方式を踏襲）、
    LLM に番号で根拠を参照させる。参照番号は parse_map が window 内 dict の
    id/section/page への機械解決に使う。
    """
    instructions = []
    if persona is not None:
        instructions.append(f"あなたは{persona}である。")
    instructions.append(
        f"以下の抜粋範囲から得られる重要な学び（洞察）を、日本語で最大{max_notes}件挙げてください。"
    )
    instructions.append("各学びに根拠チャンク番号を chunks 配列で必ず付けてください。")
    instructions.append("本文にない内容を推測で補わないでください。")
    instructions.append(_MAP_JSON_FORMAT_HINT)

    title_line = f"\n\nタイトル: {title}" if title is not None else ""
    excerpts = "\n\n".join(
        _format_window_chunk(index, chunk) for index, chunk in enumerate(window, start=1)
    )

    return "\n".join(instructions) + title_line + f"\n\n{excerpts}"


def _format_window_chunk(index: int, chunk: dict) -> str:
    meta = []
    section = chunk.get("section")
    if section is not None:
        meta.append(f"節: {section}")
    page = chunk.get("page")
    if page is not None:
        meta.append(f"p.{page}")
    header = f"[C{index}]" + (f" ({', '.join(meta)})" if meta else "")
    return f"{header}\n{chunk['text']}"


def parse_map(text: str, window: list[dict], *, max_notes: int = 5) -> list[StudyNote]:
    """map フェーズのエンジン生出力を、window 内チャンクへ接地した StudyNote 列へ変換する。

    chunks 配列（1 起点の [C番号]）を window[index-1] の id/section/page へ機械解決する。
    範囲外・非正整数・非 int の番号は「その参照だけ」捨て、text が有効な限りノート自体は
    残す（劣化方針は prompts._normalize_marker_ids と同じ番号検証規則を踏襲する）。
    代表 section/page は先頭の有効参照チャンクの値（参照が全滅した場合は None）。
    """
    data = _parse_json_object(text)
    if data is None:
        return []

    raw_notes = data.get("notes")
    if not isinstance(raw_notes, list):
        return []

    notes = [
        note for note in (_parse_map_note(item, window) for item in raw_notes) if note is not None
    ]
    return notes[:max_notes]


def _parse_map_note(item: object, window: list[dict]) -> StudyNote | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    referenced = _resolve_numbered_references(item.get("chunks"), window)
    chunk_ids = tuple(chunk["id"] for chunk in referenced)
    section = referenced[0].get("section") if referenced else None
    page = referenced[0].get("page") if referenced else None
    return StudyNote(text=text, chunk_ids=chunk_ids, section=section, page=page)


_REDUCE_JSON_FORMAT_HINT = (
    '出力は次の形式の厳格な JSON のみとし、それ以外のテキストを含めないでください: '
    '{"notes": [{"text": "...", "sources": [1, 2]}], "tags": ["..."]}'
)

# codex --output-schema に渡す厳格 JSON スキーマ（reduce フェーズ）。
# MAP_SCHEMA の chunks 相当が sources（元 map ノート番号）になり、
# 文書全体のタグ付けとして tags 配列が追加される。
REDUCE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["notes", "tags"],
    "additionalProperties": False,
}


def build_reduce_prompt(
    map_notes: list[StudyNote],
    *,
    tag_catalog: tuple[str, ...] = (),
    persona: str | None = None,
    title: str | None = None,
    max_notes: int = 20,
) -> str:
    """map フェーズの StudyNote 列全体から reduce フェーズのプロンプトを組み立てる。

    map_notes を [N1]..[Nn] で番号提示し、重複統合・タグ付与を LLM に指示する。
    parse_reduce が sources 番号を map_notes[index-1] の chunk_ids/section/page へ
    機械解決する（build_map_prompt/parse_map と対称の設計）。
    """
    instructions = []
    if persona is not None:
        instructions.append(f"あなたは{persona}である。")
    instructions.append(
        f"以下の学びノートの重複・冗長を統合し、資料全体として最重要の学びを"
        f"日本語で最大{max_notes}件に厳選してください。"
    )
    instructions.append("各学びに元ノート番号を sources 配列で必ず付けてください。")
    instructions.append("統合時は元の具体性を保ってください（薄い一般論に丸めないでください）。")
    instructions.append(
        "この資料の内容を表すタグを3〜8個、日本語で挙げてください。"
    )
    if tag_catalog:
        catalog = "、".join(tag_catalog)
        instructions.append(
            f"既存タグ一覧: {catalog}。表記揺れを避け、意味が合う既存タグを優先的に"
            "再利用してください。合うものがなければ新規タグを作成してもかまいません。"
        )
    instructions.append(_REDUCE_JSON_FORMAT_HINT)

    title_line = f"\n\nタイトル: {title}" if title is not None else ""
    notes_body = "\n\n".join(
        f"[N{index}] {note.text}" for index, note in enumerate(map_notes, start=1)
    )

    return "\n".join(instructions) + title_line + f"\n\n{notes_body}"


def parse_reduce(
    text: str, map_notes: list[StudyNote], *, max_notes: int = 20
) -> tuple[list[StudyNote], list[str]]:
    """reduce フェーズのエンジン生出力を、統合済み StudyNote 列 + 正規化タグ列へ変換する。

    sources 配列（1 起点の [N番号]）を map_notes[index-1] へ機械解決し、
    参照元 chunk_ids の和集合（出現順維持・重複除去）を統合ノートの chunk_ids とする
    （parse_map と対称の設計。§ chunk 接地を reduce 後も失わないため）。
    JSON 全体のパース失敗は ([], []) へ劣化させ、呼び出し側 service.py が
    map フェーズの結果へフォールバックするかを判断できるようにする。
    """
    data = _parse_json_object(text)
    if data is None:
        return [], []

    raw_notes = data.get("notes")
    notes: list[StudyNote] = []
    if isinstance(raw_notes, list):
        notes = [
            note
            for note in (_parse_reduce_note(item, map_notes) for item in raw_notes)
            if note is not None
        ][:max_notes]

    raw_tags = data.get("tags")
    tags = normalize_tags(raw_tags) if isinstance(raw_tags, list) else []

    return notes, tags


def _parse_reduce_note(item: object, map_notes: list[StudyNote]) -> StudyNote | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    referenced = _resolve_numbered_references(item.get("sources"), map_notes)
    chunk_ids = _union_chunk_ids(referenced)
    section = referenced[0].section if referenced else None
    page = referenced[0].page if referenced else None
    return StudyNote(text=text, chunk_ids=chunk_ids, section=section, page=page)


def _union_chunk_ids(notes: list[StudyNote]) -> tuple[str, ...]:
    """参照元ノート群の chunk_ids を出現順維持・重複除去で和集合にする。"""
    seen: set[str] = set()
    result: list[str] = []
    for note in notes:
        for chunk_id in note.chunk_ids:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            result.append(chunk_id)
    return tuple(result)


def normalize_tag(raw: object) -> str | None:
    """タグ 1 件を正規化する: NFKC 正規化 → strip → lower → 連続空白を "-" に置換。

    unicodedata は json と並ぶ標準ライブラリであり、test_boundaries.py の
    _RESTRICTED_TO_OWNER（sqlite3/subprocess/fastembed 等の外部 SDK 限定）には
    含まれないため digests.py からの import 制約に抵触しない
    （モジュール冒頭の「json + ports.py のみ」は外部 SDK ゼロという意図であり、
    stdlib 全般を禁止する制約ではないと判断した）。
    非 str・正規化後に空・30 字超はいずれもタグとして無効なため None を返す
    （呼び出し側 normalize_tags がまとめて除外する）。
    """
    if not isinstance(raw, str):
        return None
    normalized = unicodedata.normalize("NFKC", raw).strip().lower()
    normalized = "-".join(normalized.split())
    if not normalized or len(normalized) > 30:
        return None
    return normalized


def normalize_tags(raws: list, *, max_tags: int = 8) -> list[str]:
    """タグ列を正規化する: normalize_tag で無効化された要素を除去し、
    出現順維持で重複除去した上で max_tags にクランプする。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in raws:
        tag = normalize_tag(raw)
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
        if len(result) >= max_tags:
            break
    return result


def _resolve_numbered_references(raw: object, items: list) -> list:
    """1 起点の番号列を items（window の dict 列 / map_notes の StudyNote 列）へ解決する
    （重複除去・出現順維持）。parse_map の chunks 番号解決・parse_reduce の sources 番号
    解決は要素の型が違うだけで規則が同一のため、この 1 つに集約する。

    prompts._normalize_marker_ids と同じ検証規則（bool は int のサブクラスだが
    JSON 上は真偽値であり番号ではないため除外・非正整数除外）を踏襲する。
    """
    if not isinstance(raw, list):
        return []
    seen: set[int] = set()
    result: list = []
    for number in raw:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        if number > len(items) or number in seen:
            continue
        seen.add(number)
        result.append(items[number - 1])
    return result
