"""Embedding のポート定義と、fastembed(ONNX) による実装。

Embedder を Protocol にして注入可能にすることで、ドメイン(ShelfService)は
「モデルが何か」を一切知らずに済む。テストは FakeEmbedder（tests側）に差し替え、
ネットワーク・モデルダウンロードなしで全経路を検証できる。
"""
from __future__ import annotations

from typing import Protocol

import numpy as np
from fastembed import TextEmbedding


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2正規化。ゼロベクトルは0除算を避けてそのまま返す（cosine計算での事故防止）。"""
    if vec.ndim == 1:
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    safe_norms = np.where(norms > 0, norms, 1.0)
    return vec / safe_norms


class FastEmbedEmbedder:
    """fastembed(ONNX) をラップする実装。e5系のプレフィックス付与は
    fastembed の passage_embed/query_embed がモデルごとに内部で処理するため、
    ここでは呼び出しと明示的な L2 正規化だけに責務を絞る。
    """

    def __init__(
        self, model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    ) -> None:
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        self.dim = TextEmbedding.get_embedding_size(model_name)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self._model.passage_embed(texts)), dtype=np.float32)
        return l2_normalize(vecs)

    def embed_query(self, text: str) -> np.ndarray:
        vec = np.array(next(iter(self._model.query_embed([text]))), dtype=np.float32)
        return l2_normalize(vec)
