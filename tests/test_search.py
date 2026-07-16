"""search.cosine_topk の純粋関数テスト。matrix/query は正規化済み前提。"""
from __future__ import annotations

import numpy as np

from shelf.search import cosine_topk


def test_ranks_closest_vector_first():
    ids = ["a", "b", "c"]
    matrix = np.array(
        [
            [1.0, 0.0],   # a: query と直交 → score 0
            [0.0, 1.0],   # b: query と同方向 → score 1
            [0.0, -1.0],  # c: query と逆方向 → score -1
        ],
        dtype=np.float32,
    )
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=3)

    assert [h.id for h in hits] == ["b", "a", "c"]


def test_scores_are_in_descending_order():
    ids = ["a", "b", "c"]
    matrix = np.array([[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]], dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=3)

    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_respects_limit():
    ids = ["a", "b", "c"]
    matrix = np.array([[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]], dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=1)

    assert len(hits) == 1
    assert hits[0].id == "a"


def test_returns_empty_list_for_empty_matrix():
    ids: list[str] = []
    matrix = np.zeros((0, 0), dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=5)

    assert hits == []
