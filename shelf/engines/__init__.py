"""
サブスク CLI エンジンの factory と registry。

create_backend() で指定名のエンジンを timeout 付きで初期化する。
未知名は ValueError（利用可能名を列挙した安全なメッセージ）。
"""
from __future__ import annotations

from shelf.engines.agy import AgyBackend
from shelf.engines.codex import CodexBackend
from shelf.engines.gemini_cli import GeminiCliBackend
from shelf.engines.ollama import OllamaBackend
from shelf.ports import AnswerBackend

_BACKENDS = {
    "codex": CodexBackend,
    "gemini": GeminiCliBackend,
    "agy": AgyBackend,
    "ollama": OllamaBackend,
}


def create_backend(name: str, timeout: int) -> AnswerBackend:
    """指定名のエンジンを初期化して AnswerBackend Protocol インスタンスを返す。

    Args:
        name: エンジン名（"codex", "gemini", "agy", "ollama"）
        timeout: 実行timeout秒

    Returns:
        AnswerBackend Protocol を実装するエンジンインスタンス

    Raises:
        ValueError: 未知のエンジン名
    """
    if name not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS.keys()))
        raise ValueError(f"Unknown backend: {name}. Available: {available}")
    backend_cls = _BACKENDS[name]
    return backend_cls(timeout=timeout)


__all__ = ["create_backend"]
