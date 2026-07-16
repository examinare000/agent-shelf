"""numpy 総当たり cosine 類似度による topK ランキング（純粋関数）。

この規模（数百〜数千チャンク）では ANN 索引は過剰なので採用しない（設計書 §0）。
matrix・query_vec は事前に L2 正規化済みという前提を置くことで、
cosine 類似度が単純な内積（matrix @ query_vec）に帰着し実装が最小になる。
"""
from __future__ import annotations

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
