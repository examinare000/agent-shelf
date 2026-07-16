"""chunker.chunk_markdown の純粋関数テスト（I/O・DB・ネットワーク一切なし）。"""
from __future__ import annotations

import re
import string

from shelf.chunker import Chunk, chunk_markdown


class TestEmptyInput:
    def test_empty_string_returns_empty_list(self):
        assert chunk_markdown("", notebook="nb", doc_id="doc1", source_path="doc1.md") == []

    def test_whitespace_only_returns_empty_list(self):
        md = "   \n\n  \t\n"
        assert chunk_markdown(md, notebook="nb", doc_id="doc1", source_path="doc1.md") == []


class TestBasicSingleChunk:
    def test_short_plain_text_becomes_one_chunk_with_metadata(self):
        md = "これは短い本文です。"
        chunks = chunk_markdown(md, notebook="physics", doc_id="doc1", source_path="physics/doc1.md")
        assert len(chunks) == 1
        chunk = chunks[0]
        assert isinstance(chunk, Chunk)
        assert chunk.id == "physics/doc1#0"
        assert chunk.notebook == "physics"
        assert chunk.doc_id == "doc1"
        assert chunk.source_path == "physics/doc1.md"
        assert chunk.section is None
        assert chunk.page is None
        assert chunk.seq == 0
        assert chunk.text == "これは短い本文です。"


class TestHeadingSections:
    def test_single_heading_becomes_section_breadcrumb(self):
        md = "# 第1章 導入\n\n本文その1。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 1
        assert chunks[0].section == "第1章 導入"
        assert chunks[0].text == "本文その1。"

    def test_nested_headings_build_breadcrumb(self):
        md = (
            "# 第3章 エネルギー\n\n"
            "## 3.2 エネルギー保存\n\n"
            "保存則の本文。"
        )
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 1
        assert chunks[0].section == "第3章 エネルギー > 3.2 エネルギー保存"

    def test_sibling_heading_resets_breadcrumb_at_same_level(self):
        md = (
            "# 章A\n\n"
            "## 節1\n\n"
            "本文1。\n\n"
            "# 章B\n\n"
            "本文2。"
        )
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        sections = [c.section for c in chunks]
        assert sections == ["章A > 節1", "章B"]

    def test_content_before_first_heading_has_no_section(self):
        md = "冒頭の本文。\n\n# 見出し\n\n見出し後の本文。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert [c.section for c in chunks] == [None, "見出し"]

    def test_heading_with_no_body_produces_no_chunk(self):
        md = "# 空の見出し\n\n## 次の見出し\n\n本文あり。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 1
        assert chunks[0].section == "空の見出し > 次の見出し"
        assert chunks[0].text == "本文あり。"

    def test_seq_and_id_increment_across_sections(self):
        md = "# A\n\n本文a。\n\n# B\n\n本文b。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert [c.seq for c in chunks] == [0, 1]
        assert [c.id for c in chunks] == ["nb/d#0", "nb/d#1"]


class TestPageMarkers:
    def test_page_marker_before_body_sets_page_and_is_removed_from_text(self):
        md = "<!-- page: 5 -->\n\n本文。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 1
        assert chunks[0].page == 5
        assert chunks[0].text == "本文。"
        assert "page" not in chunks[0].text

    def test_no_marker_leaves_page_none(self):
        md = "本文のみ。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert chunks[0].page is None

    def test_marker_mid_body_splits_chunk_and_updates_page(self):
        md = "本文A。\n\n<!-- page: 2 -->\n\n本文B。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 2
        assert chunks[0].page is None
        assert chunks[0].text == "本文A。"
        assert chunks[1].page == 2
        assert chunks[1].text == "本文B。"

    def test_page_persists_across_paragraphs_until_next_marker(self):
        md = "<!-- page: 3 -->\n\n段落1。\n\n段落2。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        assert len(chunks) == 1
        assert chunks[0].page == 3
        assert chunks[0].text == "段落1。\n\n段落2。"


class TestSizeBasedSplittingAndOverlap:
    def test_short_paragraphs_overflow_target_chars_and_overlap_tail(self):
        md = "AAAAAAAAAA\n\nBBBBBBBBBB\n\nCCCCCCCCCC"
        chunks = chunk_markdown(
            md,
            notebook="nb",
            doc_id="d",
            source_path="d.md",
            target_chars=20,
            max_chars=25,
            overlap_ratio=0.5,
        )
        assert len(chunks) == 3
        assert chunks[0].text == "AAAAAAAAAA"
        # 直前チャンク末尾(overlap_chars=10文字)を次チャンク先頭に持ち越す
        assert chunks[1].text.startswith(chunks[0].text[-10:])
        assert chunks[2].text.startswith(chunks[1].text[-10:])

    def test_giant_single_paragraph_without_blank_lines_is_force_split_with_overlap(self):
        big_text = string.ascii_uppercase + string.digits + string.ascii_lowercase[:14]
        assert len(big_text) == 50  # 全文字が一意 → overlap の一致が偶然でないことを保証
        chunks = chunk_markdown(
            big_text,
            notebook="nb",
            doc_id="d",
            source_path="d.md",
            target_chars=20,
            max_chars=25,
            overlap_ratio=0.15,
        )
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.text) <= 25
        for prev, nxt in zip(chunks, chunks[1:]):
            overlap_chars = round(20 * 0.15)
            assert nxt.text.startswith(prev.text[-overlap_chars:])


class TestTableNotSplit:
    def test_markdown_table_is_never_split_even_when_oversized(self):
        rows = "\n".join(f"| {i:03d} | value{i:03d} |" for i in range(60))
        table_md = "| id | value |\n| --- | --- |\n" + rows
        assert len(table_md) > 1000  # デフォルト max_chars を確実に超えるサイズ
        md = "前置きの本文です。\n\n" + table_md + "\n\n後続の本文です。"
        chunks = chunk_markdown(md, notebook="nb", doc_id="d", source_path="d.md")
        # テーブル全文が1チャンク内に丸ごと収まっている(行の途中で分割されていない)
        assert sum(table_md in c.text for c in chunks) == 1


class TestMaskAppliedBeforeSplit:
    def test_secret_spanning_a_force_split_boundary_is_still_masked(self):
        """mask を分割後に適用すると、強制文字分割の境界をまたぐ秘密文字列が
        正規表現の必要長を満たせず未マスクのまま残ってしまう回帰テスト。
        mask→分割の順で実装されていれば、境界に関係なく必ずマスクされる
        (recall/chunker.py の mask→truncate 順の知見と同じ考え方)。
        """
        secret = "SECRET1234567890"  # ちょうど分割境界をまたぐ位置に置く

        def mask(text: str) -> str:
            return re.sub(r"SECRET\d{10}", "<REDACTED>", text)

        big_text = "A" * 5 + secret + "B" * 5
        chunks = chunk_markdown(
            big_text,
            notebook="nb",
            doc_id="d",
            source_path="d.md",
            mask=mask,
            target_chars=10,
            max_chars=12,
            overlap_ratio=0.0,
        )
        assert all("SECRET" not in c.text for c in chunks)
        # overlap_ratio=0 のため各チャンクは重複なく連結すれば元テキストに戻る。
        # mask が分割前に全文へ適用されていれば、連結結果は完全にマスク済みになる。
        assert "".join(c.text for c in chunks) == "AAAAA<REDACTED>BBBBB"
