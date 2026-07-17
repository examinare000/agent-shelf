"""ShelfService: MCP(server.py)/CLI(cli.py) 共通のユースケース束ね層。

なぜこの層を独立させるか: server.py（FastMCP の ask/list_notebooks 2 ツール）と
cli.py（serve/ls/new/add/rm/index/ask）はどちらも同じ業務ロジックを呼ぶ薄いラッパに
留めたい。ask フローの grounding 判定・citation 整形・エラーの安全な要約化という
本質的な複雑さをここに集約することで、両エントリポイントは配線だけの責務になる
（recall/recall/service.py と同型の「2段構え」パターン）。

Store・embedder・backend_factory・converter・mask はすべてコンストラクタで注入される
ポート/ダブルであり、このモジュール自身は sqlite3/subprocess/fastembed/外部変換ライブラリを
一切 import しない（design doc §3, §6 の import ガード方針）。
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from shelf import convert as _default_converter
from shelf.convert import ConversionError, pick_converter
from shelf.digests import (
    MAP_SCHEMA,
    REDUCE_SCHEMA,
    build_map_prompt,
    build_reduce_prompt,
    group_into_windows,
    parse_map,
    parse_reduce,
)
from shelf.indexer import IndexStats, index_notebook
from shelf.librarian import Librarian
from shelf.names import doc_id_for, validate_notebook_name
from shelf.ports import (
    AnswerBackend,
    FileSummary,
    NotebookCard,
    RetrievedChunk,
    RouteTarget,
    StudyNote,
)
from shelf.prompts import (
    ANSWER_SCHEMA,
    SUMMARY_SCHEMA,
    build_ask_prompt,
    build_summary_prompt,
    parse_answer,
    parse_summary,
)
from shelf.search import cosine_topk
from shelf.shelver import Shelver
from shelf.store import Store, UnknownNotebookError

_logger = logging.getLogger(__name__)

# citation の quote は「裏取りに足る最小抜粋」であり全文ではない（design doc §4-A）。
QUOTE_MAX_LEN = 200

# study_notes.pipeline の現行版数。旧=1（先頭4000字を1回のLLM呼び出しで要約する
# 単発生成）、新=2（body チャンク列→ウィンドウ分割→ウィンドウごとに map 抽出→
# 文書全体で reduce 統合＋タグ付与、というチャンク接地付きパイプライン）。
# ShelfService._digest_one の skip 判定・移行ロジックが参照する（該当メソッド参照）。
DIGEST_PIPELINE_VERSION = 2

# 要約生成失敗時の決定的フォールバック（分類用テキスト）が拾う markdown 先頭の文字数
# （design doc §13.8: 「title + markdown 先頭 ~500字」）。
_SHELVE_SUMMARY_FALLBACK_EXCERPT_LEN = 500

_SHELVE_DIGEST_RECOMMENDATION = (
    "学びノートは自動生成されません。`shelf digest <notebook>` の実行を検討してください。"
)


@dataclass(frozen=True)
class IngestResult:
    """_ingest_file の戻り値。doc_id に加え、converter からの利用者向け通知
    （例: OCRスキップ）を notes として運ぶ。notes は既定で空タプル（該当なし）。
    """

    doc_id: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpertAnswer:
    """_answer_with_expert の戻り値。ask()/consult() の両方が共有する専門家推論の結果。

    ok=False は「backend 呼び出し自体が失敗した」(RawAnswer.ok=False)ことを示す。
    ask() はこの場合だけ従来互換の {"error": ...} 形へ変換し、それ以外(空notebook・
    パース失敗・成功)は grounded/answer/citations/insights を持つ同一形状で返す
    (設計書 §5-B: ask と consult は共通コアを共有)。consult() は ok=False でも
    fan-out 中の1専門家の失敗で全体を止めず、grounded=False の劣化エントリとして
    集約する(add_directory の「1件失敗しても継続」と同じ流儀)。
    """

    ok: bool
    grounded: bool
    answer: str
    citations: list[dict]
    insights: list[dict]
    warning: str | None
    error: str | None = None


@dataclass(frozen=True)
class _ConvertedFile:
    """shelve フェーズ1で変換・要約済みの1ファイル分の中間表現（design doc §13.6 補足）。

    分類（Shelver.plan）はこの内容を知らない（origin+summaryテキストだけを
    FileSummary として渡す）。フェーズ2の永続化（_persist_converted 呼び出し）だけが
    markdown/title/converter/notes/summary を必要とするため、ドメイン境界（ports.py）
    を跨がないローカル DTO として service.py に閉じる。

    summary は要約生成が成功した場合のみ非 None（分類用テキストと同一値）。失敗時は
    None のまま保持し、_persist_converted へは description=None/source=None で渡す
    （§13.8: 「stored description は None」）。分類自体は失敗しても決定的フォールバック
    テキストで継続するため、FileSummary 側には別途フォールバック値を積む。
    """

    origin: str
    markdown: str
    title: str | None
    converter: str
    notes: tuple[str, ...]
    summary: str | None


def _shelve_fallback_classification_text(title: str | None, markdown: str) -> str:
    """要約生成失敗時の決定的フォールバック分類用テキスト（design doc §13.8）。

    best-effort な要約が失敗しても分類自体は継続させる必要がある（ファイルを
    失わない）ため、title + markdown 先頭 ~500字という決定的な代替テキストに
    落とす。stored description（永続化される要約）はこの関数の戻り値とは独立に
    None のまま保持される（呼び出し元 _summarize_for_shelve を参照）。
    """
    excerpt = markdown[:_SHELVE_SUMMARY_FALLBACK_EXCERPT_LEN]
    return f"{title}\n{excerpt}" if title else excerpt


def _is_url(origin: str) -> bool:
    return urlparse(origin).scheme in ("http", "https")


def _origin_type(origin: str) -> str:
    """documents.origin_type に記録する投入形式("pdf"/"docx"/"url" 等)を判定する純粋関数。"""
    if _is_url(origin):
        return "url"
    suffix = Path(origin).suffix.lstrip(".").lower()
    return suffix or "unknown"


def _stem_for(origin: str) -> str:
    """doc_id_for に渡す「ファイル名幹」。URL の場合は path 部分の stem を使う。"""
    if _is_url(origin):
        return Path(urlparse(origin).path).stem or "doc"
    return Path(origin).stem


def _validate_file_origin(origin: str) -> dict | None:
    """ファイル系 origin をパス/サイズ観点で検証する（design doc §7、中位指摘#5）。

    is_symlink() は元のパス（resolve 前）に対して判定する必要がある。resolve() は
    シンボリックリンクを実体パスへ解決してしまうため、resolve 後に判定すると
    「シンボリックリンクだった」という情報が失われ、常に False になってしまう。
    resolve 後の is_file() はディレクトリ・デバイスファイル等の特殊ファイルを
    自然に弾く（通常ファイルにのみ True を返す）ため、シンボリックリンク拒否と
    合わせて「シンボリックリンク・ディレクトリ・特殊ファイル拒否」を満たす。
    """
    raw_path = Path(origin)
    if raw_path.is_symlink():
        return {"error": f"シンボリックリンクは対応していません: {origin}"}

    resolved = raw_path.resolve()
    if not resolved.is_file():
        return {"error": f"ファイルが存在しないか、通常ファイルではありません: {origin}"}

    return None


class ShelfService:
    def __init__(
        self,
        store: Store,
        embedder,
        backend_factory: Callable[[str], AnswerBackend],
        corpus_dir: Path,
        *,
        default_backend: str = "codex",
        top_k: int = 10,
        deep_dive: bool = False,
        mask: Callable[[str], str] | None = None,
        converter=None,
        router_backend: str = "",
        route_top_n: int = 1,
        route_fallback: str = "",
        digest_max_notes: int = 20,
        digest_map_notes: int = 5,
        digest_map_window_chars: int = 8000,
        digest_backend: str = "",
        librarian: Librarian | None = None,
        shelve_backend: str = "ollama",
        shelver: Shelver | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._backend_factory = backend_factory
        self._corpus_dir = corpus_dir
        self._default_backend = default_backend
        self._top_k = top_k
        self._deep_dive = deep_dive
        self._mask = mask
        # converter 省略時は実変換器（shelf.convert モジュール）を使う。モジュールは
        # convert_file/convert_url を関数として持つため、そのまま「convert_file/
        # convert_url を持つオブジェクト」として振る舞う（cli.py 側の配線を省力化する）。
        self._converter = converter if converter is not None else _default_converter
        # 司書(Librarian)関連の設定値。config.py の SHELF_ROUTER_BACKEND/SHELF_ROUTE_TOP_N/
        # SHELF_ROUTE_FALLBACK と対になるが、service.py は config を import せず
        # （既存の default_backend/top_k と同じ流儀）呼び出し側(cli.py/server.py。
        # R9/R10の担当)がこれらの値を明示的に渡す前提のコンストラクタ引数に留める。
        self._router_backend = router_backend
        self._route_top_n = route_top_n
        self._route_fallback = route_fallback
        # config.DIGEST_MAX_NOTES(env SHELF_DIGEST_MAX_NOTES)。reduce フェーズ後・
        # 文書全体で保持する学びノート数の上限（digests.build_reduce_prompt/parse_reduce
        # へ渡す）。既定 20 は digests.py の同関数群のローカル既定値と同値にして矛盾を避ける。
        self._digest_max_notes = digest_max_notes
        # config.DIGEST_MAP_NOTES(env SHELF_DIGEST_MAP_NOTES)。map フェーズで
        # 1 ウィンドウあたり抽出する学びノート数の上限（digests.build_map_prompt/
        # parse_map へ渡す）。digest_max_notes とは独立した控えめな既定値。
        self._digest_map_notes = digest_map_notes
        # config.DIGEST_MAP_WINDOW_CHARS(env SHELF_DIGEST_MAP_WINDOW_CHARS)。
        # digests.group_into_windows(..., window_chars=...) へ渡すウィンドウ文字数上限。
        self._digest_map_window_chars = digest_map_window_chars
        # config.DIGEST_BACKEND(env SHELF_DIGEST_BACKEND)。空文字列(既定)=未指定なら
        # notebook 自体の backend にフォールバックする(router_backend と同じ流儀。
        # digest() 内で `self._digest_backend or nb["backend"] or self._default_backend`
        # として解決する)。
        self._digest_backend = digest_backend
        # 注入されなければ consult() 初回呼び出し時に backend_factory から遅延構築する
        # （_get_librarian）。テストは FakeLibrarian や FakeAnswerBackend 経由でここへ
        # 差し込める（設計書 §9-B）。
        self._librarian = librarian
        # shelve() 専用の推論バックエンド名（config.SHELVE_BACKEND 由来・既定 "ollama"）。
        # service.py は config を import しない既存流儀（default_backend と同じ）に
        # 揃え、呼び出し側（cli.py。V8 の担当）が明示的に値を渡す前提のコンストラクタ
        # 引数に留める（design doc §13.10 V7 申し送り）。要約・分類・新規notebookの
        # backend列のすべてがこの1値に倒れる（§13.1 決定6）。
        self._shelve_backend = shelve_backend
        # 注入されなければ shelve() 初回呼び出し時に backend_factory から遅延構築し、
        # 以後キャッシュする（_get_shelver・_get_librarian と同型のパターン）。
        self._shelver = shelver

    # -- notebook 名検証（共通ヘルパ） -----------------------------------------

    def _validate_notebook_name_or_error(self, notebook: str) -> dict | None:
        """notebook 名の構文検証を行い、不正なら安全な error dict を返す。

        add_source/ask の入口で最初に通す。ここを通さず `corpus_dir / notebook` を
        構築すると、"../../tmp/x" のような文字列でパストラバーサル書き込みが成立して
        しまう(重大指摘#1)。validate_notebook_name は ValueError を送出するので、
        ここで catch して他の error dict と同じ形に揃える。
        """
        try:
            validate_notebook_name(notebook)
        except ValueError as exc:
            return {"error": str(exc)}
        return None

    def _unknown_notebook_error(self, notebook: str) -> dict:
        available = [row["name"] for row in self._store.list_notebooks()]
        return {"error": f"unknown notebook: {notebook}. available: {available}"}

    # -- ask -----------------------------------------------------------------

    def ask(self, notebook: str, question: str) -> dict:
        name_error = self._validate_notebook_name_or_error(notebook)
        if name_error is not None:
            return name_error

        nb = self._store.get_notebook(notebook)
        if nb is None:
            return self._unknown_notebook_error(notebook)

        backend_name = nb["backend"] or self._default_backend
        persona = nb["persona"]

        expert = self._answer_with_expert(notebook, question, persona, backend_name)
        if not expert.ok:
            # プロンプトや出力全文を含めない安全な要約のみを返す（RawAnswer.error は
            # engines/*.py の時点で既に安全な文言に整形済み）。既存 ask() の互換形状
            # ({"error": ..., "notebook": ...}) をそのまま維持する（設計書 §5-B）。
            return {"error": f"backend failed: {expert.error}", "notebook": notebook}

        return {
            "notebook": notebook,
            "backend": backend_name,
            "grounded": expert.grounded,
            "answer": expert.answer,
            "citations": expert.citations,
            # insights は additive なキー追加（design §5-B・§5-C）。persona なし・
            # digest チャンク未整備の notebook では常に [] になり、既存 ask() の
            # 呼び出し元がキーを無視する限り従来挙動と等価。
            "insights": expert.insights,
            "warning": expert.warning,
        }

    def _answer_with_expert(
        self, notebook: str, question: str, persona: str | None, backend_name: str
    ) -> ExpertAnswer:
        """ask()/consult() が共有する専門家推論の中核（設計書 §5-B「共通コア」）。

        retrieval→prompt 構成→backend 呼び出し→パース→grounding 判定という一連の
        処理は ask/consult で完全に同一であり、notebook・question(consult の場合は
        司書の subquery)・persona・backend_name だけが呼び出しごとに異なる。
        """
        ids, matrix = self._store.load_vectors(notebook)
        if matrix.shape[0] == 0:
            # チャンク0件ならバックエンドを呼ぶ意味がない（課金・レイテンシの無駄）ので
            # ここで早期リターンする。
            return ExpertAnswer(
                ok=True, grounded=False, answer="", citations=[], insights=[],
                warning="notebook has no indexed sources",
            )

        query_vec = self._embedder.embed_query(question)
        scored = cosine_topk(matrix, ids, query_vec, self._top_k)
        chunks = self._load_chunks(scored)
        # S番号(citations)/L番号(insights)の付番は prompts.build_ask_prompt が内部で
        # 使う分割規則(kind!='digest'→citation、kind=='digest'→insight)と完全一致
        # させる必要がある。番号がズレると citation_ids/insight_ids が誤ったチャンクを
        # 指してしまうため、ここで同一の分割を複製する（prompts.py は R5 完了済みの
        # 部品モジュールのため本タスク(R8)では編集しない）。
        citation_chunks, insight_chunks = self._split_chunks_by_kind(chunks)

        prompt = build_ask_prompt(question, chunks, self._deep_dive, persona=persona)
        backend = self._backend_factory(backend_name)
        raw = backend.answer(
            prompt, workdir=self._corpus_dir / notebook, schema=ANSWER_SCHEMA
        )
        if not raw.ok:
            return ExpertAnswer(
                ok=False, grounded=False, answer="", citations=[], insights=[],
                warning=None, error=raw.error,
            )

        parsed = parse_answer(raw.text)
        if not parsed.parse_ok:
            # パース失敗はエラーで潰さず、生テキスト+warning付きの劣化返却にする
            # （design doc §2 手順5）。
            return ExpertAnswer(
                ok=True, grounded=False, answer=parsed.answer, citations=[], insights=[],
                warning="engine output was not valid JSON",
            )

        grounded = parsed.confident and self._citations_in_range(
            parsed.citation_ids, len(citation_chunks)
        )
        return ExpertAnswer(
            ok=True,
            grounded=grounded,
            answer=parsed.answer,
            citations=self._build_citations(parsed.citation_ids, citation_chunks),
            insights=self._build_insights(parsed.insight_ids, insight_chunks),
            warning=None,
        )

    def _load_chunks(self, scored) -> list[RetrievedChunk]:
        chunks: list[RetrievedChunk] = []
        for item in scored:
            row = self._store.get_chunk(item.id)
            if row is None:  # 検索後に削除された等のレースは無視して結果から除く
                continue
            chunks.append(
                RetrievedChunk(
                    id=row["id"],
                    doc_id=row["doc_id"],
                    source_path=row["source_path"],
                    section=row["section"],
                    page=row["page"],
                    text=row["text"],
                    kind=row["kind"],
                )
            )
        return chunks

    @staticmethod
    def _split_chunks_by_kind(
        chunks: list[RetrievedChunk],
    ) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
        """retrieved チャンクを citation 用(body/summary)と insight 用(digest)に分ける。

        prompts.build_ask_prompt 内部の同名フィルタと同一規則でなければならない
        （S/L 番号のズレ防止。理由は _answer_with_expert のコメントを参照）。
        """
        citation_chunks = [c for c in chunks if c.kind != "digest"]
        insight_chunks = [c for c in chunks if c.kind == "digest"]
        return citation_chunks, insight_chunks

    @staticmethod
    def _citations_in_range(citation_ids: list[int], chunk_count: int) -> bool:
        """grounded 判定用: citation_ids が1件以上あり、かつ全てが 1..chunk_count 内か。"""
        return bool(citation_ids) and all(1 <= s <= chunk_count for s in citation_ids)

    @staticmethod
    def _build_citations(
        citation_ids: list[int], chunks: list[RetrievedChunk]
    ) -> list[dict]:
        """範囲外の S番号は無視し、同一 (source, page) の重複は除去して整形する。

        grounded 判定（_citations_in_range）とは独立に、範囲内citationだけで
        citations を構成する。範囲外citationが1つでも混じれば grounded=False には
        なるが、有効な引用まで消す理由はないため。
        """
        citations: list[dict] = []
        seen_source_page: set[tuple[str, int | None]] = set()
        for s in citation_ids:
            if not (1 <= s <= len(chunks)):
                continue
            chunk = chunks[s - 1]
            key = (chunk.source_path, chunk.page)
            if key in seen_source_page:
                continue
            seen_source_page.add(key)
            citations.append(
                {
                    "n": s,
                    "chunk_id": chunk.id,
                    "source": chunk.source_path,
                    "section": chunk.section,
                    "page": chunk.page,
                    "quote": chunk.text[:QUOTE_MAX_LEN],
                }
            )
        return citations

    @staticmethod
    def _build_insights(
        insight_ids: list[int], insight_chunks: list[RetrievedChunk]
    ) -> list[dict]:
        """L番号(insight_ids)を retrieved された digest チャンクの学びに変換する。

        _build_citations と対称の構造だが、(source, page) 重複除去はしない: 学びノートは
        同一資料から複数件が独立した価値を持つため（citations の「同一箇所の重複引用を
        1件にまとめる」判断とは意図が異なる）。note_id は chunks テーブルの id
        （例 "nb/doc#-2"）をそのまま使う。study_notes.id の "#d{n}" 形式ではないが、
        indexer.py/ports.py を編集できない本タスク(R8)の範囲では retrieved チャンクの
        id が学びノートを一意に指す唯一の値であり、これで足りる（R9/R10 への申し送り
        事項として完了報告に明記）。
        """
        insights: list[dict] = []
        for l in insight_ids:  # noqa: E741 - 設計書 §5-C の "l" 番号をそのまま踏襲
            if not (1 <= l <= len(insight_chunks)):
                continue
            chunk = insight_chunks[l - 1]
            insights.append(
                {
                    "l": l,
                    "note_id": chunk.id,
                    "source": chunk.source_path,
                    "text": chunk.text[:QUOTE_MAX_LEN],
                }
            )
        return insights

    # -- set_persona（設計書 §7-A） ---------------------------------------------

    def set_persona(self, notebook: str, persona: str | None) -> None:
        """notebook の専門家ペルソナを設定/更新する。

        create_notebook/index と同じ「不正な notebook 名/存在しない notebook は
        例外で通知する」流儀に揃える（ask/add_source のような dict-error 変換は
        しない）。persona は system prompt として backend へ送信されるため、
        codex/gemini/agy 時のクラウド送信を含めて「backend 送信テキストは全て
        mask 済み」という不変条件を保つ（設計書 §7-A）。
        """
        validate_notebook_name(notebook)
        masked = (
            self._mask(persona) if persona is not None and self._mask is not None else persona
        )
        self._store.set_persona(notebook, masked)

    # -- list_notebooks --------------------------------------------------------

    def list_notebooks(self) -> list[dict]:
        return [
            {
                "notebook": row["name"],
                "description": row["description"],
                "backend": row["backend"],
                "sources": row["documents"],
                "chunks": row["chunks"],
            }
            for row in self._store.list_notebooks()
        ]

    # -- create_notebook ---------------------------------------------------------

    def create_notebook(
        self, name: str, description: str | None = None, backend: str | None = None
    ) -> None:
        # 名前検証・backend解決可能性の検証は、どちらも store への書き込み前に行う
        # （不正な状態を catalog に残さないため）。
        validate_notebook_name(name)
        if backend is not None:
            self._backend_factory(backend)
        self._store.create_notebook(name, description=description, backend=backend)

    # -- description（要約）自動生成 -----------------------------------------

    def _resolve_description(
        self,
        *,
        notebook: str,
        doc_id: str,
        markdown: str,
        title: str | None,
        description: str | None,
        auto_summary: bool,
        backend_name: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """(description, description_source, note) を優先順位に従って決定する。

        優先順位: 1) 明示 description → mask 適用のうえ source='user'。
        2) auto_summary=True → notebook の backend で要約生成し、成功すれば
        mask 適用のうえ source='auto'。3) 生成失敗（ok=False/parse失敗/例外）→
        既存 description/description_source があれば維持、無ければ (None, None)。
        4) auto_summary=False で明示もなし → 既存を維持（backend は呼ばない）。
        note は失敗時にのみ非 None を返し、呼び出し元が IngestResult.notes に積む。
        """
        explicit = description.strip() if description is not None else ""
        if explicit:
            masked = self._mask(explicit) if self._mask is not None else explicit
            return masked, "user", None

        existing = self._store.get_document(doc_id)
        existing_description = existing["description"] if existing is not None else None
        existing_source = existing["description_source"] if existing is not None else None

        if not auto_summary:
            return existing_description, existing_source, None

        # _backend_factory（backend設定不備等）も含めて except Exception で囲む。
        # これは意図的な設計であり隠蔽ではない: add の主目的は資料投入そのものであり、
        # 要約生成は best-effort。notebook 側の backend 設定不備程度で投入自体を
        # 失敗させたくないため、生成失敗は notes で利用者に可視化した上で継続する。
        summary: str | None = None
        try:
            backend = self._backend_factory(backend_name or self._default_backend)
            raw = backend.answer(
                build_summary_prompt(markdown, title=title),
                workdir=self._corpus_dir / notebook,
                schema=SUMMARY_SCHEMA,
            )
            if raw.ok:
                summary = parse_summary(raw.text)
        except Exception:
            summary = None

        if summary is not None:
            masked = self._mask(summary) if self._mask is not None else summary
            return masked, "auto", None

        if existing_description is not None:
            return (
                existing_description,
                existing_source,
                "要約の再生成に失敗したため既存の説明を維持しました",
            )
        return None, None, "要約生成に失敗しました"

    # -- add_source ----------------------------------------------------------

    def _ingest_file(
        self,
        notebook: str,
        origin: str,
        *,
        is_url: bool = False,
        description: str | None = None,
        auto_summary: bool = True,
        backend_name: str | None = None,
    ) -> IngestResult:
        """変換→mask→description解決を行い、永続化は `_persist_converted` に委ねる。

        add_source（単一ファイル/URL）と add_directory（複数ファイル一括）の共通部分。
        notebook 名検証・索引化（index_notebook）はここに含めない: 前者は呼び出し元が
        入口で1回だけ行うべき検証であり、後者は複数ファイルをまとめて処理した後に
        1回だけ実行すべきだから（呼び出し毎に索引を回すと add_directory が
        ファイル数分だけ索引を再構築してしまう）。ConversionError はそのまま送出し、
        呼び出し元がそれぞれの流儀（add_source は即時 error dict 化、add_directory は
        継続して errors に記録）で処理する。
        """
        # ConversionError は convert.py 側で既に安全な文言に整形済み（スタックトレース・
        # パス全体を含まない）。呼び出し元ごとの処理方針に委ねるため、ここでは
        # try/except で捕まえず加工なしに伝播させる。
        if is_url:
            result = self._converter.convert_url(origin)
        else:
            result = self._converter.convert_file(Path(origin))

        # mask はチャンク時（chunker.chunk_markdown）だけでなく、corpus に永続化する
        # markdown 自体にも適用する（中位指摘#2）。適用済みでなければ DEEP_DIVE 等で
        # チャンク境界の外側からファイルを直接読まれた場合にマスク境界が無効化される。
        # マスクは正規表現ベースの置換であり冪等（一度置換された文字列が再度マッチする
        # ことはない）なので、チャンク時に再度 mask() を通しても二重適用は安全。
        markdown = self._mask(result.markdown) if self._mask is not None else result.markdown

        # description 解決には doc_id が要る（既存 description を store.get_document(doc_id)
        # で引き当てるため）。doc_id_for は notebook+origin+stem のみに依存する純粋関数
        # なので、ここで計算しても _persist_converted 側の再計算と食い違うことはない
        # （同一入力→同一出力。doc_id 生成則そのものはこの1関数にしか実装されない）。
        doc_id = doc_id_for(f"{notebook}:{origin}", _stem_for(origin))
        doc_description, doc_description_source, description_note = self._resolve_description(
            notebook=notebook,
            doc_id=doc_id,
            markdown=markdown,
            title=result.title,
            description=description,
            auto_summary=auto_summary,
            backend_name=backend_name,
        )
        notes = result.notes
        if description_note is not None:
            notes = (*notes, description_note)

        return self._persist_converted(
            notebook,
            origin,
            is_url=is_url,
            markdown=markdown,
            title=result.title,
            converter=result.converter,
            conversion_notes=notes,
            description=doc_description,
            description_source=doc_description_source,
        )

    def _persist_converted(
        self,
        notebook: str,
        origin: str,
        *,
        is_url: bool,
        markdown: str,
        title: str | None,
        converter: str,
        conversion_notes: tuple[str, ...],
        description: str | None,
        description_source: str | None,
    ) -> IngestResult:
        """doc_id採番→corpus書き出し→upsert_document のみを行う「永続化半分」。

        design doc §13.10 V4: `_ingest_file` から抽出した extract-method。変換
        （convert_file/convert_url）も要約生成（backend 呼び出し）もここでは行わない
        （責務を永続化のみに絞る）。将来 shelve が「既に変換・要約済みの markdown と
        description」を直接渡して呼べるよう、auto_summary/backend_name のような
        要約系引数を持たないシグネチャにしてある。doc_id/corpus書き出し/upsert を
        1関数にまとめることで、これらの単一真実源を保つ（重複コピーは doc_id 生成則の
        分岐という将来事故源になる・§13.11）。
        """
        doc_id = doc_id_for(f"{notebook}:{origin}", _stem_for(origin))
        normalized_path = f"{notebook}/{doc_id}.md"
        dest = self._corpus_dir / notebook / f"{doc_id}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(markdown, encoding="utf-8")

        now = datetime.now(UTC).isoformat()
        self._store.upsert_document(
            id=doc_id,
            notebook=notebook,
            origin=origin,
            origin_type=_origin_type(origin),
            normalized_path=normalized_path,
            converter=converter,
            added_at=now,
            title=title,
            fetched_at=now if is_url else None,
            description=description,
            description_source=description_source,
        )
        return IngestResult(doc_id=doc_id, notes=conversion_notes)

    def add_source(
        self,
        notebook: str,
        origin: str,
        *,
        description: str | None = None,
        auto_summary: bool = True,
    ) -> dict:
        # notebook 名の構文検証・存在確認は、変換やファイル書き込みより必ず先に行う
        # （重大指摘#1）。この2検証を通す前に corpus へ書き込むと、未検証文字列が
        # そのままパスに使われてパストラバーサル書き込みが成立してしまう。
        name_error = self._validate_notebook_name_or_error(notebook)
        if name_error is not None:
            return name_error
        nb = self._store.get_notebook(notebook)
        if nb is None:
            return self._unknown_notebook_error(notebook)
        backend_name = nb["backend"] or self._default_backend

        is_url = _is_url(origin)
        if not is_url:
            # ディレクトリ origin は add_directory へ委譲する。symlink はここで
            # is_symlink() を先に見て弾く（resolve 前の判定が必要な理由は
            # _validate_file_origin の docstring と同じ）: symlink がディレクトリを
            # 指していても dir 分岐に入れてしまうと、シンボリックリンク拒否を
            # すり抜けて再帰走査に迷い込む恐れがあるため、意図的にディスパッチしない。
            raw = Path(origin)
            if not raw.is_symlink() and raw.is_dir():
                # --desc はファイル単位の指定を意図しており、ディレクトリ一括投入では
                # どのファイルの説明かが定まらない。converter を呼ぶ前（副作用ゼロ）に
                # 拒否する。
                if description is not None and description.strip():
                    return {
                        "error": "--desc はディレクトリ投入では指定できません"
                        "（ファイル単位で指定してください）"
                    }
                return self.add_directory(notebook, origin, auto_summary=auto_summary)
            # URL 系 origin は convert_url 側の Content-Length/打ち切り読みでサイズを
            # 制御するため対象外（design doc §7 はファイル系 origin の規定）。
            file_error = _validate_file_origin(origin)
            if file_error is not None:
                return file_error
            # add_directory（resolve 済み絶対パスで origin 記録）と表記を揃える。
            # ここで resolve しないと、相対パスで add した場合と親ディレクトリを
            # 一括 add した場合とで同一物理ファイルの origin 文字列が食い違い、
            # doc_id_for の入力が分かれて documents に二重登録されてしまう。
            # symlink 判定は resolve 前の raw/_validate_file_origin 側で既に
            # 完了しているため、ここでの resolve は安全。
            origin = str(Path(origin).resolve())

        try:
            ingest_result = self._ingest_file(
                notebook,
                origin,
                is_url=is_url,
                description=description,
                auto_summary=auto_summary,
                backend_name=backend_name,
            )
        except ConversionError as exc:
            return {"error": str(exc)}

        stats = index_notebook(
            self._corpus_dir, notebook, self._store, self._embedder, mask=self._mask
        )
        response = {
            "doc_id": ingest_result.doc_id,
            "notebook": notebook,
            "chunks_written": stats.chunks_written,
        }
        if ingest_result.notes:
            # notes が空のときはキー自体を付けない(JSONノイズを避ける)。
            response["notes"] = list(ingest_result.notes)
        return response

    # -- add_directory ---------------------------------------------------------

    @staticmethod
    def _iter_directory_candidates(root: Path, skipped: list[dict]) -> Iterator[Path]:
        """root 配下を再帰走査し、対応形式の通常ファイルの Path だけを yield する。

        add_directory と shelve（設計書 §13.2 手順1「add_directory と同一規則」）が
        共有するスキャン規則: 隠しファイル/ディレクトリ（root からの相対パス構成要素の
        いずれかが "." 始まり）は記録すらせず黙って除外し、symlink・未対応形式は
        呼び出し元が渡す skipped リストへ記録したうえで除外する。rglob はディレクトリ
        シンボリックリンクを辿らない（Python 3.11+）ため、tree 外へ迷い出る走査
        ループの心配なくそのまま使える。root は呼び出し元が resolve 済みである前提
        （yield する Path も resolve 済み絶対パスの子孫になる）。
        """
        for path in sorted(root.rglob("*")):
            rel_parts = path.relative_to(root).parts
            if any(part.startswith(".") for part in rel_parts):
                continue

            if path.is_symlink():
                # ファイル/ディレクトリいずれを指す symlink も一律 skip。dir を指す
                # symlink は rglob が配下を辿らないため、ここで一度だけ記録すれば十分。
                skipped.append(
                    {"origin": str(path), "reason": "シンボリックリンクは対応していません"}
                )
                continue

            if path.is_dir():
                continue  # 通常ディレクトリ自体は無視。配下は rglob が別途列挙する。

            try:
                pick_converter(str(path))
            except ConversionError:
                skipped.append({"origin": str(path), "reason": "未対応の形式です"})
                continue

            yield path

    def add_directory(
        self, notebook: str, dir_path: str, *, auto_summary: bool = True
    ) -> dict:
        """ディレクトリ配下を再帰走査し、対応形式ファイルを一括投入する。

        add_source からのディスパッチに加え、直接呼び出しにも対応するため、入口で
        notebook 名検証・存在確認を独自に行う（直接呼び出しへの防御）。

        description は引数を持たず（ファイル単位の指定は add_source 側で拒否済み）、
        ファイルごとに auto_summary の方針に従う。
        """
        name_error = self._validate_notebook_name_or_error(notebook)
        if name_error is not None:
            return name_error
        nb = self._store.get_notebook(notebook)
        if nb is None:
            return self._unknown_notebook_error(notebook)
        backend_name = nb["backend"] or self._default_backend

        # resolve してから走査することで、配下ファイルの origin も絶対パスで記録
        # される（"./docs" と絶対パスの再実行で同一 doc_id → upsert 冪等になる）。
        root = Path(dir_path).resolve()
        added: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []

        for path in self._iter_directory_candidates(root, skipped):
            origin = str(path)
            try:
                ingest_result = self._ingest_file(
                    notebook,
                    origin,
                    is_url=False,
                    auto_summary=auto_summary,
                    backend_name=backend_name,
                )
            except ConversionError as exc:
                # ConversionError は既に安全な文言に整形済み（convert.py 側で内部詳細を含まない）。
                # 単一ファイルなら add_source は即時 error dict を返すが、ここでは
                # 1ファイルの変換失敗で全体を止めず、errors に記録して継続する。
                errors.append({"origin": origin, "error": str(exc)})
                continue
            except OSError:
                # ファイル読み取り不可（権限不足・削除・マウント解除等）な OS レベル例外も
                # 継続させる。内部詳細（スタックトレース・エラーメッセージ）は出さない
                # （design doc 「内部詳細を漏らさない」方針）。
                errors.append({"origin": origin, "error": "ファイルを読み取れませんでした"})
                continue
            added_entry: dict[str, object] = {
                "doc_id": ingest_result.doc_id,
                "origin": origin,
            }
            if ingest_result.notes:
                # add_source と同様、notes は非空のときだけエントリに付与する。
                added_entry["notes"] = list(ingest_result.notes)
            added.append(added_entry)

        chunks_written = 0
        if added:
            # ファイル数分ではなく1回だけ索引化する。add_source と異なり複数ファイルを
            # まとめて処理するため、逐次索引化は無駄な再構築になる。
            stats = index_notebook(
                self._corpus_dir, notebook, self._store, self._embedder, mask=self._mask
            )
            chunks_written = stats.chunks_written

        if not added and not errors:
            return {"error": f"投入対象のファイルが見つかりませんでした: {root}"}

        return {
            "notebook": notebook,
            "added": added,
            "skipped": skipped,
            "errors": errors,
            "chunks_written": chunks_written,
        }

    # -- shelve（自動分類投入・設計書 §13） -------------------------------------

    def _get_shelver(self) -> Shelver:
        """注入された Shelver があればそれを使い、無ければ backend_factory から
        遅延構築して以後キャッシュする（_get_librarian と同型・設計書 §13.3）。
        Shelver は corpus_dir 直下を workdir として使う（分類時点ではまだ投入先
        notebook が確定していないため、notebook 別サブディレクトリを持てない）。
        """
        if self._shelver is None:
            backend = self._backend_factory(self._shelve_backend)
            self._shelver = Shelver(
                backend, workdir=self._corpus_dir, notebook_backend=self._shelve_backend
            )
        return self._shelver

    def _summarize_for_shelve(
        self, markdown: str, title: str | None, backend: AnswerBackend
    ) -> tuple[str, str | None]:
        """(分類用テキスト, 永続化用description) を返す（設計書 §13.2/§13.8）。

        既存 description 自動生成パス（build_summary_prompt/SUMMARY_SCHEMA/
        parse_summary）をそのまま再利用する（§13.1 決定5: 分類専用の別要約経路は
        作らない）。成功時は両者とも同じ要約テキストを共有する。失敗（例外・
        ok=False・パース失敗）時は分類用テキストを決定的フォールバックへ落とし、
        永続化用 description は None のまま返す（§13.8: 「stored description は
        None（既存 best-effort と同じ・source も None）」・分類はフォールバック
        テキストで継続しファイルを失わない）。
        """
        try:
            raw = backend.answer(
                build_summary_prompt(markdown, title=title),
                workdir=self._corpus_dir,
                schema=SUMMARY_SCHEMA,
            )
            if raw.ok:
                summary = parse_summary(raw.text)
                if summary is not None:
                    masked = self._mask(summary) if self._mask is not None else summary
                    return masked, masked
        except Exception:
            pass
        return _shelve_fallback_classification_text(title, markdown), None

    def _prepare_shelve_candidates(
        self, root: Path, summarize_backend: AnswerBackend
    ) -> tuple[list[_ConvertedFile], list[FileSummary], list[dict], list[dict]]:
        """フェーズ1 の scan→冪等スキップ→convert→mask→summarize を行う。

        add_directory と同一のスキャン規則（隠し/symlink除外・対応形式・resolve済み
        絶対パス）を再利用する（設計書 §13.2 手順1）。分類（Shelver.plan）はここでは
        行わず、呼び出し元（shelve）が summaries をまとめて渡す。
        """
        converted: list[_ConvertedFile] = []
        summaries: list[FileSummary] = []
        skipped: list[dict] = []
        errors: list[dict] = []

        for path in self._iter_directory_candidates(root, skipped):
            origin = str(path)
            # 既に(いずれかのnotebookに)投入済みの origin は変換・要約・分類の
            # コストを払わずスキップする（設計書 §13.1 決定4/§13.7）。
            existing = self._store.find_documents_by_origin(origin)
            if existing:
                skipped.append(
                    {
                        "origin": origin,
                        "reason": f"既に notebook '{existing[0]['notebook']}' に投入済みです",
                    }
                )
                continue

            try:
                result = self._converter.convert_file(Path(origin))
            except ConversionError as exc:
                errors.append({"origin": origin, "error": str(exc)})
                continue
            except OSError:
                errors.append({"origin": origin, "error": "ファイルを読み取れませんでした"})
                continue

            markdown = (
                self._mask(result.markdown) if self._mask is not None else result.markdown
            )
            classification_text, description = self._summarize_for_shelve(
                markdown, result.title, summarize_backend
            )

            converted.append(
                _ConvertedFile(
                    origin=origin,
                    markdown=markdown,
                    title=result.title,
                    converter=result.converter,
                    notes=result.notes,
                    summary=description,
                )
            )
            summaries.append(FileSummary(origin=origin, summary=classification_text))

        return converted, summaries, skipped, errors

    def shelve(self, directory: str, *, dry_run: bool = False) -> dict:
        """ディレクトリ一括投入時に、モデルが1ファイルずつ既存/新規 notebook へ
        自動分類する（設計書 §13）。

        フェーズ1（scan→convert→summarize→classify）で計画（ShelvePlan）を組み立て、
        dry_run=True ならここで打ち切り永続副作用ゼロの計画のみを返す（§13.2 決定2）。
        dry_run=False ならフェーズ2（新notebook作成→_persist_converted→影響notebook
        毎に1回index）を実行する。digest は自動実行せず、notes で案内するのみ
        （§13.1 決定5）。MCP には公開しない（server.py 無変更・CLI 専用・V8 の担当）。
        """
        root = Path(directory).resolve()
        summarize_backend = self._backend_factory(self._shelve_backend)
        converted, summaries, skipped, errors = self._prepare_shelve_candidates(
            root, summarize_backend
        )

        catalog = self._build_catalog()
        plan = self._get_shelver().plan(summaries, catalog)
        errors = [*errors, *plan.errors]

        if dry_run:
            return {
                "directory": str(root),
                "dry_run": True,
                "plan": [
                    {
                        "origin": a.origin,
                        "notebook": a.notebook,
                        "new_notebook": a.new_notebook,
                        "summary": a.summary,
                        "reason": a.reason,
                    }
                    for a in plan.assignments
                ],
                "created_notebooks": [
                    {"notebook": c.name, "description": c.description, "backend": c.backend}
                    for c in plan.created
                ],
                "skipped": skipped,
                "errors": errors,
            }

        for spec in plan.created:
            self._store.create_notebook(
                spec.name, description=spec.description, backend=spec.backend
            )

        converted_by_origin = {c.origin: c for c in converted}
        added: list[dict] = []
        affected_notebooks: list[str] = []

        for assignment in plan.assignments:
            cf = converted_by_origin[assignment.origin]
            ingest_result = self._persist_converted(
                assignment.notebook,
                assignment.origin,
                is_url=False,
                markdown=cf.markdown,
                title=cf.title,
                converter=cf.converter,
                conversion_notes=cf.notes,
                description=cf.summary,
                description_source="auto" if cf.summary is not None else None,
            )
            added.append(
                {
                    "doc_id": ingest_result.doc_id,
                    "origin": assignment.origin,
                    "notebook": assignment.notebook,
                }
            )
            if assignment.notebook not in affected_notebooks:
                affected_notebooks.append(assignment.notebook)

        chunks_written = 0
        for notebook in affected_notebooks:
            stats = index_notebook(
                self._corpus_dir, notebook, self._store, self._embedder, mask=self._mask
            )
            chunks_written += stats.chunks_written

        return {
            "directory": str(root),
            "dry_run": False,
            "added": added,
            "created_notebooks": [c.name for c in plan.created],
            "skipped": skipped,
            "errors": errors,
            "chunks_written": chunks_written,
            "notes": [_SHELVE_DIGEST_RECOMMENDATION],
        }

    # -- index -----------------------------------------------------------------

    def index(self, notebook: str, full: bool = False) -> IndexStats:
        # add_source と同様、notebook 名の構文検証・存在確認を corpus_dir アクセスより
        # 先に行う（重大指摘#1）。index() の戻り値は IndexStats 固定のため、add_source/
        # ask のような error dict ではなく ValueError/UnknownNotebookError をそのまま
        # 送出する（create_notebook と同じ流儀）。
        validate_notebook_name(notebook)
        if self._store.get_notebook(notebook) is None:
            raise UnknownNotebookError(f"notebook '{notebook}' does not exist")
        return index_notebook(
            self._corpus_dir, notebook, self._store, self._embedder,
            mask=self._mask, full=full,
        )

    # -- consult（司書ルーティング入口・設計書 §5-A/§6） -------------------------

    def _build_catalog(self) -> list[NotebookCard]:
        """Librarian.route() に渡す投影 DTO を store.list_notebooks() から組み立てる。

        Librarian は store を一切知らない（設計書 §3「カタログは service が組み立てて
        Librarian に渡す」）ため、この変換は service の責務。
        """
        return [
            NotebookCard(
                name=row["name"],
                description=row["description"],
                persona=row["persona"],
                doc_count=row["documents"],
            )
            for row in self._store.list_notebooks()
        ]

    def _get_librarian(self) -> Librarian:
        """注入された Librarian があればそれを使い、無ければ backend_factory から
        遅延構築して以後キャッシュする。router_backend が未指定(空文字列)の場合は
        default_backend にフォールバックする（config.py の解決方式と同じ規約・
        設計書 §6-D）。ask() を主目的に ShelfService を構築する呼び出し元
        （router 未使用）で余計な backend_factory 呼び出しを起こさないよう、
        __init__ 時ではなく初回 consult() 呼び出し時まで構築を遅らせる。
        """
        if self._librarian is None:
            backend_name = self._router_backend or self._default_backend
            self._librarian = Librarian(
                self._backend_factory(backend_name),
                workdir=self._corpus_dir,
                top_n=self._route_top_n,
                fallback=self._route_fallback,
            )
        return self._librarian

    def consult(self, question: str) -> dict:
        """司書がルーティングし、選ばれた専門家が抜粋+学びを返す（設計書 §5-A）。

        catalog が空の場合は Librarian.route() 自体を呼ばずに短絡する（apply_fallback
        も同じ分岐で対象ゼロを返すが、catalog が空だと事前に分かっている以上、
        無駄な backend 呼び出しを避けるほうがレイテンシ・コスト面で望ましい）。
        route() が空リストを返す理由（カタログ空／answerable=false／パース失敗／
        backend失敗のいずれか）は routing.apply_fallback 内部で吸収され Librarian の
        外からは区別できないため、ここでは一律「資料からは分からない」型の返却
        （grounded=false・専門家を呼ばない）に倒す（部品からの申し送り事項）。
        """
        catalog = self._build_catalog()
        if not catalog:
            return {
                "question": question,
                "answered": False,
                "routed": [],
                "warning": "利用可能な notebook がありません",
            }

        librarian = self._get_librarian()
        targets = librarian.route(question, catalog)
        if not targets:
            return {
                "question": question,
                "answered": False,
                "routed": [],
                "warning": "資料からは分からない",
            }

        return {
            "question": question,
            "answered": True,
            "routed": [self._consult_target(target) for target in targets],
            "warning": None,
        }

    def _consult_target(self, target: RouteTarget) -> dict:
        """ルーティング対象 1 件に対して専門家推論を実行し、透明性情報と集約する。

        専門家の backend 呼び出しが失敗しても consult() 全体は止めない
        （add_directory の「1件の失敗で全体を止めない」流儀を踏襲）。この場合は
        grounded=False・空の answer/citations/insights を持つ劣化エントリとして
        routed[] に含める。
        """
        nb = self._store.get_notebook(target.notebook) or {}
        backend_name = nb.get("backend") or self._default_backend
        persona = nb.get("persona")

        expert = self._answer_with_expert(target.notebook, target.subquery, persona, backend_name)
        return {
            "notebook": target.notebook,
            "reason": target.reason,
            "subquery": target.subquery,
            "score": target.score,
            "backend": backend_name,
            "persona": persona,
            "grounded": expert.grounded if expert.ok else False,
            "answer": expert.answer if expert.ok else "",
            "citations": expert.citations if expert.ok else [],
            "insights": expert.insights if expert.ok else [],
        }

    # -- digest（学びノート生成・map-reduce パイプライン・設計書 §7-B） -------------

    def digest(self, notebook: str, doc_id: str | None = None, *, force: bool = False) -> dict:
        """資料を専門家ペルソナで消化し、学びノートを生成して study_notes に保存する。

        生成後は当該 notebook を再索引し、digest チャンクを検索対象に加える。
        indexer は study_notes の変更だけでは再索引をトリガーしない（mtime/size が
        変わらないため file_state の early-skip に吸われる）ので、study_notes を
        書いた直後にここで対象 doc の file_state を明示的に無効化してから
        index_notebook を呼ぶのが service.digest() の責務（部品からの申し送り#6。
        file_state 無効化の詳細な理由は下の for ループのコメントを参照）。

        陳腐化判定は documents.content_hash（現状 NULL のまま・§12-3 は本タスクの
        対象外と判断し見送った。完了報告に明記）ではなく、対象 doc の現在の
        markdown 本文から直接計算したハッシュと既存 study_notes.source_hash を
        比較する self-contained な方式にした。これにより _ingest_file（ingest
        経路）に触れずに陳腐化検出を成立させられる。
        """
        name_error = self._validate_notebook_name_or_error(notebook)
        if name_error is not None:
            return name_error
        nb = self._store.get_notebook(notebook)
        if nb is None:
            return self._unknown_notebook_error(notebook)

        # config.DIGEST_BACKEND（空文字列=未指定）が非空ならそれを優先し、
        # 空なら notebook 自体の backend（さらに空ならサービス全体既定）へフォールバック
        # する（router_backend と同じ「空=呼び出し側でフォールバック」流儀）。
        backend_name = self._digest_backend or nb["backend"] or self._default_backend
        persona = nb["persona"]

        if doc_id is not None:
            doc = self._store.get_document(doc_id)
            if doc is None or doc["notebook"] != notebook:
                return {"error": f"unknown document: {doc_id}"}
            targets = [doc]
        else:
            targets = self._store.list_documents(notebook)

        # map フェーズの入力は store.list_chunks(kind="body") が返す既索引済みチャンク
        # である。shelf add 直後（本文はまだ未索引）に digest を呼ぶ運用や、
        # 新規 notebook で add→digest の間に一度も index が走っていないケースでも
        # 必ずチャンクが存在する状態にするため、対象解決後・本処理前に notebook 単位の
        # 増分 index を1回実行しておく（brief §3: 「digest() 冒頭で notebook 単位の
        # 増分 index を1回実行する方式でよい」）。file_state が mtime/size 一致なら
        # indexer.py 側で early-skip されるため、既に索引済みのファイルへのコストは
        # ほぼゼロ。
        index_notebook(
            self._corpus_dir, notebook, self._store, self._embedder, mask=self._mask
        )

        generated: list[str] = []
        skipped: list[str] = []
        errors: list[dict] = []

        # notebook 横断 GROUP BY の list_tags_by_notebook はループ前に1回だけ引き、
        # _digest_one には共有ミュータブルリストとして渡す（コードレビュー指摘#3の
        # N+1解消。_digest_one 側が新規タグをその場で追記するため、後続文書の
        # reduce プロンプトに先行文書のタグが反映される実質挙動は変わらない）。
        tag_catalog = list(self._store.list_tags_by_notebook().get(notebook, ()))

        for doc in targets:
            outcome = self._digest_one(
                notebook, doc, persona, backend_name, force=force, tag_catalog=tag_catalog
            )
            if outcome == "skipped":
                skipped.append(doc["id"])
            elif outcome == "generated":
                generated.append(doc["id"])
            else:
                errors.append({"doc_id": doc["id"], "error": outcome})

        if generated:
            # study_notes を更新した doc の file_state を狙い撃ちで無効化してから
            # 再索引する。WHY: index_notebook の mtime/size 一致 early-skip
            # (indexer.py) は「ファイル自体は変わっていないが study_notes だけ
            # 変わった」ケースを検知できない。shelf add → shelf digest という実運用
            # 順序では add 時点で file_state が既に確定しているため、この early-skip
            # に吸われて kind='digest' チャンクが chunks に一度も書かれない
            # (Critical不具合)。file_state を削除しておけば次の index_notebook が
            # 当該ファイルだけを強制的に再チャンク・再埋め込みする。
            # full=True で notebook 全体を再構築する案(候補a)より対象を絞れるため、
            # 資料数に比例したコスト増を避けられる(候補bを採用した理由)。
            # 1資料ずつではなく、生成が1件でもあれば notebook 単位で1回だけ再索引する
            # （add_directory と同じ「ファイル数分ではなく1回」の流儀）。
            for doc_id in generated:
                path = self._corpus_dir / notebook / f"{doc_id}.md"
                source_path = str(path.relative_to(self._corpus_dir))
                self._store.delete_file_state(source_path)
            index_notebook(
                self._corpus_dir, notebook, self._store, self._embedder, mask=self._mask
            )

        return {
            "notebook": notebook,
            "generated": generated,
            "skipped": skipped,
            "errors": errors,
        }

    def _digest_one(
        self,
        notebook: str,
        doc: dict,
        persona: str | None,
        backend_name: str,
        *,
        force: bool,
        tag_catalog: list[str],
    ) -> str:
        """1資料分の学びノート生成（map-reduce パイプライン）。

        戻り値は "generated"/"skipped"/それ以外(エラー文言)。1資料の失敗で
        全体を止めず、呼び出し元 digest() が errors に記録して次の資料へ継続する
        （add_directory と同じ流儀・設計書 §4-C）。

        flow: body チャンク列(store.list_chunks) → group_into_windows → ウィンドウ
        ごとに map 抽出 → 文書全体で reduce 統合＋タグ付与 → mask 適用 →
        replace_study_notes/replace_document_tags。

        tag_catalog は digest() のループ前に1回だけ list_tags_by_notebook を
        引いた結果（呼び出し元と共有するミュータブルな list）。文書ごとに
        notebook 横断 GROUP BY を引き直す N+1（コードレビュー指摘#3）を避けつつ、
        「後続文書の reduce プロンプトに先行文書のタグが反映される」実質挙動は
        このメソッドが新規タグを都度 tag_catalog に追記することで保つ。
        """
        doc_id = doc["id"]
        path = self._corpus_dir / notebook / f"{doc_id}.md"
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            return "資料ファイルを読み取れませんでした"

        content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

        if not force:
            # skip 判定は「source_hash 一致」だけでなく「既存ノートが現行パイプライン
            # (pipeline=2)で生成済み」も条件にする。旧パイプライン(pipeline=1)の
            # ノートは本文が変わっていなくても新パイプラインへの移行対象として
            # 再生成する（--force なしで移行が進むようにするため・完了報告に明記）。
            existing_notes = self._store.list_study_notes(notebook, doc_id)
            if existing_notes and all(
                n["source_hash"] == content_hash
                and n["pipeline"] == DIGEST_PIPELINE_VERSION
                for n in existing_notes
            ):
                return "skipped"

        chunks = self._store.list_chunks(notebook, doc_id, kind="body")
        windows = group_into_windows(chunks, window_chars=self._digest_map_window_chars)

        backend = self._backend_factory(backend_name)
        title = doc.get("title")
        workdir = self._corpus_dir / notebook

        # map フェーズ: ウィンドウごとに学びを抽出する。1ウィンドウの失敗（backend 例外・
        # ok=False・parse失敗＝空リスト）は他のウィンドウを止めず継続する。全ウィンドウが
        # 失敗/ゼロ件（= 合計 map_notes が空）の場合のみ、この資料をエラー扱いにする
        # （brief §5「1ウィンドウの失敗は継続・全ウィンドウ全滅のみ doc エラー」）。
        # last_backend_error は observability のため、map/reduce 各フェーズで最後に
        # 観測した backend の raw.error 文言を保持する（コードレビュー指摘#5。
        # backend.answer() が例外を送出した経路は raw を得られないため対象外）。
        map_notes: list[StudyNote] = []
        last_backend_error: str | None = None
        for window in windows:
            map_prompt = build_map_prompt(
                window, persona=persona, title=title, max_notes=self._digest_map_notes
            )
            try:
                map_raw = backend.answer(map_prompt, workdir=workdir, schema=MAP_SCHEMA)
            except Exception:  # noqa: BLE001 - 1ウィンドウの例外で他ウィンドウを止めない
                continue
            if not map_raw.ok:
                last_backend_error = map_raw.error
                continue
            map_notes.extend(parse_map(map_raw.text, window, max_notes=self._digest_map_notes))

        if not map_notes:
            if last_backend_error:
                return f"学びノート生成に失敗しました: {last_backend_error}"
            return "学びノート生成に失敗しました"

        # reduce フェーズ: 文書全体で map ノートを統合し、タグを付与する。
        # tag_catalog は同一 notebook の既存タグ（他資料分含む・この実行内で先行
        # 文書が保存した分も含む）を渡し、表記揺れなく既存タグを再利用しやすく
        # する（brief §6・list_tags_by_notebook 配線）。
        reduce_prompt = build_reduce_prompt(
            map_notes,
            tag_catalog=tuple(tag_catalog),
            persona=persona,
            title=title,
            max_notes=self._digest_max_notes,
        )
        try:
            reduce_raw = backend.answer(reduce_prompt, workdir=workdir, schema=REDUCE_SCHEMA)
        except Exception:  # noqa: BLE001 - reduce 失敗も doc 全体は落とさず劣化継続
            reduce_raw = None

        if reduce_raw is not None and not reduce_raw.ok:
            last_backend_error = reduce_raw.error

        if reduce_raw is not None and reduce_raw.ok:
            reduced_notes, tags = parse_reduce(
                reduce_raw.text, map_notes, max_notes=self._digest_max_notes
            )
        else:
            reduced_notes, tags = [], []

        if reduced_notes:
            final_notes = reduced_notes
        else:
            # reduce 失敗（backend 例外・ok=False・parse失敗で notes が空）時は、
            # 文書全体としての統合・重複除去こそ得られないが、map フェーズの学び自体は
            # 有効なため丸ごと捨てず、map ノートを digest_max_notes にクランプして
            # そのまま採用する（劣化継続）。この場合タグは reduce 専用の付与物のため空。
            # observability のため、最後に観測した backend エラー文言を含めて
            # warning ログを残す（コードレビュー指摘#5。戻り値スキーマは変えない）。
            _logger.warning(
                "digest reduce フェーズが失敗したため map ノートで劣化継続します: "
                "notebook=%s doc_id=%s last_backend_error=%s",
                notebook, doc_id, last_backend_error,
            )
            final_notes = map_notes[: self._digest_max_notes]
            tags = []

        note_dicts = [
            {
                "text": self._mask(note.text) if self._mask is not None else note.text,
                "source_span": note.span,
                "source_hash": content_hash,
                "model": backend.name,
                "section": (
                    self._mask(note.section)
                    if self._mask is not None and note.section is not None
                    else note.section
                ),
                "page": note.page,
                "source_chunk_ids": list(note.chunk_ids),
                "pipeline": DIGEST_PIPELINE_VERSION,
            }
            for note in final_notes
        ]
        self._store.replace_study_notes(notebook, doc_id, note_dicts)
        masked_tags = [self._mask(tag) if self._mask is not None else tag for tag in tags]
        self._store.replace_document_tags(notebook, doc_id, masked_tags)
        # 後続文書の reduce プロンプトにこの文書のタグを反映させるため、
        # 呼び出し元と共有する tag_catalog にその場で追記する（DB再問い合わせなし）。
        for tag in masked_tags:
            if tag not in tag_catalog:
                tag_catalog.append(tag)
        return "generated"
