"""masking.py の drift ガード、および mask() 現行規則の仕様固定テスト。

drift ガード（import 経路の健全性）に加えて、distill/extract.py の mask()
が持つ 5 つの regex（sk-/ghp_系/AKIA/汎用 password 等/JWT）の実挙動を
positive/negative ケースで固定する。extract.py は agent-recall 由来の共有
資産であり、ここではパターンの追加・修正は行わず、現行の挙動をそのまま
テストとして記録する（=仕様のスナップショット）。

PII（メール・電話・住所）はマスク対象外（蔵書コーパスでは誤マスクの害が
大きい）。パターン追加や規則変更時は agent-recall 側との同期を検討してください。
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
            pytest.param(
                'password: "hunter 2 with spaces"',
                id="double-quoted-multiword-value",
            ),
            pytest.param(
                "api_key: 'foo bar baz'",
                id="single-quoted-multiword-value",
            ),
            pytest.param(
                'password: """hunter 2 with spaces"""',
                id="triple-quoted-multiword-value-with-unbalanced-quotes",
            ),
            pytest.param(
                'export API_KEY=""$REAL_KEY_VALUE""',
                id="empty-quote-wrapped-shell-variable",
            ),
            pytest.param(
                'password: "start of secret\nmore stuff here\napi_key: "closed later',
                id="crossline-unterminated-double-quote",
            ),
            pytest.param(
                "token: 'it starts\nhere and it's\nnever closed",
                id="crossline-unterminated-single-quote-with-apostrophe",
            ),
        ],
    )
    def test_repeated_mask_is_stable(self, text: str) -> None:
        once = mask(text)
        twice = mask(once)
        assert twice == once


class TestKnownQuirks:
    """現行規則の既知の癖（過剰/過少マスク）。

    発見内容:
    1. regex 適用順によるラベル剥落: sk-/ghp_/AKIA/JWT の各 regex が先に走り
       値を <REDACTED-*> に置換した後、汎用 password|token|... regex が
       "token: <REDACTED-KEY>" のような文字列に再度マッチし、より具体的な
       マーカーを汎用の "token=<REDACTED>" で上書きしてしまう。実害（漏洩）
       はないが、ログ上でどの種類の秘密だったかの情報が失われる（過剰マスク
       ではなく「情報劣化」）。
    2. （修正済み・2026-08-02）汎用 password|passwd|secret|api_key|token regex は
       元々値を `\\S+`（空白を含まない）でしか捕捉せず、`password: "hunter 2 style"`
       のようにクォートで囲まれた複数語の値は先頭の1語しかマスクされずに残りの
       単語がログに残っていた（過少マスク・実害あり）。値パターンをクォート文字列
       全体優先（`"..."` / `'...'` を丸ごと捕捉し、どちらでもなければ従来の `\\S+`
       にフォールバック）へ変更し解消した。挙動は TestQuotedValueMasking を参照。
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

    def test_quoted_multiword_value_is_fully_masked(self) -> None:
        text = 'password: "hunter 2 with spaces"'

        result = mask(text)

        # クォート文字列全体（閉じクォートまで）がマスクされ、漏洩しない。
        assert result == "password=<REDACTED>"
        assert "hunter" not in result
        assert "with spaces" not in result


class TestQuotedValueMasking:
    """クォート付き複数語 secret 値の修正後の挙動を固定する。

    値パターンは `"(?:\\\\.|[^"\\\\\\n])*"(?!\\\\S)` （ダブルクォート、内部エスケープ
    許容、改行は body から除外）/ `'(?:\\\\.|[^'\\\\\\n])*'(?!\\\\S)` （シングルクォート、
    同様）を `\\S+` より先に試し、どちらにもマッチしなければ従来どおり `\\S+` に
    フォールバックする。

    `\\n` を body から除外するのは、閉じクォートが別行にある入力（例:
    password 行の値がダブルクォートで開いたまま複数行下の別ラベル行で
    初めて閉じクォートに出会うケース）で無関係な複数行を丸ごと飲み込んで
    消してしまうのを防ぐため。閉じ直後に `(?!\\S)`（次が非空白なら不採用）を
    置くのは、値が空クォートや連続する複数個のクォート文字で始まるケースで
    「早期に閉じたと誤認して後続語を露出させる」短勝ちマッチを弾き、旧実装
    （`\\S+` のみ）と同等以上の安全側へ倒すため。両条件のいずれかで不採用になった
    場合は `\\S+` にフォールバックし、旧実装と同じ「先頭トークンのみマスク」に
    留まる。
    """

    def test_double_quoted_multiword_value_is_fully_masked(self) -> None:
        text = 'password: "hunter 2 with spaces"'

        result = mask(text)

        assert result == "password=<REDACTED>"

    def test_quoted_value_with_trailing_punctuation_falls_back_to_first_token(self) -> None:
        # 閉じクォート直後が非空白（, ) } ; 等。JSON5/YAML flow/Python kwarg で頻出）の
        # 場合は (?!\S) によりクォート分岐を採らず、旧実装と同じ先頭トークンのみの
        # マスクに留まる。短勝ちマッチの再発防止と引き換えの既知の制限（CHANGELOG 開示）。
        result = mask('password: "hunter 2 spaces",')

        assert result == 'password=<REDACTED> 2 spaces",'

    def test_single_quoted_multiword_value_is_fully_masked(self) -> None:
        text = "api_key: 'foo bar baz'"

        result = mask(text)

        assert result == "api_key=<REDACTED>"

    def test_double_quoted_value_with_escaped_quote_is_fully_masked(self) -> None:
        # 値の中に \" を含むエスケープ済みクォートがあっても、そこで閉じたと
        # 誤認せず本当の閉じクォートまでをマスクする。
        text = r'secret: "say \"hi\" to bob"'

        result = mask(text)

        assert result == "secret=<REDACTED>"
        assert "bob" not in result

    def test_single_quoted_value_with_escaped_quote_is_fully_masked(self) -> None:
        text = r"token: 'it\'s a secret'"

        result = mask(text)

        # 完全一致で全体マスクを確認済みのため、この時点で "secret" は result に
        # 一切含まれない（`result.replace("<REDACTED>", "")` の再チェックは
        # 直前の完全一致に完全に含意される冗長な主張だったため、意味のある
        # 主張として「アポストロフィエスケープを含む値でも label だけが残る」
        # ことを明示する形に置き換える）。
        assert result == "token=<REDACTED>"

    def test_double_quoted_value_starting_with_empty_quote_falls_back_to_full_token(
        self,
    ) -> None:
        # 値が空クォート ("") で始まり直後に非空白が続く場合、「早期に閉じた」と
        # 誤認して残りの語を露出させてはならない。(?!\S) が弾くことで \S+ に
        # フォールバックし、旧実装（\S+ のみ）と同じ「1トークン全体マスク」に
        # 落ち着く（このケースは内部に空白が無いため \S+ でも取りこぼしなく
        # 全体がマスクされる）。
        text = 'password: ""hunter2"'

        result = mask(text)

        assert result == "password=<REDACTED>"
        assert "hunter2" not in result

    def test_double_quoted_empty_value_surrounding_variable_falls_back_to_full_token(
        self,
    ) -> None:
        # shell 変数展開に典型的な `""$VAR""` 形式。空クォートの早期閉じ誤認で
        # $REAL_KEY_VALUE が露出してはならない。
        text = 'export API_KEY=""$REAL_KEY_VALUE""'

        result = mask(text)

        assert result == "export API_KEY=<REDACTED>"


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
