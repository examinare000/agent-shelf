"""Markdown 本文をチャンク列へ変換する純粋関数群。

I/O（ファイル読み込み・DB・embedding）を一切持たないため、文字列 fixture だけで
見出し階層・ページ番号・overlap の全パターンを高速に単体テストできる。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PAGE_MARKER_RE = re.compile(r"^\s*<!--\s*page:\s*(\d+)\s*-->\s*$", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _is_table_block(text: str) -> bool:
    """全行が Markdown テーブル行(`| ... |`)である段落かを判定する。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return bool(lines) and all(_TABLE_ROW_RE.match(ln) for ln in lines)


@dataclass(frozen=True)
class Chunk:
    id: str
    notebook: str
    doc_id: str
    source_path: str
    section: str | None
    page: int | None
    seq: int
    text: str


@dataclass(frozen=True)
class _Unit:
    """段落単位の中間表現。テキストとページ番号を紐付けたまま分割・結合する。

    is_table は「表を跨がない」制約のためのフラグ。表ブロックは max_chars を
    超えていても強制文字分割の対象から除外し、常に丸ごと1チャンクに収める。
    """

    text: str
    page: int | None
    is_table: bool = False


def chunk_markdown(
    md: str,
    *,
    notebook: str,
    doc_id: str,
    source_path: str,
    mask: Callable[[str], str] | None = None,
    target_chars: int = 800,
    max_chars: int = 1000,
    overlap_ratio: float = 0.15,
) -> list[Chunk]:
    if mask is not None:
        md = mask(md)
    if not md.strip():
        return []

    segments = _split_into_segments(md)

    chunks: list[Chunk] = []
    seq = 0
    for section, units in segments:
        if not units:
            continue
        for text, page in _pack_units(units, target_chars, max_chars, overlap_ratio):
            chunks.append(
                Chunk(
                    id=f"{notebook}/{doc_id}#{seq}",
                    notebook=notebook,
                    doc_id=doc_id,
                    source_path=source_path,
                    section=section,
                    page=page,
                    seq=seq,
                    text=text,
                )
            )
            seq += 1
    return chunks


def _split_into_segments(md: str) -> list[tuple[str | None, list[_Unit]]]:
    """見出し行を境界に本文を分割し、(見出しパンくず, 段落unit列) のペア列を返す。

    パンくずは見出しスタック（レベル順）を保持し、現レベル以上の見出しを
    pop してから push することで、兄弟見出しでのリセットを表現する。
    ページマーカー行は本文から除去しつつ current_page を更新し、
    かつ段落境界（空行相当）として扱うことで、マーカーを跨ぐ段落が
    誤って単一ページに属すことを防ぐ。
    """
    heading_stack: list[tuple[int, str]] = []
    segments: list[tuple[str | None, list[_Unit]]] = []
    current_section: str | None = None
    current_lines: list[tuple[str, int | None]] = []
    current_page: int | None = None

    def flush() -> None:
        segments.append((current_section, _lines_to_units(current_lines)))

    for raw_line in md.split("\n"):
        m_marker = _PAGE_MARKER_RE.match(raw_line)
        if m_marker:
            current_page = int(m_marker.group(1))
            current_lines.append(("", None))  # 段落境界として扱う（マーカー行自体は除去）
            continue

        m_heading = _HEADING_RE.match(raw_line)
        if m_heading:
            flush()
            current_lines = []
            level = len(m_heading.group(1))
            title = m_heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_section = " > ".join(t for _, t in heading_stack)
            continue

        current_lines.append((raw_line, current_page))
    flush()

    return segments


def _lines_to_units(line_page_pairs: list[tuple[str, int | None]]) -> list[_Unit]:
    """空行（マーカー跡含む）区切りで段落単位にまとめ、各段落の先頭行のページを付与する。"""
    units: list[_Unit] = []
    buf: list[tuple[str, int | None]] = []

    def flush_buf() -> None:
        if not buf:
            return
        text = "\n".join(t for t, _ in buf).strip("\n")
        if not text.strip():
            return
        units.append(_Unit(text=text, page=buf[0][1], is_table=_is_table_block(text)))

    for line, page in line_page_pairs:
        if not line.strip():
            flush_buf()
            buf = []
        else:
            buf.append((line, page))
    flush_buf()

    return units


def _char_split_unit(u: _Unit, target_chars: int) -> list[_Unit]:
    """table でない巨大 unit を target_chars 単位の生スライスへ分割する。

    overlap はここでは付与しない。overlap は _pack_units 側の flush 処理で
    一元的に付与する設計にし、二重にオーバーラップが積み重なるのを避ける。
    """
    text = u.text
    pieces: list[_Unit] = []
    i = 0
    n = len(text)
    while i < n:
        pieces.append(_Unit(text=text[i : i + target_chars], page=u.page, is_table=False))
        i += target_chars
    return pieces


def _expand_oversized(units: list[_Unit], target_chars: int, max_chars: int) -> list[_Unit]:
    """max_chars を超える非表unitを強制分割する。表unitは「表を跨がない」仕様のため
    サイズに関わらず常に丸ごと1つの unit のまま残す。
    """
    expanded: list[_Unit] = []
    for u in units:
        if u.is_table or len(u.text) <= max_chars:
            expanded.append(u)
        else:
            expanded.extend(_char_split_unit(u, target_chars))
    return expanded


def _pack_units(
    units: list[_Unit], target_chars: int, max_chars: int, overlap_ratio: float
) -> list[tuple[str, int | None]]:
    """段落unitを target_chars 前後になるまで貪欲に詰め込み、溢れたら次チャンクへ
    overlap_ratio 分の末尾を持ち越す。

    現チャンク長 + 次unit長 が target_chars を超えたら flush する（max_chars では
    なく target_chars で判定するのは、「target_chars 前後」という仕様どおりに
    早めに区切るため）。

    ページ番号が変わる unit をまたいで結合すると、1チャンクに複数ページの本文が
    混在し引用の page が不正確になる。そのため次unitのページが現チャンクのページと
    異なる場合は、サイズに関わらず必ず flush してページ境界をチャンク境界に一致させる。
    """
    units = _expand_oversized(units, target_chars, max_chars)
    overlap_chars = max(0, round(target_chars * overlap_ratio))
    results: list[tuple[str, int | None]] = []
    current_text = ""
    current_page: int | None = None
    started = False

    for u in units:
        if not started:
            current_text = u.text
            current_page = u.page
            started = True
            continue

        page_changed = u.page != current_page
        candidate_len = len(current_text) + 2 + len(u.text)
        if candidate_len > target_chars or page_changed:
            results.append((current_text, current_page))
            # ページ境界での flush はオーバーラップを持ち越さない。持ち越すと
            # 前ページ本文の断片が次ページ扱いのチャンクに混入し、citation の
            # page 番号と実際の引用元テキストが食い違う（裏取りが壊れる）ため。
            if page_changed:
                current_text = u.text
            else:
                tail = current_text[-overlap_chars:] if overlap_chars > 0 else ""
                current_text = (tail + "\n\n" + u.text) if tail else u.text
            current_page = u.page
        else:
            current_text = current_text + "\n\n" + u.text

    if started:
        results.append((current_text, current_page))
    return results
