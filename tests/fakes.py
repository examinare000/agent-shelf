"""テスト専用の決定論的ダブル群。ネットワーク・実 CLI・実プロセスを一切使わない。

FakeEmbedder は recall/tests/fakes.py と同じ作法（既知文字列は明示ベクトル、
未知文字列はハッシュから決定論的に生成）を dim をコンストラクタ引数化して流用する。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from shelf.convert import ConvertResult
from shelf.ports import RawAnswer, RouteTarget


class FakeEmbedder:
    model_name = "fake-embedder"

    def __init__(self, dim: int = 8, known: dict[str, list[float]] | None = None) -> None:
        self.dim = dim
        self.known = known or {}

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)

    def _vec(self, text: str) -> np.ndarray:
        if text in self.known:
            vec = np.array(self.known[text], dtype=np.float32)
        else:
            # sha256 は32バイト固定なので、dim>8 では1回のdigestだけでは足りない。
            # カウンタを混ぜて繰り返しハッシュし、必要バイト数まで伸長する。
            needed_bytes = self.dim * 4
            digest = b""
            counter = 0
            while len(digest) < needed_bytes:
                digest += hashlib.sha256(f"{text}:{counter}".encode("utf-8")).digest()
                counter += 1
            vec = np.frombuffer(digest[:needed_bytes], dtype=np.uint32).astype(np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


class FakeAnswerBackend:
    """AnswerBackend Protocol を満たす決定論的ダブル。

    canned に単一の RawAnswer/str を渡せば毎回同じ結果を返し、
    list を渡せば呼び出し順に消費する（最後の要素に達したらそれを返し続ける）。
    呼び出し履歴は calls に記録し、service.py 側の副作用検証に使う。
    """

    name = "fake"

    def __init__(
        self,
        canned: RawAnswer | str | BaseException | list[RawAnswer | str | BaseException] | None = None,
    ) -> None:
        if canned is None:
            canned = RawAnswer(text="", ok=True, error=None)
        self._responses: list[RawAnswer | str | BaseException] = (
            list(canned) if isinstance(canned, list) else [canned]
        )
        self._call_count = 0
        self.calls: list[dict] = []

    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer:
        self.calls.append({"prompt": prompt, "workdir": workdir, "schema": schema})
        index = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        response = self._responses[index]
        # 例外インスタンスを canned に混ぜられるようにし、Shelver 等が
        # backend.answer() 自体の例外（ok=False とは別の失敗経路）を継続可能な
        # エラーへ変換する経路をテストできるようにする（shelf/tests/test_shelver.py）。
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return RawAnswer(text=response, ok=True, error=None)
        return response


class FakeLibrarian:
    """Librarian.route と同じシグネチャ(question, catalog) -> list[RouteTarget] を持つ
    決定論的ダブル。routing.py の判断ロジック(プロンプト構成・パース・フォールバック)を
    経由せず、固定の RouteTarget リストを返す。consult() の集約ロジック(層1 のルーティング
    判断をモックしつつ、層2 の fan-out・集約だけを検証したい)テスト用（設計書 §9-B）。
    """

    def __init__(self, targets: list[RouteTarget]) -> None:
        self._targets = targets
        self.calls: list[dict] = []

    def route(self, question: str, catalog) -> list[RouteTarget]:
        self.calls.append({"question": question, "catalog": catalog})
        return self._targets


class FakeConverter:
    """convert.py の変換器差し替え用の決定論的ダブル。

    service.py が要求する convert_file(path)->ConvertResult / convert_url(url)->ConvertResult
    の2メソッド構成に合わせる（旧 convert(path_or_url) 単一メソッドは service.py の
    実際のインターフェースと不整合だったため修正）。呼び出し引数は file_calls/url_calls に
    記録し、add_source からの委譲経路をテストで検証できるようにする。
    """

    def __init__(
        self,
        markdown: str = "",
        converter: str = "raw",
        title: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> None:
        self._result = ConvertResult(markdown=markdown, converter=converter, title=title, notes=notes)
        self.file_calls: list[Path] = []
        self.url_calls: list[str] = []

    def convert_file(self, path: Path) -> ConvertResult:
        self.file_calls.append(path)
        return self._result

    def convert_url(self, url: str) -> ConvertResult:
        self.url_calls.append(url)
        return self._result
