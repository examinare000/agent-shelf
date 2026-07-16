"""embedder.py の純粋部分（L2正規化）のみを単体テストする。

FastEmbedEmbedder 自体は ONNX モデルの実ダウンロードを伴うため、
ここでは検証せず実データスモーク（README/報告）で確認する（設計書 §14）。
"""
from __future__ import annotations

import numpy as np

from shelf.embedder import l2_normalize


def test_l2_normalize_single_vector_has_unit_norm():
    vec = np.array([3.0, 4.0], dtype=np.float32)
    normalized = l2_normalize(vec)
    assert np.isclose(np.linalg.norm(normalized), 1.0)


def test_l2_normalize_matrix_normalizes_each_row():
    matrix = np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32)
    normalized = l2_normalize(matrix)
    norms = np.linalg.norm(normalized, axis=1)
    np.testing.assert_allclose(norms, [1.0, 1.0])


def test_l2_normalize_zero_vector_stays_zero_without_division_error():
    vec = np.array([0.0, 0.0], dtype=np.float32)
    normalized = l2_normalize(vec)
    np.testing.assert_allclose(normalized, [0.0, 0.0])
