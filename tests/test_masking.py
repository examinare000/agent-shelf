"""masking.py の drift ガード、および mask() 現行規則の仕様固定テスト。

drift ガード（import 経路の健全性）に加えて、distill/extract.py の mask()
が持つ 5 つの regex（sk-/ghp_系/AKIA/汎用 password 等/JWT）の実挙動を
positive/negative ケースで固定する。extract.py は agent-recall と共有される
改変禁止の既存資産なので、ここではパターンの追加・修正は行わず、現行の
挙動をそのままテストとして記録する（=仕様のスナップショット）。

PII（メール・電話・住所）はマスク対象外（蔵書コーパスでは誤マスクの害が
大きい）。パターン追加は agent-recall との同期方針決定が必要。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shelf.masking import mask

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_mask_redacts_secret_like_string() -> None:
    text = "token: sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdefghij"

    result = mask(text)

    assert result != text


class TestPositiveCases:
    """各 regex が「マスクすべき値」を実際にマスクすることを確認する。"""

    @pytest.mark.parametrize(
        ("text", "expected_marker"),
        [
            pytest.param(
                "sk-ABCDEFGHIJKL", "<REDACTED-KEY>", id="sk-exactly-min-length-12"
            ),
            pytest.param(
                "here is my key sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890",
                "<REDACTED-KEY>",
                id="sk-openai-style-embedded-in-sentence",
            ),
            pytest.param(
                "ghp_1234567890ABCDEFGHIJ", "<REDACTED-TOKEN>", id="ghp-personal-token"
            ),
            pytest.param(
                "ghs_ABCDEFGHIJ1234567890XX", "<REDACTED-TOKEN>", id="ghs-server-token"
            ),
            pytest.param("AKIA1234567890AB", "<REDACTED-AWS>", id="akia-min-length-12"),
            pytest.param(
                "password: hunter2", "password=<REDACTED>", id="password-colon-value"
            ),
            pytest.param(
                "api_key=abc123", "api_key=<REDACTED>", id="api-key-underscore-equals"
            ),
            pytest.param(
                "api-key: xyz", "api-key=<REDACTED>", id="api-key-hyphen-colon"
            ),
            pytest.param(
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                "<REDACTED-JWT>",
                id="jwt-three-segments",
            ),
        ],
    )
    def test_masks_secret(self, text: str, expected_marker: str) -> None:
        assert expected_marker in mask(text)


class TestNegativeCases:
    """マスク対象ではない文字列が変更されない（過剰マスクされない）ことを確認する。

    spec で例示された典型的な誤検知の種: sk- を含む語の途中(risk-free)、
    短すぎる接頭辞、値を伴わない裸のキーワード、JWT に見えるが1セグメント
    しかない base64、通常の base64 断片、AWS プレフィックスの大文字小文字違い。
    """

    @pytest.mark.parametrize(
        ("text",),
        [
            pytest.param("this is a risk-free investment", id="sk-substring-midword-risk-free"),
            pytest.param("sk-short", id="sk-prefix-too-short"),
            pytest.param("ghp_short", id="ghp-suffix-too-short"),
            pytest.param(
                "akia1234567890ab",
                id="akia-lowercase-not-matched-case-sensitive",
            ),
            pytest.param("AKIAshort", id="akia-suffix-too-short"),
            pytest.param("password", id="password-bare-word-no-value"),
            pytest.param(
                "my password is great but no colon or equals",
                id="password-word-without-delimiter",
            ),
            pytest.param(
                "eyJhbGciOiJIUzI1NiJ9",
                id="jwt-looking-single-segment-no-dots",
            ),
            pytest.param(
                "SGVsbG8gV29ybGQhCg==",
                id="ordinary-base64-fragment-unrelated-to-jwt",
            ),
        ],
    )
    def test_leaves_text_unchanged(self, text: str) -> None:
        assert mask(text) == text


class TestIdempotency:
    """mask(mask(x)) == mask(x)。二重適用しても追加の変換が発生しない。"""

    @pytest.mark.parametrize(
        ("text",),
        [
            pytest.param(
                "token: sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdefghij",
                id="sk-key-with-label",
            ),
            pytest.param("ghp_1234567890ABCDEFGHIJ", id="ghp-token"),
            pytest.param("AKIA1234567890AB", id="akia-key"),
            pytest.param(
                "password: hunter2 and token: sk-ABCDEFGHIJKLMNOP",
                id="multiple-secrets-in-one-string",
            ),
            pytest.param(
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                id="jwt",
            ),
            pytest.param("nothing secret here at all", id="plain-text"),
        ],
    )
    def test_repeated_mask_is_stable(self, text: str) -> None:
        once = mask(text)
        twice = mask(once)
        assert twice == once


class TestKnownQuirks:
    """現行規則の既知の癖（過剰/過少マスク）。extract.py は改変禁止のため、
    仕様として固定するのみで修正はしない。

    発見内容:
    1. regex 適用順によるラベル剥落: sk-/ghp_/AKIA/JWT の各 regex が先に走り
       値を <REDACTED-*> に置換した後、汎用 password|token|... regex が
       "token: <REDACTED-KEY>" のような文字列に再度マッチし、より具体的な
       マーカーを汎用の "token=<REDACTED>" で上書きしてしまう。実害（漏洩）
       はないが、ログ上でどの種類の秘密だったかの情報が失われる（過剰マスク
       ではなく「情報劣化」）。
    2. 汎用 password|passwd|secret|api_key|token regex は値を `\\S+`
       （空白を含まない）でしか捕捉しないため、`password: "hunter 2 style"`
       のようにクォートで囲まれた複数語の値は先頭の1語しかマスクされず、
       残りの単語がログに残る（過少マスク・実害あり）。
    3. sk-/ghp_ 系の regex には単語境界 (`\\b`) が無いため、"prefix_sk-XXXX"
       のように語の途中に埋め込まれていてもマッチする。値そのものは正しく
       マスクされるため過少マスクにはならないが、意図せず前後の文字列が
       欠落方向に働く可能性がある観察事項として記録する。
    """

    def test_generic_regex_overwrites_specific_marker_after_labeled_key(self) -> None:
        text = "token: sk-ABCDEFGHIJKLMNOP"

        result = mask(text)

        # 具体的なマーカー <REDACTED-KEY> ではなく、汎用マーカーに上書きされる。
        assert result == "token=<REDACTED>"
        assert "<REDACTED-KEY>" not in result

    def test_quoted_multiword_value_only_partially_masked(self) -> None:
        text = 'password: "hunter 2 with spaces"'

        result = mask(text)

        # 最初の空白区切りトークンまでしかマスクされず、残りの単語が漏洩する。
        assert result == 'password=<REDACTED> 2 with spaces"'
        assert "hunter" not in result
        assert "with spaces" in result


class TestExtractPyOverride:
    """SHELF_EXTRACT_PY env var による差し替え経路のテスト。

    masking.py は import 時に importlib で extract.py を読み込み、
    sys.modules にキャッシュするため、同一プロセス内での再読み込みでは
    検証できない。別プロセス(subprocess)を起動し、SHELF_EXTRACT_PY で
    tmp_path 上の代替 extract.py を指すことで、override 経路が実際に
    使われることを確認する。
    """

    def test_subprocess_uses_overridden_extract_py(self, tmp_path: Path) -> None:
        fake_extract = tmp_path / "fake_extract.py"
        fake_extract.write_text(
            "import re\n"
            "def mask(text):\n"
            "    return re.sub(r'FAKE-SECRET-\\d+', '<FAKE-REDACTED>', text)\n"
        )
        env = dict(os.environ, SHELF_EXTRACT_PY=str(fake_extract))

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from shelf.masking import mask; print(mask('FAKE-SECRET-123 leaked'))",
            ],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "<FAKE-REDACTED> leaked"

    def test_subprocess_without_override_uses_real_extract_py(self) -> None:
        # override 無しの通常経路では本物の extract.py の regex が使われる
        # ことを、同じ subprocess 経路で対照確認する（回帰時に override 側
        # の配線だけが誤って常時有効化されていないことの保証）。
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from shelf.masking import mask; "
                "print(mask('token: sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890'))",
            ],
            env=os.environ.copy(),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert "<REDACTED-KEY>" not in result.stdout  # 既知の癖: 汎用側に上書きされる
        assert "token=<REDACTED>" in result.stdout
