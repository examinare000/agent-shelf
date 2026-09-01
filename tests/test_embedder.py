"""embedder.py の純粋部分（L2正規化）と TextEmbedding への引数受け渡しを単体テストする。

FastEmbedEmbedder の実推論は ONNX モデルの実ダウンロードを伴うため、
ここでは検証せず実データスモーク（README/報告）で確認する。ただし cache_dir の
受け渡しだけは TextEmbedding をスパイへ差し替えることでダウンロードなしに検証する。
"""
from __future__ import annotations

import numpy as np

import shelf.embedder as embedder_module
from shelf.config import MODEL_CACHE_DIR
from shelf.embedder import FastEmbedEmbedder, l2_normalize


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


class SpyTextEmbedding:
    """TextEmbedding へ渡された kwargs を記録するだけのスパイ（実ダウンロードなし）。"""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        SpyTextEmbedding.last_kwargs = kwargs

    @staticmethod
    def get_embedding_size(model_name: str) -> int:
        return 384


def test_fastembed_embedder_passes_model_cache_dir_by_default(monkeypatch):
    # cache_dir 未指定時に fastembed 既定（$TMPDIR 配下）へ落ちると、サンドボックス内外で
    # 別パスに解決されモデルを再ダウンロードしに行くため、config の固定パスを必ず渡す。
    monkeypatch.setattr(embedder_module, "TextEmbedding", SpyTextEmbedding)
    FastEmbedEmbedder()
    assert SpyTextEmbedding.last_kwargs["cache_dir"] == str(MODEL_CACHE_DIR)


def test_fastembed_embedder_explicit_cache_dir_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(embedder_module, "TextEmbedding", SpyTextEmbedding)
    FastEmbedEmbedder("custom/model", tmp_path / "mycache")
    assert SpyTextEmbedding.last_kwargs["cache_dir"] == str(tmp_path / "mycache")
    assert SpyTextEmbedding.last_kwargs["model_name"] == "custom/model"
