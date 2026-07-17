"""
ローカル LLM エンジン（Ollama /api/chat 経由）。

RTX 4060 8GB 実機・Tailscale VPN 越し接続を想定したローカルバックエンド。
codex/gemini/agy と異なり subprocess ではなく HTTP 経由で呼び出すため、
標準ライブラリの urllib.request のみを使用する（新規ランタイム依存を増やさない）。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from shelf import config
from shelf.ports import RawAnswer


def build_payload(prompt: str, model: str, schema: dict | None) -> dict:
    """Ollama /api/chat のリクエストボディを組み立てる（純粋関数）。

    Args:
        prompt: エンジンに投入するプロンプト（user メッセージとして渡す）
        model: 使用するモデル名（例: "qwen3:8b"）
        schema: structured outputs 用 JSON スキーマ。None なら format キーを省く

    Returns:
        /api/chat に POST するペイロード dict

    Notes:
        think: false は常に付与する。qwen3 系は thinking モードを持ち、
        思考トークンが JSON 出力と干渉するため抑止が必要。古い Ollama が
        未知フィールドを無視する場合も無害（設計書 §10-4）。
        temperature: 0 も常に付与する。分類・ルーティング等の構造化判断で
        実行ごとに結果が揺れ、理由文と action が自己矛盾する事象を実機で
        観測したため、サンプリングを決定論化する（根拠付きQAでも温度0は
        資料への忠実性を優先する方向に働くため全経路で固定してよい）。
    """
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    if schema is not None:
        payload["format"] = schema
    return payload


def is_reachable(url: str, *, timeout: float = 1.0) -> bool:
    """指定 URL の Ollama デーモンに HTTP 疎通できるかを確認する（`shelf setup` の
    ローカル LLM 自動検出用）。

    urllib.request の import は本ファイル（design doc §9-C の import ガード）に
    限定されるため、shelf/setup.py はこの関数を経由してのみ HTTP 疎通確認できる。
    /api/tags はモデル一覧取得用の軽量エンドポイントで、応答の中身は見ない
    （疎通確認だけが目的で、レスポンス本文の解釈はここでは不要なため）。
    """
    try:
        with urlopen(Request(f"{url}/api/tags"), timeout=timeout):
            return True
    except Exception:
        return False


class OllamaBackend:
    """Ollama /api/chat を使用するローカル LLM エンジン実装（RTX 4060 8GB 実機想定）。

    url/model 省略時は config.OLLAMA_URL/OLLAMA_MODEL を既定値として使う。
    codex/gemini/agy と異なり subprocess ではなく HTTP 経由で呼び出すため
    engines/runner.py は使わず、urllib.request を直接使用する
    （urllib.request の import 元は本ファイルと convert.py の URL fetch のみに限定。
    tests/test_boundaries.py で強制）。
    """

    name = "ollama"

    def __init__(
        self,
        timeout: int = 300,
        url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.url = url if url is not None else config.OLLAMA_URL
        self.model = model if model is not None else config.OLLAMA_MODEL

    def answer(self, prompt: str, workdir: Path, schema: dict | None) -> RawAnswer:
        """Ollama に質問を投げ、message.content から応答を抽出する。

        Args:
            prompt: エンジンに投入するプロンプト
            workdir: 使用されない（Ollama API はサンドボックス concept を持たない）
            schema: structured outputs 用 JSON スキーマ（format キーとして渡す）

        Returns:
            RawAnswer（text, ok, error）。接続エラー・HTTP エラー・タイムアウト・
            JSON パース失敗はいずれも例外として送出されるため、他 engines/*.py と
            同様に単一の except Exception で捕捉し、安全な要約（例外クラス名のみ。
            URL・レスポンス全文・内部詳細は含めない）に変換する。
        """
        payload = build_payload(prompt, self.model, schema)
        try:
            request = Request(
                f"{self.url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            text = data["message"]["content"]
            return RawAnswer(text=text, ok=True, error=None)
        except Exception as e:
            return RawAnswer(
                text="",
                ok=False,
                error=f"ollama error: {type(e).__name__}",
            )
