"""テストセッション全体の共通フィクスチャ。

開発者の実行環境に ~/.config/agent-shelf/config.env が存在すると、config.py の
既定値解決（resolve_config_path の SHELF_CONFIG 未設定時フォールバック）がその
実ファイルの中身を拾ってしまい、「未設定時は既定値」を前提にしたテストの
アサーションを汚染しうる。autouse セッションフィクスチャで SHELF_CONFIG を
実在しない一時パスへ固定し、実ファイルの有無に関わらずテストが決定的に
振る舞うようにする。

session スコープの autouse フィクスチャだが、pytest はテスト実行フェーズの前に
全テストモジュールの import（collection）を完了させるため、collection 中に
`shelf.config` を最初に import するモジュール（import 時に環境変数を解決する）
より後にこのフィクスチャの setup が走ると手遅れになりうる。そのため、フィクス
チャ本体に加えて本ファイルのトップレベルでも同じ固定を行い、conftest.py が
（同ディレクトリのテストモジュール一式より必ず先に）import される時点で
即座に SHELF_CONFIG を確定させる（フィクスチャは以後の一貫性維持と、他テストが
monkeypatch で書き換えた場合の値を保証する目的で残す）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 実在しない一時パス固定用のディレクトリ(実際に config.env ファイルを作らない
# ことで「未設定時と同じ既定値解決に落ちる」ことを保証する)。
_ISOLATED_CONFIG_DIR = tempfile.mkdtemp(prefix="shelf-test-config-")
_ISOLATED_CONFIG_PATH = str(Path(_ISOLATED_CONFIG_DIR) / "config.env")

# conftest.py の import 時点(同ディレクトリの他テストモジュールが import される
# より前)で固定する。
os.environ["SHELF_CONFIG"] = _ISOLATED_CONFIG_PATH


@pytest.fixture(scope="session", autouse=True)
def _pin_shelf_config_to_isolated_temp_path():
    """SHELF_CONFIG をテストセッション全体で実在しない一時パスに固定し続ける。"""
    os.environ["SHELF_CONFIG"] = _ISOLATED_CONFIG_PATH
    yield
