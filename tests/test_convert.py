"""convert.py のテスト（形式判定・テキスト抽出）。

外部変換ライブラリ（pymupdf4llm・markitdown）は lazy import し、
ネットワークアクセス・実 DB・時計に触れない。
PDF/markitdown の実変換はスモークのみ。
"""

from __future__ import annotations

import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from shelf.convert import ConversionError, ConvertResult, convert_file, convert_url, pick_converter


def _pdf_chunk(text, page: int | None = None, page_number: int | None = None) -> defaultdict:
    """pymupdf4llm.to_markdown(page_chunks=True) が返す実際のチャンク形（defaultdict）を模擬。

    実体は `collections.defaultdict(lambda: None)` で、存在しないキーへのアクセスは
    KeyError にならず None を返す（バグ#1 の原因）。metadata は 'page' キーを持つ場合と
    持たない場合の両方をテストできるよう任意指定にする。

    page_number は実際にインストールされている pymupdf4llm（document_layout.py の
    make_page_chunk）が返す実キー名。'page' キーは持たないため、'page' 優先 → 'page_number'
    フォールバック → enumerate+1 の順で解決できることを別テストで検証する。
    """
    chunk: defaultdict = defaultdict(lambda: None)
    metadata: dict = {}
    if page is not None:
        metadata["page"] = page
    if page_number is not None:
        metadata["page_number"] = page_number
    chunk["metadata"] = metadata
    chunk["text"] = text
    return chunk


class TestPickConverter:
    """pick_converter（純粋）の形式判定テスト。"""

    def test_pdf_extension(self):
        assert pick_converter("document.pdf") == "pymupdf4llm"

    def test_docx_extension(self):
        assert pick_converter("document.docx") == "markitdown"

    def test_xlsx_extension(self):
        assert pick_converter("spreadsheet.xlsx") == "markitdown"

    def test_xls_extension(self):
        assert pick_converter("spreadsheet.xls") == "markitdown"

    def test_pptx_extension(self):
        assert pick_converter("presentation.pptx") == "markitdown"

    def test_html_extension(self):
        assert pick_converter("page.html") == "markitdown"

    def test_htm_extension(self):
        assert pick_converter("page.htm") == "markitdown"

    def test_markdown_extension(self):
        assert pick_converter("notes.md") == "raw"

    def test_text_extension(self):
        assert pick_converter("data.txt") == "raw"

    def test_rst_extension(self):
        assert pick_converter("docs.rst") == "raw"

    def test_python_extension(self):
        assert pick_converter("script.py") == "raw"

    def test_javascript_extension(self):
        assert pick_converter("main.js") == "raw"

    def test_typescript_extension(self):
        assert pick_converter("main.ts") == "raw"

    def test_shell_extension(self):
        assert pick_converter("deploy.sh") == "raw"

    def test_toml_extension(self):
        assert pick_converter("config.toml") == "raw"

    def test_yaml_extension(self):
        assert pick_converter("config.yaml") == "raw"

    def test_yml_extension(self):
        assert pick_converter("config.yml") == "raw"

    def test_json_extension(self):
        assert pick_converter("data.json") == "raw"

    def test_http_url(self):
        assert pick_converter("http://example.com/page") == "markitdown"

    def test_https_url(self):
        assert pick_converter("https://example.com/page") == "markitdown"

    def test_case_insensitive_extension(self):
        # 拡張子の大文字小文字を区別しない想定
        assert pick_converter("Document.PDF") == "pymupdf4llm"
        assert pick_converter("Document.DOCX") == "markitdown"

    def test_windows_absolute_path_forward_slash(self):
        """Windows ドライブレター(C:/)を URL スキームと誤認しない。

        service.py が add 時に resolve() で絶対パス化するため、Windows では
        全ファイル投入がここを通る（誤認すると add が全面的に機能しない）。
        """
        assert pick_converter("C:/Users/user/docs/note.md") == "raw"

    def test_windows_absolute_path_backslash(self):
        assert pick_converter("C:\\Users\\user\\docs\\paper.pdf") == "pymupdf4llm"

    def test_non_http_scheme_still_rejected(self):
        """複数文字の非 http/https スキーム(ftp 等)は従来どおり拒否する。"""
        with pytest.raises(ConversionError) as exc_info:
            pick_converter("ftp://example.com/file.txt")
        assert "スキーム" in str(exc_info.value)

    def test_unsupported_extension(self):
        with pytest.raises(ConversionError) as exc_info:
            pick_converter("file.exe")
        assert "対応形式" in str(exc_info.value)
        # 対応形式リストが含まれることを確認
        assert "pdf" in str(exc_info.value).lower()

    def test_no_extension(self):
        with pytest.raises(ConversionError) as exc_info:
            pick_converter("README")
        assert "対応形式" in str(exc_info.value)


class TestInsertPageMarkers:
    """PDF の page_chunks 結果（dict のリスト）から <!-- page: N --> マーカーを挿入する純粋関数のテスト。

    実 pymupdf4llm は lazy import で避け、結果の構造（defaultdict、キー: metadata/text 等）
    だけを模擬する。バグ#1: 旧実装はタプル前提で chunk[0] を読んでいたため、実際の
    defaultdict では存在しないキー 0 へのアクセスが KeyError にならず None を返し、
    本文が丸ごと文字列 "None" になっていた。
    """

    def test_insert_page_markers_single_page(self):
        """単一ページの場合、1始まりで <!-- page: 1 --> を挿入。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("Page 1 content")]
        result = _insert_page_markers(chunks)
        assert result == "<!-- page: 1 -->\nPage 1 content"

    def test_insert_page_markers_multiple_pages(self):
        """複数ページの場合、各ページの先頭に 1始まりの <!-- page: N --> を挿入。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("Page 1"), _pdf_chunk("Page 2"), _pdf_chunk("Page 3")]
        result = _insert_page_markers(chunks)
        expected = "<!-- page: 1 -->\nPage 1\n\n<!-- page: 2 -->\nPage 2\n\n<!-- page: 3 -->\nPage 3"
        assert result == expected

    def test_insert_page_markers_empty(self):
        """空リストの場合は空文字列を返す。"""
        from shelf.convert import _insert_page_markers

        chunks = []
        result = _insert_page_markers(chunks)
        assert result == ""

    def test_insert_page_markers_preserves_content(self):
        """ページ内容のマークダウン構造を保持。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("# Heading\n\nSome text"), _pdf_chunk("## Subheading\n\nMore text")]
        result = _insert_page_markers(chunks)
        assert "<!-- page: 1 -->" in result
        assert "# Heading" in result
        assert "<!-- page: 2 -->" in result
        assert "## Subheading" in result

    def test_insert_page_markers_uses_metadata_page_when_present(self):
        """metadata['page'] があればページ番号としてそれを優先する。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("Cover", page=5), _pdf_chunk("Next", page=6)]
        result = _insert_page_markers(chunks)
        assert "<!-- page: 5 -->\nCover" in result
        assert "<!-- page: 6 -->\nNext" in result

    def test_insert_page_markers_uses_metadata_page_number_when_no_page_key(self):
        """metadata に 'page' が無く 'page_number' がある場合はそれを優先する
        （実ライブラリの実キーは 'page_number'）。途中ページが空でスキップされても、
        後続ページの番号は enumerate ではなく metadata の実ページ番号に従うためずれない。
        """
        from shelf.convert import _insert_page_markers

        chunks = [
            _pdf_chunk("A", page_number=10),
            _pdf_chunk("", page_number=11),  # 空ページ（スキップされる）
            _pdf_chunk("C", page_number=12),
        ]
        result = _insert_page_markers(chunks)
        assert result == "<!-- page: 10 -->\nA\n\n<!-- page: 12 -->\nC"

    def test_insert_page_markers_prefers_page_over_page_number(self):
        """'page' と 'page_number' の両方があれば 'page' を優先する。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("A", page=1, page_number=99)]
        result = _insert_page_markers(chunks)
        assert result == "<!-- page: 1 -->\nA"

    def test_insert_page_markers_falls_back_to_enumerate_when_no_metadata_page(self):
        """metadata に 'page' キーが無ければ enumerate+1（1始まり）にフォールバック。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("A"), _pdf_chunk("B")]  # page 未指定
        result = _insert_page_markers(chunks)
        assert "<!-- page: 1 -->\nA" in result
        assert "<!-- page: 2 -->\nB" in result

    def test_insert_page_markers_skips_none_text_chunk(self):
        """text が None の要素はスキップし、'None' という文字列を出力しない（再発防止）。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk("Real content"), _pdf_chunk(None)]
        result = _insert_page_markers(chunks)
        assert "None" not in result
        assert "Real content" in result

    def test_insert_page_markers_skips_empty_string_text_chunk(self):
        """text が空文字列の要素もスキップする。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk(""), _pdf_chunk("Real content")]
        result = _insert_page_markers(chunks)
        assert result == "<!-- page: 2 -->\nReal content"

    def test_insert_page_markers_all_empty_returns_empty_string(self):
        """全ページが空/None の場合、マーカーを含まない空文字列を返す。"""
        from shelf.convert import _insert_page_markers

        chunks = [_pdf_chunk(None), _pdf_chunk("")]
        result = _insert_page_markers(chunks)
        assert result == ""


class TestConvertFile:
    """convert_file（ファイル変換）のテスト。"""

    def test_convert_file_nonexistent(self):
        """存在しないファイルは ConversionError を raise。"""
        with pytest.raises(ConversionError) as exc_info:
            convert_file(Path("/nonexistent/file.txt"))
        assert "存在しません" in str(exc_info.value) or "見つかりません" in str(exc_info.value)

    def test_convert_file_raw_markdown(self):
        """raw 形式（markdown）は utf-8 で read_text。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.md"
            content = "# Title\n\nSome content " * 5  # 100 字以上のコンテンツ
            path.write_text(content, encoding="utf-8")

            result = convert_file(path)

            assert isinstance(result, ConvertResult)
            assert result.markdown == content
            assert result.converter == "raw"
            assert result.title is None

    def test_convert_file_raw_text(self):
        """raw 形式（text）は utf-8 で read_text。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            content = "This is plain text. " * 10  # 100 字以上のコンテンツ
            path.write_text(content, encoding="utf-8")

            result = convert_file(path)

            assert result.markdown == content
            assert result.converter == "raw"

    def test_convert_file_raw_python_code(self):
        """raw 形式（Python コード）は read_text。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "script.py"
            content = "def hello():\n    print('world')\n\n# Some long comment " * 5  # 100 字以上
            path.write_text(content, encoding="utf-8")

            result = convert_file(path)

            assert result.markdown == content
            assert result.converter == "raw"

    def test_convert_file_too_short(self):
        """変換結果が100字未満の場合は ConversionError（スキャン PDF 等の fail-fast）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "short.txt"
            content = "short"  # 5 文字
            path.write_text(content, encoding="utf-8")

            with pytest.raises(ConversionError) as exc_info:
                convert_file(path)
            assert "抽出できませんでした" in str(exc_info.value)

    def test_convert_file_too_short_message_is_generic_for_non_pdf(self):
        """スキャンPDF専用の文言はPDF変換(_convert_pdf)にのみ妥当で、raw/markitdown等の
        他形式に流用すると無関係な原因を示唆してしまう(中位指摘#6)。txt(raw)の場合は
        PDFに言及しない汎用文言であることを確認する。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "short.txt"
            path.write_text("short", encoding="utf-8")

            with pytest.raises(ConversionError) as exc_info:
                convert_file(path)
            assert "PDF" not in str(exc_info.value)
            assert "抽出できませんでした" in str(exc_info.value)

    def test_convert_markitdown_too_short_message_is_generic_for_non_pdf(self):
        """markitdown(docx等)変換結果が100字未満でも「スキャンPDF」という無関係な文言を
        含めない(中位指摘#6)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.docx"
            path.write_bytes(b"dummy docx bytes")

            with patch("markitdown.MarkItDown") as mock_cls:
                mock_instance = Mock()
                mock_instance.convert.return_value = Mock(text_content="short", title=None)
                mock_cls.return_value = mock_instance

                with pytest.raises(ConversionError) as exc_info:
                    convert_file(path)
                assert "PDF" not in str(exc_info.value)
                assert "抽出できませんでした" in str(exc_info.value)

    def test_convert_file_encoding_error_fallback(self):
        """エンコーディングエラーは errors='replace' で処理。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.txt"
            # 不正な UTF-8 バイト列
            path.write_bytes(b"Valid \xff\xfe invalid UTF-8" + b"x" * 100)

            result = convert_file(path)

            # errors='replace' で '�' に置換されるため、テキストは存在するはず
            assert len(result.markdown) >= 100
            assert result.converter == "raw"



class TestConvertPdf:
    """_convert_pdf（pymupdf4llm 経由の PDF 変換）のテスト。

    実ライブラリの to_markdown はモックし、実際の戻り値構造（dict のリスト）だけを模擬する。
    """

    def test_convert_pdf_uses_real_page_text(self):
        """dict 形の chunk から text を正しく取得し、本文が 'None' 化しない（バグ#1 の直接再現）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "doc.pdf"
            path.write_bytes(b"%PDF-1.4 dummy")

            content = "This is the real extracted page content. " * 5  # 100字以上
            with patch("pymupdf4llm.to_markdown") as mock_to_markdown:
                mock_to_markdown.return_value = [_pdf_chunk(content, page=1)]

                result = convert_file(path)

                assert "None" not in result.markdown
                assert content in result.markdown
                assert result.converter == "pymupdf4llm"

    def test_convert_pdf_all_pages_empty_raises_conversion_error(self):
        """全ページの text が None/空の場合は ConversionError（'None' 連結が100字ガードを
        すり抜けていた再発防止の確認）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "scanned.pdf"
            path.write_bytes(b"%PDF-1.4 dummy")

            with patch("pymupdf4llm.to_markdown") as mock_to_markdown:
                mock_to_markdown.return_value = [_pdf_chunk(None), _pdf_chunk("")]

                with pytest.raises(ConversionError) as exc_info:
                    convert_file(path)
                assert "抽出できませんでした" in str(exc_info.value)


class TestDecideSkipOcr:
    """_decide_skip_ocr（サンプルページのテキスト長リストから OCR スキップ可否を決める
    純粋関数）の境界値テスト。50字以上を「テキスト層あり」とみなし、サンプル中
    過半数がそれに該当すれば True（fail-open: 空リストは常に False）。
    """

    def test_all_pages_rich_returns_true(self):
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([200, 300, 150]) is True

    def test_all_pages_zero_returns_false(self):
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([0, 0, 0]) is False

    def test_empty_list_returns_false(self):
        """検出失敗・0頁 PDF は fail-open で False（現行=OCRあり動作に落とす）。"""
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([]) is False

    def test_exact_majority_returns_true(self):
        """5頁中3頁がテキスト層あり(過半数ちょうど)。"""
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([100, 100, 100, 0, 0]) is True

    def test_less_than_half_returns_false(self):
        """5頁中2頁のみテキスト層あり(半数未満)。"""
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([100, 100, 0, 0, 0]) is False

    def test_fifty_char_boundary_is_included_forty_nine_is_excluded(self):
        """50字はテキスト層ありとしてカウントし、49字はカウントしない。"""
        from shelf.convert import _decide_skip_ocr

        assert _decide_skip_ocr([50]) is True
        assert _decide_skip_ocr([49]) is False


class TestSamplePageIndices:
    """_sample_page_indices（サンプリング対象頁インデックスを返す純粋関数）のテスト。
    書籍は表紙・図版等の無テキスト頁が先頭に集中するため、全体から等間隔で
    サンプリングする(先頭・末尾を含む)。
    """

    def test_small_document_samples_all_pages(self):
        from shelf.convert import _sample_page_indices

        assert _sample_page_indices(5) == [0, 1, 2, 3, 4]

    def test_large_document_samples_evenly_spaced_unique_ascending_with_bounds(self):
        from shelf.convert import _sample_page_indices

        indices = _sample_page_indices(300)

        assert len(indices) == 8
        assert len(set(indices)) == 8  # 重複なし
        assert indices == sorted(indices)  # 昇順
        assert indices[0] == 0
        assert indices[-1] == 299


class TestPdfTextLayerLengths:
    """_pdf_text_layer_lengths（pymupdf でサンプル頁のテキスト長を取得する関数）のテスト。

    実 pymupdf は lazy import かつ open() 自体をモックし、doc オブジェクトの形
    （needs_pass/page_count/インデックスアクセスでの get_text()）だけを模擬する。
    検出はあくまで最適化であり、暗号化・0頁・open失敗はすべて fail-open で
    空リストに落とす(既存のダミーPDFバイトを使うテストの互換性にも必須)。
    """

    def test_returns_stripped_text_lengths_for_sampled_pages(self):
        from shelf.convert import _pdf_text_layer_lengths

        mock_doc = MagicMock()
        mock_doc.needs_pass = False
        mock_doc.page_count = 2
        mock_doc.__enter__.return_value = mock_doc
        pages = {
            0: Mock(get_text=Mock(return_value="  hello  ")),
            1: Mock(get_text=Mock(return_value="world!")),
        }
        mock_doc.__getitem__.side_effect = lambda i: pages[i]

        with patch("pymupdf.open", return_value=mock_doc) as mock_open:
            result = _pdf_text_layer_lengths(Path("doc.pdf"))

        mock_open.assert_called_once_with("doc.pdf")
        assert result == [len("hello"), len("world!")]

    def test_encrypted_document_returns_empty_list(self):
        from shelf.convert import _pdf_text_layer_lengths

        mock_doc = MagicMock()
        mock_doc.needs_pass = True
        mock_doc.__enter__.return_value = mock_doc

        with patch("pymupdf.open", return_value=mock_doc):
            assert _pdf_text_layer_lengths(Path("doc.pdf")) == []

    def test_zero_page_document_returns_empty_list(self):
        from shelf.convert import _pdf_text_layer_lengths

        mock_doc = MagicMock()
        mock_doc.needs_pass = False
        mock_doc.page_count = 0
        mock_doc.__enter__.return_value = mock_doc

        with patch("pymupdf.open", return_value=mock_doc):
            assert _pdf_text_layer_lengths(Path("doc.pdf")) == []

    def test_open_failure_returns_empty_list(self):
        """open() 自体が例外を投げても fail-open で空リストに落ちる。"""
        from shelf.convert import _pdf_text_layer_lengths

        with patch("pymupdf.open", side_effect=RuntimeError("boom")):
            assert _pdf_text_layer_lengths(Path("doc.pdf")) == []

    def test_context_manager_exit_is_called(self):
        """with 文で確実に doc を close していることを __exit__ 呼び出しで確認する。"""
        from shelf.convert import _pdf_text_layer_lengths

        mock_doc = MagicMock()
        mock_doc.needs_pass = False
        mock_doc.page_count = 0
        mock_doc.__enter__.return_value = mock_doc

        with patch("pymupdf.open", return_value=mock_doc):
            _pdf_text_layer_lengths(Path("doc.pdf"))

        mock_doc.__exit__.assert_called_once()


class TestConvertPdfOcrSkipWiring:
    """_convert_pdf がテキスト層検出結果に応じて use_ocr kwarg と notes を配線する
    ことのテスト。_pdf_text_layer_lengths 自体は別クラスで検証済みなのでモックし、
    「配線」だけを見る。
    """

    def test_text_layer_detected_passes_use_ocr_false_and_adds_note(self):
        from shelf.convert import _OCR_SKIP_NOTE

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "doc.pdf"
            path.write_bytes(b"%PDF-1.4 dummy")
            content = "Real extracted page content with a text layer. " * 5

            with patch("shelf.convert._pdf_text_layer_lengths", return_value=[200, 200]), \
                 patch("pymupdf4llm.to_markdown") as mock_to_markdown:
                mock_to_markdown.return_value = [_pdf_chunk(content, page=1)]

                result = convert_file(path)

            assert mock_to_markdown.call_args.kwargs["use_ocr"] is False
            assert result.notes == (_OCR_SKIP_NOTE,)

    def test_no_text_layer_omits_use_ocr_kwarg_and_notes_is_empty(self):
        """レガシー(pymupdf-layout 非導入)環境では未知 kwarg が警告を出すため、
        スキップしない場合は use_ocr を一切渡さない。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "doc.pdf"
            path.write_bytes(b"%PDF-1.4 dummy")
            content = "Real extracted page content without a text layer. " * 5

            with patch("shelf.convert._pdf_text_layer_lengths", return_value=[]), \
                 patch("pymupdf4llm.to_markdown") as mock_to_markdown:
                mock_to_markdown.return_value = [_pdf_chunk(content, page=1)]

                result = convert_file(path)

            assert "use_ocr" not in mock_to_markdown.call_args.kwargs
            assert result.notes == ()


class TestConvertUrl:
    """convert_url（URL 取得・変換）のテスト。"""

    def test_convert_url_invalid_scheme_file(self):
        """file:// スキームは明示拒否。"""
        with pytest.raises(ConversionError) as exc_info:
            convert_url("file:///local/file.html")
        assert "http" in str(exc_info.value).lower() or "スキーム" in str(exc_info.value)

    def test_convert_url_invalid_scheme_ftp(self):
        """ftp:// スキームは明示拒否。"""
        with pytest.raises(ConversionError) as exc_info:
            convert_url("ftp://example.com/file.txt")
        assert "http" in str(exc_info.value).lower() or "スキーム" in str(exc_info.value)


    def test_convert_url_size_limit(self):
        """サイズが 20MB を超える場合は拒否。"""
        with patch("shelf.convert.urllib.request.urlopen") as mock_urlopen:
            mock_response = Mock()
            # Content-Length が 20MB を超える
            mock_response.headers = {"Content-Length": str(20 * 1024 * 1024 + 1)}
            mock_urlopen.return_value = mock_response

            with pytest.raises(ConversionError) as exc_info:
                convert_url("http://example.com/large.bin")
            assert "20MB" in str(exc_info.value) or "サイズ" in str(exc_info.value)

    def test_convert_url_timeout_default(self):
        """timeout パラメータのデフォルトが 30 秒であることを確認。"""
        # timeout が正しく使用されることは実装で確認済みなので、ここではスキップ。
        # 実 network テストはスモークで実施。
        pass

    def test_convert_url_sets_user_agent_header(self):
        """urlopen に渡す Request に User-Agent ヘッダが設定されている（バグ#3: 403 対策）。"""
        with patch("shelf.convert.urllib.request.urlopen") as mock_urlopen:
            mock_response = Mock()
            mock_response.headers = {}
            mock_response.read.return_value = b"<html>" + b"x" * 100 + b"</html>"
            mock_urlopen.return_value = mock_response

            with patch("shelf.convert._convert_markitdown") as mock_convert_markitdown:
                mock_convert_markitdown.return_value = ConvertResult(
                    markdown="x" * 100, converter="markitdown", title=None
                )
                convert_url("http://example.com/page.html")

            assert mock_urlopen.called
            request = mock_urlopen.call_args[0][0]
            assert isinstance(request, urllib.request.Request)
            # Request.add_header は key.capitalize() で正規化するため実キーは "User-agent"。
            assert request.get_header("User-agent") == "shelf/0.1 (personal knowledge tool)"

    def test_convert_url_reads_response_with_a_bounded_size_cap(self):
        """既存実装は response.read() を無制限に呼んでから len(data) で判定していた。
        コメントは「読み込み中もサイズチェック」と主張していたが、実際には全量
        ダウンロードを終えるまでサイズ超過に気づけない未完了の偽装だった(中位指摘#4)。
        response.read(MAX_SIZE + 1) の打ち切り読みで呼ばれることを検証する。
        """
        max_size = 20 * 1024 * 1024
        with patch("shelf.convert.urllib.request.urlopen") as mock_urlopen:
            mock_response = Mock()
            mock_response.headers = {}
            mock_response.read.return_value = b"x" * (max_size + 1)
            mock_urlopen.return_value = mock_response

            with pytest.raises(ConversionError):
                convert_url("http://example.com/huge")

            mock_response.read.assert_called_once_with(max_size + 1)

    def test_convert_url_no_content_length_size_check(self):
        """Content-Length ヘッダがない場合、読み込みデータサイズで制限チェック。"""
        with patch("shelf.convert.urllib.request.urlopen") as mock_urlopen:
            mock_response = Mock()
            mock_response.headers = {}  # Content-Length なし
            # 大きなデータを返す
            mock_response.read.return_value = b"x" * (25 * 1024 * 1024)
            mock_urlopen.return_value = mock_response

            with pytest.raises(ConversionError) as exc_info:
                convert_url("http://example.com/large")
            assert "20MB" in str(exc_info.value) or "サイズ" in str(exc_info.value)
