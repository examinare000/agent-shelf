"""masking.py の drift ガード（軽量）。

mask のロジック自体は distill/extract.py 側でテスト済みなので、ここでは
「shelf からも mask を呼び出せて、秘密様文字列が実際に変換される」ことだけを
確認する（= import 経路が壊れていない・別実装にすり替わっていないことの保証）。
"""
from __future__ import annotations

from shelf.masking import mask


def test_mask_redacts_secret_like_string() -> None:
    text = "token: sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdefghij"

    result = mask(text)

    assert result != text
