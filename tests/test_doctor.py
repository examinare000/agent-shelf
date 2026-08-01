"""shelf doctor（起動前プリフライト診断）の単体テスト。

doctor.py は読み取り専用の診断層で、LLM呼び出し・モデルDL・書込みは一切行わない。
DB 到達性チェック（check_db_open）は「DB ファイルが既に存在する場合のみ」開いて
close する（未作成パスに対して Store を構築し新規スキーマ・WAL を生成してしまう
副作用を避けるため。レビュー指摘 must-1 対応）。実 PATH・実ネットワーク・
実ファイルシステムに依存させないため、各検査関数は which/reachable/store_factory
等を注入できる設計にし、テストはフェイクを注入して決定論的に検証する
（TestCheckDbOpen 末尾の2件のみ、本番デフォルト経路 store_factory=Store を
実際に通す統合テスト）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from shelf.doctor import (
    CheckResult,
    check_config_env,
    check_corpus_dir,
    check_db_open,
    check_db_parent_dir,
    check_engine_cli,
    check_fastembed_cache,
    check_ollama,
    resolve_fastembed_cache_dir,
    run_checks,
)


class TestCheckResult:
    def test_equals_plain_tuple_of_name_ok_detail(self):
        result = CheckResult(name="engine:codex", ok=True, detail="見つかりました")

        assert result == ("engine:codex", True, "見つかりました")


class TestCheckEngineCli:
    def test_ok_true_when_which_finds_command(self):
        result = check_engine_cli("codex", "codex", which=lambda name: True)

        assert result.name == "engine:codex"
        assert result.ok is True

    def test_ok_false_when_which_does_not_find_command(self):
        result = check_engine_cli("codex", "codex", which=lambda name: False)

        assert result.ok is False

    def test_passes_command_name_to_which(self):
        seen = []

        def fake_which(name: str) -> bool:
            seen.append(name)
            return True

        check_engine_cli("gemini", "gemini", which=fake_which)

        assert seen == ["gemini"]


class TestCheckOllama:
    def test_ok_true_when_reachable(self):
        result = check_ollama("http://127.0.0.1:11434", reachable=lambda url: True)

        assert result.name == "ollama"
        assert result.ok is True

    def test_ok_false_when_unreachable(self):
        result = check_ollama("http://127.0.0.1:11434", reachable=lambda url: False)

        assert result.ok is False

    def test_passes_url_to_reachable(self):
        seen = []

        def fake_reachable(url: str) -> bool:
            seen.append(url)
            return True

        check_ollama("http://example:11434", reachable=fake_reachable)

        assert seen == ["http://example:11434"]


class TestCheckDbParentDir:
    def test_ok_true_when_parent_dir_exists_and_writable(self, tmp_path):
        db_path = tmp_path / "shelf.db"

        result = check_db_parent_dir(db_path)

        assert result.name == "db_parent_dir"
        assert result.ok is True

    def test_ok_true_when_parent_dir_missing_but_creatable(self, tmp_path):
        """親ディレクトリ自体は無くても、Store 構築時に mkdir(parents=True) される
        ため、その祖先(既存の書込可能なディレクトリ)まで遡って判定できれば十分。
        """
        db_path = tmp_path / "not-yet-created" / "nested" / "shelf.db"

        result = check_db_parent_dir(db_path)

        assert result.ok is True

    @pytest.mark.skipif(
        os.name == "nt",
        reason="chmod 0o500 は Windows のディレクトリ書込み可否に影響しないため無効",
    )
    def test_ok_false_when_nearest_existing_ancestor_is_not_writable(self, tmp_path):
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o500)
        try:
            db_path = readonly_dir / "nested" / "shelf.db"

            result = check_db_parent_dir(db_path)

            assert result.ok is False
        finally:
            # tmp_path のクリーンアップが権限不足で失敗しないよう元に戻す。
            readonly_dir.chmod(0o700)

    def test_does_not_leave_a_stray_probe_file_behind(self, tmp_path):
        """os.access の代わりに実際の書込みで判定するため(should-3対応)、検査後に
        一時ファイルが残らないことを確認する(副作用ゼロの契約)。
        """
        db_path = tmp_path / "shelf.db"

        check_db_parent_dir(db_path)

        assert list(tmp_path.iterdir()) == []


class _FakeStore:
    """Store の代わりに注入する決定論的ダブル。sqlite3 に一切触れない。"""

    instances: list[str] = []

    def __init__(self, db_path):
        self.db_path = db_path
        self.closed = False
        _FakeStore.instances.append(str(db_path))

    def close(self):
        self.closed = True


class _RaisingStoreFactory:
    def __call__(self, db_path):
        raise OSError("disk full")


class TestCheckDbOpen:
    """check_db_open は「読み取り専用の診断」という契約を守るため、DB ファイルが
    まだ存在しない場合は store_factory を一切呼ばない(=新規スキーマ作成やWAL化を
    引き起こさない)。存在する場合のみ開いて close する(レビュー指摘 must-1 対応)。
    """

    def test_ok_true_and_closes_store_when_db_file_already_exists(self, tmp_path):
        db_path = tmp_path / "shelf.db"
        db_path.touch()
        opened: list[_FakeStore] = []

        def store_factory(path):
            store = _FakeStore(path)
            opened.append(store)
            return store

        result = check_db_open(db_path, store_factory=store_factory)

        assert result.name == "db_open"
        assert result.ok is True
        assert opened[0].closed is True

    def test_ok_false_with_exception_type_name_when_open_fails(self, tmp_path):
        db_path = tmp_path / "shelf.db"
        db_path.touch()

        result = check_db_open(db_path, store_factory=_RaisingStoreFactory())

        assert result.ok is False
        assert "OSError" in result.detail

    def test_ok_true_and_skips_store_factory_when_db_file_does_not_exist(self, tmp_path):
        db_path = tmp_path / "shelf.db"
        calls: list[str] = []

        def store_factory(path):
            calls.append(str(path))
            return _FakeStore(path)

        result = check_db_open(db_path, store_factory=store_factory)

        assert result.ok is True
        assert calls == []
        assert "未作成" in result.detail

    def test_does_not_create_db_file_when_it_does_not_exist(self, tmp_path):
        """デフォルト経路(store_factory=Store)を実際に通す統合テスト:
        未作成パスに対して呼んでもファイルが新規生成されないことを確認する
        (Store の既定 store_factory を上書きしない = 本番と同じ経路)。
        """
        db_path = tmp_path / "shelf.db"

        result = check_db_open(db_path)

        assert result.ok is True
        assert not db_path.exists()

    def test_opens_real_existing_db_file_via_default_store_factory(self, tmp_path):
        """デフォルト経路(store_factory=Store)で、実際に存在する DB ファイルを
        開いて close できることを確認する統合テスト(本番デフォルト経路の実証)。
        """
        from shelf.store import Store

        db_path = tmp_path / "shelf.db"
        bootstrap = Store(db_path)
        bootstrap.close()
        assert db_path.exists()

        result = check_db_open(db_path)

        assert result.ok is True
        assert result.name == "db_open"


class TestCheckCorpusDir:
    def test_ok_true_when_dir_exists(self, tmp_path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()

        result = check_corpus_dir(corpus_dir)

        assert result.name == "corpus_dir"
        assert result.ok is True

    def test_ok_false_when_dir_missing(self, tmp_path):
        result = check_corpus_dir(tmp_path / "not-here")

        assert result.ok is False


class TestResolveFastembedCacheDir:
    """fastembed.common.utils.define_cache_dir と同じ優先順位（env > 既定の
    tempdir/fastembed_cache）を、fastembed 自体を import せずに再現する。
    """

    def test_uses_env_var_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "custom_cache"))

        result = resolve_fastembed_cache_dir()

        assert result == tmp_path / "custom_cache"

    def test_falls_back_to_tempdir_fastembed_cache_when_unset(self, monkeypatch):
        import tempfile

        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)

        result = resolve_fastembed_cache_dir()

        assert result == Path(tempfile.gettempdir()) / "fastembed_cache"


class TestCheckFastembedCache:
    """モデルロードはしない = ディレクトリの存在有無を報告するだけの情報提供
    チェックとする（初回起動時は未作成が正常であり、失敗として扱わない）。
    """

    def test_ok_true_when_dir_exists(self, tmp_path):
        cache_dir = tmp_path / "fastembed_cache"
        cache_dir.mkdir()

        result = check_fastembed_cache(cache_dir)

        assert result.name == "fastembed_cache"
        assert result.ok is True

    def test_ok_true_even_when_dir_missing_but_detail_notes_absence(self, tmp_path):
        cache_dir = tmp_path / "not-yet"

        result = check_fastembed_cache(cache_dir)

        assert result.ok is True
        assert "未作成" in result.detail or "見つかりません" in result.detail


class TestRunChecks:
    """run_checks は cli.py が呼ぶ唯一の窓口。実PATH/実ネットワーク/実DBに触れず
    全経路をテストできるよう、9項目それぞれの依存を kwargs で注入できる。
    """

    def _run(self, tmp_path, *, which, reachable, store_factory):
        return run_checks(
            which=which,
            reachable=reachable,
            db_path=tmp_path / "shelf.db",
            store_factory=store_factory,
            corpus_dir=tmp_path / "corpus",
            config_path=tmp_path / "config.env",
            fastembed_cache_dir=tmp_path / "fastembed_cache",
            ollama_url="http://fake:11434",
        )

    def test_returns_nine_checks_in_a_stable_order(self, tmp_path):
        results = self._run(
            tmp_path,
            which=lambda name: True,
            reachable=lambda url: True,
            store_factory=lambda db_path: _FakeStore(db_path),
        )

        assert [r.name for r in results] == [
            "engine:codex",
            "engine:gemini",
            "engine:agy",
            "ollama",
            "db_parent_dir",
            "db_open",
            "corpus_dir",
            "config_env",
            "fastembed_cache",
        ]

    def test_all_ok_when_every_dependency_healthy_and_corpus_dir_exists(self, tmp_path):
        (tmp_path / "corpus").mkdir()

        results = self._run(
            tmp_path,
            which=lambda name: True,
            reachable=lambda url: True,
            store_factory=lambda db_path: _FakeStore(db_path),
        )

        assert all(r.ok for r in results)

    def test_engine_and_ollama_and_corpus_failures_propagate(self, tmp_path):
        """corpus_dir は作らない(存在しない)ままにして、実際に失敗が伝播するかを見る。"""
        results = self._run(
            tmp_path,
            which=lambda name: False,
            reachable=lambda url: False,
            store_factory=lambda db_path: _FakeStore(db_path),
        )

        by_name = {r.name: r for r in results}
        assert by_name["engine:codex"].ok is False
        assert by_name["ollama"].ok is False
        assert by_name["corpus_dir"].ok is False
        # config_env/fastembed_cache は情報提供のみで常に ok=True。
        assert by_name["config_env"].ok is True
        assert by_name["fastembed_cache"].ok is True

    def test_db_open_failure_propagates_from_store_factory(self, tmp_path):
        # check_db_open は DB ファイルが未作成なら store_factory を呼ばない(must-1
        # 対応)ため、store_factory の失敗を実際に伝播させるには先にファイルを
        # 用意しておく必要がある。
        (tmp_path / "shelf.db").touch()

        results = self._run(
            tmp_path,
            which=lambda name: True,
            reachable=lambda url: True,
            store_factory=_RaisingStoreFactory(),
        )

        by_name = {r.name: r for r in results}
        assert by_name["db_open"].ok is False


class TestCheckConfigEnv:
    """config.env は任意設定（無くても既定値で動作する）ため、存在有無を知らせる
    だけで ok=True に固定する（shelf/config.py の parse_config_file と同じ
    フェイルソフト方針）。
    """

    def test_ok_true_and_reports_path_when_exists(self, tmp_path):
        config_path = tmp_path / "config.env"
        config_path.write_text("SHELF_DB_PATH=x\n", encoding="utf-8")

        result = check_config_env(config_path)

        assert result.name == "config_env"
        assert result.ok is True
        assert str(config_path) in result.detail

    def test_ok_true_and_notes_absence_when_missing(self, tmp_path):
        config_path = tmp_path / "config.env"

        result = check_config_env(config_path)

        assert result.ok is True
        assert "未作成" in result.detail or "見つかりません" in result.detail
