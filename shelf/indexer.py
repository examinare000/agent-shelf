"""corpus/<notebook> 走査 → 増分判定 → chunk → embed → store のオーケストレーション層。

chunker(純粋)・store(境界)・embedder(境界) はそれぞれ単体テスト済みなので、
ここでは「いつ再チャンク/再埋め込みするか」という増分判定の分岐だけに責務を絞る
（recall/recall/indexer.py と同型。notebook 単位の走査対象・doc_id 命名だけが異なる）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shelf.chunker import chunk_markdown
from shelf.names import validate_notebook_name
from shelf.store import Store

# 本文チャンクは seq 0 起点(chunker)なので -1 は衝突しない。UNIQUE(notebook, doc_id, seq) も満たす。
SUMMARY_SEQ = -1
SUMMARY_SECTION_USER = "資料概要"
SUMMARY_SECTION_AUTO = "資料概要（自動生成の要約）"

# 学びノート(digest)チャンクの seq 起点。本文(≥0)・サマリ(SUMMARY_SEQ=-1)と衝突しない
# 予約負域(≤-2)を使う(design §4-B)。study_notes.seq(0起点)ごとに 1 ずつ負方向へ進める。
DIGEST_SEQ_BASE = -2


@dataclass(frozen=True)
class IndexStats:
    indexed: int
    skipped: int
    pruned: int
    chunks_written: int
    errors: list[str]


def index_notebook(
    corpus_dir: Path,
    notebook: str,
    store: Store,
    embedder,
    mask: Callable[[str], str] | None = None,
    full: bool = False,
) -> IndexStats:
    """corpus_dir/<notebook>/*.md を増分索引化する。

    - meta の "model" が embedder.model_name と異なる場合は自動で全再構築する
      （モデルを跨いでベクトル空間が混在するのを防ぐため。recall と同じ方針）。
    - full=True は状態を無視して全ファイルを再チャンク・再埋め込みする。
    - store.chunks テーブルには notebook への FK 制約が無いため、事前の
      store.create_notebook() 呼び出しは不要（store.py 側の設計に従う）。
    """
    # ShelfService を経由しない直接呼び出し経路(将来の別CLI等)への防御として、
    # ここでも notebook 名の構文検証だけは通す(重大指摘#1)。store.get_notebook による
    # 存在確認まではここでは行わない――未知 notebook を指定しても corpus_dir/notebook
    # の走査結果が単に空になるだけで実害がないため、検証コストに見合わない。
    validate_notebook_name(notebook)

    stored_model = store.get_meta("model")
    if stored_model is not None and stored_model != embedder.model_name:
        full = True

    notebook_dir = corpus_dir / notebook

    indexed = 0
    skipped = 0
    chunks_written = 0
    errors: list[str] = []
    existing_source_files: set[str] = set()

    for path in sorted(notebook_dir.glob("*.md")):
        source_path = str(path.relative_to(corpus_dir))
        doc_id = path.stem

        try:
            stat = path.stat()

            if not full:
                state = store.get_file_state(source_path)
                unchanged = (
                    state is not None
                    and state["mtime"] == stat.st_mtime
                    and state["size"] == stat.st_size
                    and state["model"] == embedder.model_name
                )
                if unchanged:
                    existing_source_files.add(source_path)
                    skipped += 1
                    continue

            md = path.read_text(encoding="utf-8")
        except OSError as exc:
            # distill/extract.py と同じ方針: 1ファイルの読み込みエラーで索引全体を
            # 止めない。既存の chunk/file_state を誤って prune しないよう
            # existing として扱い、次回再試行できるようにする。
            existing_source_files.add(source_path)
            errors.append(f"{source_path}: {exc}")
            continue

        try:
            chunks = chunk_markdown(
                md, notebook=notebook, doc_id=doc_id, source_path=source_path, mask=mask
            )
        except Exception as exc:  # noqa: BLE001 - 1ファイルのチャンク失敗で全体を止めない
            existing_source_files.add(source_path)
            errors.append(f"{source_path}: {exc}")
            continue

        existing_source_files.add(source_path)
        store.delete_by_source_file(source_path)

        rows = [
            {
                "id": c.id,
                "notebook": c.notebook,
                "doc_id": c.doc_id,
                "source_path": c.source_path,
                "section": c.section,
                "page": c.page,
                "seq": c.seq,
                "text": c.text,
                "kind": "body",
            }
            for c in chunks
        ]

        # description は documents 側で既に mask 済みの前提のため、本文と違って
        # ここでは mask を適用しない（背景の要件どおり: service 層の保存時点で処理済み）。
        doc = store.get_document(doc_id) or {}
        description = doc.get("description")
        if description:
            section = (
                SUMMARY_SECTION_AUTO
                if doc.get("description_source") == "auto"
                else SUMMARY_SECTION_USER
            )
            rows.insert(
                0,
                {
                    "id": f"{notebook}/{doc_id}#{SUMMARY_SEQ}",
                    "notebook": notebook,
                    "doc_id": doc_id,
                    # 本文と同じ source_path にすることで、delete_by_source_file /
                    # prune_missing / rm --doc の掃除ロジックが要約チャンクにも
                    # 自然に及ぶようにする（別経路の掃除処理を増やさない）。
                    "source_path": source_path,
                    "section": section,
                    "page": None,
                    "seq": SUMMARY_SEQ,
                    "text": description,
                    "kind": "summary",
                },
            )

        # study_notes(shelf digest が書いた学びノートの source-of-truth)を
        # kind='digest' チャンクとして索引化する(design §4-B: 既存 description
        # → seq=-1 サマリチャンクパターンの一般化)。source-of-truth は
        # study_notes テーブルにあり LLM 呼び出しは不要なため、既存 embedder
        # で埋め込んで chunks に相乗りさせるだけでよい。本文と同じ source_path
        # にすることで、こちらも delete_by_source_file / prune_missing の
        # 既存掃除ロジックに自然に乗る(§4-B)。
        for note in store.list_study_notes(notebook, doc_id):
            digest_seq = DIGEST_SEQ_BASE - note["seq"]
            # map-reduce パイプライン(pipeline=2)の study_notes は section/page を
            # チャンク接地情報として直接持つため、これを優先する。旧パイプライン
            # (pipeline=1)は section を持たないので、後方互換として従来どおり
            # source_span を代替の人間可読表示に使う。
            rows.append(
                {
                    "id": f"{notebook}/{doc_id}#{digest_seq}",
                    "notebook": notebook,
                    "doc_id": doc_id,
                    "source_path": source_path,
                    "section": note.get("section") or note.get("source_span"),
                    "page": note.get("page"),
                    "seq": digest_seq,
                    "text": note["text"],
                    "kind": "digest",
                }
            )

        if rows:
            embeddings = embedder.embed_documents([r["text"] for r in rows])
            for row, embedding in zip(rows, embeddings):
                row["embedding"] = embedding
            store.upsert_chunks(rows)
        store.set_file_state(
            source_path, mtime=stat.st_mtime, size=stat.st_size, model=embedder.model_name
        )
        indexed += 1
        chunks_written += len(rows)

    # file_state テーブルは notebook 列を持たずグローバルに全 notebook の
    # source_file を保持している(store.py 側の既存スキーマ)。existing_source_files
    # は本 notebook 分しか集めていないため、そのまま渡すと他 notebook のファイルが
    # 「消えた」と誤判定されて prune されてしまう。他 notebook 分は無条件で
    # "existing" 扱いに加えて保護する。
    other_notebook_prefix = f"{notebook}/"
    other_notebooks_tracked = {
        f for f in store.list_source_files() if not f.startswith(other_notebook_prefix)
    }
    pruned = store.prune_missing(existing_source_files | other_notebooks_tracked)
    store.set_meta("model", embedder.model_name)
    store.set_meta("dim", str(embedder.dim))

    return IndexStats(
        indexed=indexed,
        skipped=skipped,
        pruned=pruned,
        chunks_written=chunks_written,
        errors=errors,
    )
