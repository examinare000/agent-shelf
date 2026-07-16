"""投入形式ごとの正規化：PDF・Office・テキスト・URL を Markdown に統一。

外部ライブラリ（pymupdf4llm・pymupdf・markitdown）の import はこのモジュールのみ、
かつ関数内 lazy import。urllib（標準ライブラリ）での fetch もこのモジュール内で完結。
"""

from __future__ import annotations

import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConversionError(ValueError):
    """変換処理のエラー。安全なメッセージのみを含む。"""

    pass


@dataclass(frozen=True)
class ConvertResult:
    """テキスト変換結果。"""

    markdown: str
    converter: str  # 'pymupdf4llm' | 'markitdown' | 'raw'
    title: str | None
    # 利用者への明示的な通知（例: OCRスキップ）。空タプルが既定で、全構築箇所が
    # キーワード引数呼び出しのため末尾追加でも非破壊(design doc/計画で grep 確認済み)。
    notes: tuple[str, ...] = ()


# テキスト層検出により OCR をスキップした際に notes へ載せる文言。
_OCR_SKIP_NOTE = "既存のテキスト層を検出したため OCR をスキップしました"

# サンプリングする最大頁数。頁毎の get_text() は数ms程度なので、大部数PDFでも
# 全頁走査を避けつつ十分な確度で判定できる。
_OCR_DETECTION_MAX_SAMPLE_PAGES = 8

# 1頁あたり、これ以上の文字数(strip後)があれば「テキスト層あり」の頁と判定する。
# 純スキャンPDFでも頁番号等の数字が拾われることがあるため、数十字程度の余裕を持つ。
_OCR_DETECTION_RICH_PAGE_CHAR_THRESHOLD = 50


def pick_converter(origin: str) -> str:
    """拡張子または URL スキームから適切な変換器名を返す純粋関数。

    Args:
        origin: ファイルパスまたは URL。

    Returns:
        'pymupdf4llm' | 'markitdown' | 'raw'

    Raises:
        ConversionError: 未対応形式の場合。
    """
    # URL スキームの判定
    parsed = urlparse(origin)
    if parsed.scheme in ("http", "https"):
        return "markitdown"

    # スキームがあるなら URL の一種だが http/https 以外は拒否。
    # ただし単一文字スキームは Windows ドライブレター（C:\... の resolve 結果）で
    # あり URL ではないため、ファイルパスとして拡張子判定へ流す。
    if len(parsed.scheme) > 1 and parsed.scheme not in ("http", "https"):
        raise ConversionError(f"スキーム {parsed.scheme} は対応していません。http/https のみを対応します。")

    # ファイル拡張子による判定
    path = Path(origin)
    ext = path.suffix.lower()

    # PDF: pymupdf4llm
    if ext == ".pdf":
        return "pymupdf4llm"

    # Office/HTML: markitdown
    if ext in (".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm"):
        return "markitdown"

    # Code/Text: raw
    if ext in (
        ".md",
        ".txt",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
    ):
        return "raw"

    # 未対応
    supported = [
        ".pdf (PDF)",
        ".docx, .xlsx, .xls, .pptx, .html, .htm (Office/HTML)",
        ".md, .txt, .rst, .py, .js, .ts, .sh, .toml, .yaml, .yml, .json (Code/Text)",
        "http://, https:// (URL)",
    ]
    raise ConversionError(f"対応形式: {', '.join(supported)}")


def _insert_page_markers(chunks: list[dict]) -> str:
    """PDF の page_chunks 結果から <!-- page: N --> マーカーを挿入する純粋関数。

    Args:
        chunks: pymupdf4llm.to_markdown(page_chunks=True) の戻り値（dict のリスト。
            実体は defaultdict(lambda: None) で、キーは metadata/toc_items/text 等）。

    Returns:
        ページマーカー付きの結合済みテキスト。text が None/空の要素はスキップし、
        全要素がスキップされた場合は空文字列を返す（呼び出し側の 100 字ガードに委ねる）。
    """
    if not chunks:
        return ""

    parts = []
    for idx, chunk in enumerate(chunks):
        text = chunk.get("text")
        if not text:
            continue

        metadata = chunk.get("metadata") or {}
        page_number = metadata.get("page")
        if not isinstance(page_number, int):
            # 実際にインストールされている pymupdf4llm のメタデータキーは "page" ではなく
            # "page_number"（document_layout.py の make_page_chunk）。空ページのスキップと
            # 無関係に実ページ番号を維持するため、enumerate フォールバックより優先する。
            page_number = metadata.get("page_number")
        if not isinstance(page_number, int):
            page_number = idx + 1  # 1始まり（フォールバック）

        parts.append(f"<!-- page: {page_number} -->\n{text}")

    return "\n\n".join(parts)


def convert_file(path: Path) -> ConvertResult:
    """ファイルを形式に応じて Markdown に変換。

    Args:
        path: 変換対象ファイルパス。

    Returns:
        ConvertResult

    Raises:
        ConversionError: ファイルが存在しない、形式未対応、内容が不十分の場合。
    """
    # 存在チェック
    if not path.exists():
        raise ConversionError(f"ファイルが存在しません: {path}")

    # 形式判定
    converter_name = pick_converter(str(path))

    # 形式ごとに変換
    if converter_name == "pymupdf4llm":
        return _convert_pdf(path)
    elif converter_name == "markitdown":
        return _convert_markitdown(path)
    else:  # "raw"
        return _convert_raw(path)


def _sample_page_indices(page_count: int, max_pages: int = _OCR_DETECTION_MAX_SAMPLE_PAGES) -> list[int]:
    """OCRスキップ判定のためにテキストを抽出する頁インデックス(0始まり)を返す純粋関数。

    書籍は表紙・図版・目次などテキストの薄い頁が先頭に集中しがちなので、先頭N頁を
    そのまま使うと「テキスト層なし」に誤判定しやすい。全頁から等間隔(先頭・末尾を
    必ず含む)でサンプリングすることで、文書全体の傾向に近い判定材料を得る。
    """
    if page_count <= max_pages:
        return list(range(page_count))

    step = (page_count - 1) / (max_pages - 1)
    # set で重複排除後に昇順ソート。等間隔の丸め計算では隣接インデックスが
    # 稀に一致し得るが、判定用のサンプル集合としては重複を持つ意味がない。
    return sorted({round(i * step) for i in range(max_pages)})


def _decide_skip_ocr(text_lengths: list[int]) -> bool:
    """サンプル頁のテキスト長リストから OCR をスキップしてよいかを決める純粋関数。

    fail-open: 空リスト(検出失敗・0頁)は常に False とし、現行動作(OCRあり)に
    委ねる。検出はあくまで最適化であり、判定不能を理由に変換自体を止めてはならない。
    """
    if not text_lengths:
        return False
    rich_page_count = sum(
        1 for length in text_lengths if length >= _OCR_DETECTION_RICH_PAGE_CHAR_THRESHOLD
    )
    return rich_page_count > len(text_lengths) / 2


def _pdf_text_layer_lengths(path: Path) -> list[int]:
    """サンプル頁ごとの、既存テキスト層の文字数(strip後)リストを返す。

    検出はあくまで最適化(_decide_skip_ocr への入力)であり、失敗しても変換自体を
    止めてはならない。暗号化PDF・0頁PDF・pymupdf.open() 失敗・その他あらゆる例外を
    区別せず fail-open で空リストに落とす。既存テストのダミーPDFバイト
    (`%PDF-1.4 dummy`)を pymupdf に渡しても壊れないための必須要件でもある。
    """
    try:
        import pymupdf  # lazy import: このモジュールのみに許可(test_boundaries.py)

        with pymupdf.open(str(path)) as doc:
            if doc.needs_pass or doc.page_count == 0:
                return []
            indices = _sample_page_indices(doc.page_count)
            return [len(doc[idx].get_text().strip()) for idx in indices]
    except Exception:
        return []


def _convert_pdf(path: Path) -> ConvertResult:
    """pymupdf4llm を使用した PDF 変換。"""
    import pymupdf4llm

    # pymupdf-layout 導入環境では to_markdown が既定で use_ocr=True になり、
    # ABBYY/ScanSnap 等が作る透明テキスト層(Tesseract形式と認識されない)を
    # 持つ頁でも再OCRが発火してしまう。既存テキスト層をここで事前検出できた
    # 場合のみ use_ocr=False を明示して再OCRを抑止する。
    text_lengths = _pdf_text_layer_lengths(path)
    skip_ocr = _decide_skip_ocr(text_lengths)

    # 非スキップ時は kwargs を一切渡さない: pymupdf-layout が導入されていない
    # legacy モードの環境では、to_markdown が未知の use_ocr kwarg を受け取ると
    # 呼び出しごとに警告 print を出す。スキップしない場合は「検出できなかった/
    # テキスト層がなかった」だけであり、legacy 環境への配慮を優先して kwarg 自体を
    # 省略する。
    kwargs = {"use_ocr": False} if skip_ocr else {}

    # page_chunks=True で各ページを分割
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, **kwargs)
    markdown = _insert_page_markers(chunks)

    # 100 字未満チェック
    if len(markdown) < 100:
        raise ConversionError(
            "テキストを抽出できませんでした（スキャン PDF の可能性があります）"
        )

    notes = (_OCR_SKIP_NOTE,) if skip_ocr else ()
    return ConvertResult(markdown=markdown, converter="pymupdf4llm", title=None, notes=notes)


def _convert_markitdown(path: Path) -> ConvertResult:
    """markitdown を使用した Office/HTML 変換。"""
    from markitdown import MarkItDown

    converter = MarkItDown()
    result = converter.convert(str(path))

    markdown = result.text_content
    title = getattr(result, "title", None)

    # 100 字未満チェック。「スキャン PDF の可能性」という文言は _convert_pdf 専用の
    # 原因説明であり、markitdown が扱う docx/xlsx/pptx/html には無関係(中位指摘#6)。
    if len(markdown) < 100:
        raise ConversionError("テキストを抽出できませんでした")

    return ConvertResult(markdown=markdown, converter="markitdown", title=title)


def _convert_raw(path: Path) -> ConvertResult:
    """テキスト形式（md/txt/code）の read_text。"""
    markdown = path.read_text(encoding="utf-8", errors="replace")

    # 100 字未満チェック。「スキャン PDF の可能性」という文言は _convert_pdf 専用の
    # 原因説明であり、raw が扱う md/txt/コードには無関係(中位指摘#6)。
    if len(markdown) < 100:
        raise ConversionError("テキストを抽出できませんでした")

    return ConvertResult(markdown=markdown, converter="raw", title=None)


def convert_url(url: str, timeout: int = 30) -> ConvertResult:
    """URL から Markdown に変換。

    http/https のみをサポート。サイズ上限 20MB。

    Args:
        url: 変換対象 URL。
        timeout: urllib タイムアウト秒数。

    Returns:
        ConvertResult

    Raises:
        ConversionError: スキーム不正、サイズ超過、内容不十分の場合。
    """
    # スキーム検証
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConversionError(
            f"スキーム {parsed.scheme} は対応していません。http:// または https:// のみを対応します。"
        )

    # Content-Length をチェック（20MB 上限）
    MAX_SIZE = 20 * 1024 * 1024
    # User-Agent 未設定だと 403 を返すサイトがあるため明示的に設定する。
    request = urllib.request.Request(
        url, headers={"User-Agent": "shelf/0.1 (personal knowledge tool)"}
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_SIZE:
            raise ConversionError("ファイルサイズが 20MB を超えています")

        # Content-Length ヘッダは信頼できない・欠落し得るため、実読み込みでも上限を
        # 課す。read() に上限を渡さず全量読み込んでから len() で判定すると、20MB超の
        # レスポンスを実際に最後までダウンロードしてしまい、打ち切りの意味がない。
        # read(MAX_SIZE + 1) で上限+1バイトに制限して読み、それを超えていれば
        # 早期に打ち切って拒否する。
        data = response.read(MAX_SIZE + 1)
        if len(data) > MAX_SIZE:
            raise ConversionError("ファイルサイズが 20MB を超えています")
    except urllib.error.URLError as e:
        raise ConversionError(f"URL の取得に失敗しました: {e}")

    # 一時ファイルに書き込み→ markitdown で変換
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="wb"
    ) as tmpfile:
        tmpfile.write(data)
        tmpfile_path = Path(tmpfile.name)

    try:
        return _convert_markitdown(tmpfile_path)
    finally:
        tmpfile_path.unlink(missing_ok=True)
