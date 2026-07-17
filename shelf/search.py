"""numpy 総当たり cosine 類似度による topK ランキング（純粋関数）。

この規模（数百〜数千チャンク）では ANN 索引は過剰なので採用しない（設計書 §0）。
matrix・query_vec は事前に L2 正規化済みという前提を置くことで、
cosine 類似度が単純な内積（matrix @ query_vec）に帰着し実装が最小になる。

rrf_merge/build_fts_query はベクトル検索と FTS5 キーワード検索を併用する
ハイブリッド検索（service.py が呼び出す）向けの純粋関数。store.py/service.py には
一切依存しない（sqlite3 も import しない）ため、id リスト・文字列だけで完結する
テストが書ける。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ScoredId:
    id: str
    score: float


def cosine_topk(
    matrix: np.ndarray, ids: list[str], query_vec: np.ndarray, limit: int
) -> list[ScoredId]:
    """matrix の各行(正規化済みベクトル)と query_vec の内積(=cosine類似度)で降順topKを返す。"""
    if matrix.shape[0] == 0:
        return []
    scores = matrix @ query_vec
    order = np.argsort(scores)[::-1][:limit]
    return [ScoredId(id=ids[i], score=float(scores[i])) for i in order]


def rrf_merge(rankings: Sequence[Sequence[str]], *, limit: int, k: int = 60) -> list[str]:
    """複数の順位付き id リストを Reciprocal Rank Fusion で統合し、上位 limit 件の
    id を返す（ベクトル検索順位・FTS5 キーワード検索順位の併用向け）。

    score(id) = Σ 1/(k + rank)（rank は各 ranking 内で 1 起点）。同点は「最初に
    登場した ranking・その中の順位」が早い方を先にする決定的なタイブレークとする
    （sorted は安定ソートなので、走査順=初出順を保った list を desc ソートするだけで
    この規則を満たす）。空の rankings・要素が空の ranking が混じっても頑健
    （スコア加算の対象が単に無い/減るだけで例外にはならない）。
    """
    scores: dict[str, float] = {}
    order: list[str] = []  # 初出順（同点タイブレーク用）
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            if item_id not in scores:
                scores[item_id] = 0.0
                order.append(item_id)
            scores[item_id] += 1.0 / (k + rank)
    ranked = sorted(order, key=lambda item_id: scores[item_id], reverse=True)
    return ranked[:limit]


_MAX_FTS_TERMS = 32
_MIN_RUN_LEN = 3  # trigram tokenizer は3文字未満のランを索引できない（照合不能）。


def _quote(term: str) -> str:
    # FTS5 標準の quoted-string escaping（`"` を `""` に二重化）。文字クラスの
    # ランに分割した後は `"` はラン境界として弾かれ term 内には現れない想定だが、
    # 将来の入力形式変化に備えた防御的実装として維持する。
    return '"' + term.replace('"', '""') + '"'


def _terms_from_run(run: str) -> list[str]:
    """1つの文字クラスラン（ASCII英数字 or 非ASCII）からタームを生成する。

    ASCII 英数字ランはそのまま1タームとするが、非ASCII（CJK等）ランはスペース
    無しで単語境界が取れない自然文になりがちなため、スライディングウィンドウの
    3文字グラム（stride 1）に展開する——これにより「量子力学の基礎について」の
    ような文全体が丸ごと1フレーズになり trigram 索引に一致しない、という不具合
    （コードレビュー指摘#1）を避ける。どちらの種別も3文字未満は trigram
    tokenizer で照合不能なため捨てる。
    """
    if len(run) < _MIN_RUN_LEN:
        return []
    if run.isascii():
        return [run]
    return [run[i : i + _MIN_RUN_LEN] for i in range(len(run) - _MIN_RUN_LEN + 1)]


def build_fts_query(question: str) -> str:
    """質問文を FTS5 MATCH 用に無害化した OR クエリへ変換する（純粋関数）。

    文字クラス（ASCII英数字 / 非ASCII / それ以外＝記号・空白）でランに分割し、
    各ランからタームを生成して `"..."` で囲みリテラル化する。これにより質問文に
    AND/OR/NEAR/`*` 等の FTS5 演算子や `"` が紛れ込んでも MATCH 構文エラーには
    ならない。記号・空白はラン境界として捨てる（ターム内容には残らない）。
    語同士は AND ではなく OR で結合する: デフォルトの AND だと質問文の全語一致に
    まで絞られ recall が落ちるため、trigram tokenizer 前提で部分一致が効く
    この用途では OR で広く拾うほうが望ましい。
    重複タームは初出順を保ったまま除去し、クエリ肥大を防ぐため先頭
    _MAX_FTS_TERMS 件（既定32）に切り詰める（先頭優先）。
    """
    terms: list[str] = []
    run_chars: list[str] = []
    run_is_ascii: bool | None = None

    def flush() -> None:
        if run_chars:
            terms.extend(_terms_from_run("".join(run_chars)))

    for ch in question:
        if ch.isalnum():
            is_ascii = ch.isascii()
            if run_is_ascii is not None and is_ascii != run_is_ascii:
                flush()
                run_chars = []
            run_chars.append(ch)
            run_is_ascii = is_ascii
        else:
            flush()
            run_chars = []
            run_is_ascii = None
    flush()

    unique_terms = list(dict.fromkeys(terms))[:_MAX_FTS_TERMS]
    if not unique_terms:
        return ""
    return " OR ".join(_quote(term) for term in unique_terms)
