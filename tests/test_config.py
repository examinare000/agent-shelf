"""shelf.config の環境変数上書き検証。

各設定値は import 時に環境変数から解決される（recall.config と同じ設計）。
monkeypatch.context() の with ブロック内で env を差し替えて importlib.reload
し、ブロックを抜けたら（env が元に戻った状態で）再度 reload してモジュール状態を
既定値へ戻す。この往復を徹底することで、他のテストへ副作用を漏らさない。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import shelf.config as config


def test_db_path_default_is_under_shelf_package_root():
    assert config.DB_PATH == config.PACKAGE_ROOT / ".catalog" / "shelf.db"


def test_db_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.db"
    with monkeypatch.context() as m:
        m.setenv("SHELF_DB_PATH", str(custom))
        importlib.reload(config)
        assert config.DB_PATH == custom
    importlib.reload(config)


def test_corpus_dir_default_is_under_shelf_package_root():
    assert config.CORPUS_DIR == config.PACKAGE_ROOT / "corpus"


def test_corpus_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "mycorpus"
    with monkeypatch.context() as m:
        m.setenv("SHELF_CORPUS_DIR", str(custom))
        importlib.reload(config)
        assert config.CORPUS_DIR == custom
    importlib.reload(config)


def test_embed_model_default():
    assert config.EMBED_MODEL == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_embed_model_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_EMBED_MODEL", "custom/model")
        importlib.reload(config)
        assert config.EMBED_MODEL == "custom/model"
    importlib.reload(config)


def test_default_backend_default():
    assert config.DEFAULT_BACKEND == "codex"


def test_default_backend_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DEFAULT_BACKEND", "gemini_cli")
        importlib.reload(config)
        assert config.DEFAULT_BACKEND == "gemini_cli"
    importlib.reload(config)


def test_top_k_default():
    assert config.TOP_K == 10


def test_top_k_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_TOP_K", "5")
        importlib.reload(config)
        assert config.TOP_K == 5
    importlib.reload(config)


def test_top_k_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_TOP_K", "abc")
        importlib.reload(config)
        assert config.TOP_K == 10
    importlib.reload(config)


def test_answer_timeout_default():
    assert config.ANSWER_TIMEOUT == 300


def test_answer_timeout_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ANSWER_TIMEOUT", "60")
        importlib.reload(config)
        assert config.ANSWER_TIMEOUT == 60
    importlib.reload(config)


def test_answer_timeout_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ANSWER_TIMEOUT", "not-a-number")
        importlib.reload(config)
        assert config.ANSWER_TIMEOUT == 300
    importlib.reload(config)


def test_deep_dive_default_is_false():
    assert config.DEEP_DIVE is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE"])
def test_deep_dive_env_truthy_values(monkeypatch, value):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DEEP_DIVE", value)
        importlib.reload(config)
        assert config.DEEP_DIVE is True
    importlib.reload(config)


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_deep_dive_env_falsy_values(monkeypatch, value):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DEEP_DIVE", value)
        importlib.reload(config)
        assert config.DEEP_DIVE is False
    importlib.reload(config)


def test_ollama_url_default():
    assert config.OLLAMA_URL == "http://127.0.0.1:11434"


def test_ollama_url_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_OLLAMA_URL", "http://192.168.1.10:11434")
        importlib.reload(config)
        assert config.OLLAMA_URL == "http://192.168.1.10:11434"
    importlib.reload(config)


def test_ollama_model_default():
    assert config.OLLAMA_MODEL == "qwen3:8b"


def test_ollama_model_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_OLLAMA_MODEL", "llama3:70b")
        importlib.reload(config)
        assert config.OLLAMA_MODEL == "llama3:70b"
    importlib.reload(config)


def test_router_backend_default_is_empty():
    # 未設定時は空文字列。service 側で `config.ROUTER_BACKEND or DEFAULT_BACKEND` として
    # 解決される前提（設計書 §6-D）のため、既定は「未指定」を表す空文字列にする。
    assert config.ROUTER_BACKEND == ""


def test_router_backend_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ROUTER_BACKEND", "ollama")
        importlib.reload(config)
        assert config.ROUTER_BACKEND == "ollama"
    importlib.reload(config)


def test_route_top_n_default_is_one():
    assert config.ROUTE_TOP_N == 1


def test_route_top_n_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ROUTE_TOP_N", "2")
        importlib.reload(config)
        assert config.ROUTE_TOP_N == 2
    importlib.reload(config)


def test_route_top_n_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ROUTE_TOP_N", "abc")
        importlib.reload(config)
        assert config.ROUTE_TOP_N == 1
    importlib.reload(config)


def test_route_fallback_default_is_empty():
    # 空文字列 = routing.FALLBACK_ALL("all") と一致しないため、apply_fallback は
    # 保守的な「対象ゼロ」分岐を取る（設計書 §6-C 分岐3の既定）。
    assert config.ROUTE_FALLBACK == ""


def test_route_fallback_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_ROUTE_FALLBACK", "all")
        importlib.reload(config)
        assert config.ROUTE_FALLBACK == "all"
    importlib.reload(config)


def test_digest_max_notes_default_matches_reduce_phase_default():
    # map-reduce パイプライン化(digests.build_reduce_prompt の既定 max_notes=20)に伴い、
    # reduce 後に1資料あたり保持する学びノート数の既定上限を単発生成時代の5から20へ
    # 拡大した(旧 digests.DIGEST_DEFAULT_MAX_NOTES=5 は単発生成向けの控えめな値だった)。
    assert config.DIGEST_MAX_NOTES == 20


def test_digest_max_notes_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAX_NOTES", "3")
        importlib.reload(config)
        assert config.DIGEST_MAX_NOTES == 3
    importlib.reload(config)


def test_digest_max_notes_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAX_NOTES", "not-a-number")
        importlib.reload(config)
        assert config.DIGEST_MAX_NOTES == 20
    importlib.reload(config)


def test_digest_map_notes_default_is_five():
    # map フェーズは1ウィンドウあたりの上限であり、reduce 後の DIGEST_MAX_NOTES(20)
    # とは独立した控えめな既定値にする(1ウィンドウから20件も学びが出るのは過剰)。
    assert config.DIGEST_MAP_NOTES == 5


def test_digest_map_notes_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAP_NOTES", "3")
        importlib.reload(config)
        assert config.DIGEST_MAP_NOTES == 3
    importlib.reload(config)


def test_digest_map_notes_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAP_NOTES", "abc")
        importlib.reload(config)
        assert config.DIGEST_MAP_NOTES == 5
    importlib.reload(config)


def test_digest_map_window_chars_default_matches_digests_module_default():
    # digests.WINDOW_DEFAULT_CHARS(8000)と矛盾しない既定値であることをここで固定する。
    assert config.DIGEST_MAP_WINDOW_CHARS == 8000


def test_digest_map_window_chars_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAP_WINDOW_CHARS", "4000")
        importlib.reload(config)
        assert config.DIGEST_MAP_WINDOW_CHARS == 4000
    importlib.reload(config)


def test_digest_map_window_chars_invalid_value_falls_back_to_default(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_MAP_WINDOW_CHARS", "abc")
        importlib.reload(config)
        assert config.DIGEST_MAP_WINDOW_CHARS == 8000
    importlib.reload(config)


def test_digest_backend_default_is_empty_string():
    # 空文字列 = 未指定。ROUTER_BACKEND と同じ「空=呼び出し側(service.py)が
    # notebook backend へフォールバックする」流儀。
    assert config.DIGEST_BACKEND == ""


def test_digest_backend_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_DIGEST_BACKEND", "ollama")
        importlib.reload(config)
        assert config.DIGEST_BACKEND == "ollama"
    importlib.reload(config)


def test_shelve_backend_default_is_ollama():
    # §13.1 決定6: 要約・分類推論と新規 notebook の backend 列は SHELVE_BACKEND に集約し、
    # 全体既定 codex（クラウド）ではなくローカル ollama を既定にする（実効ctx小・課金回避）。
    assert config.SHELVE_BACKEND == "ollama"


def test_shelve_backend_env_override(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SHELF_SHELVE_BACKEND", "codex")
        importlib.reload(config)
        assert config.SHELVE_BACKEND == "codex"
    importlib.reload(config)


def test_hybrid_search_default_is_true():
    # ベクトル検索単体では拾えないキーワード一致を取りこぼさないよう、既定で
    # ベクトル＋FTS5キーワードのハイブリッド検索を有効にする（brief既定値）。
    assert config.HYBRID_SEARCH is True


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE"])
def test_hybrid_search_env_truthy_values(monkeypatch, value):
    with monkeypatch.context() as m:
        m.setenv("SHELF_HYBRID_SEARCH", value)
        importlib.reload(config)
        assert config.HYBRID_SEARCH is True
    importlib.reload(config)


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_hybrid_search_env_falsy_values(monkeypatch, value):
    with monkeypatch.context() as m:
        m.setenv("SHELF_HYBRID_SEARCH", value)
        importlib.reload(config)
        assert config.HYBRID_SEARCH is False
    importlib.reload(config)


class TestSessionWideConfigIsolation:
    """tests/conftest.py の autouse フィクスチャによる SHELF_CONFIG 固定を検証する。

    開発者の実 ~/.config/agent-shelf/config.env が既定値アサーションを汚染しない
    よう、テストセッション全体で実在しない一時パスに固定されている前提を保証する。
    """

    def test_shelf_config_env_is_pinned_to_nonexistent_temp_path(self):
        import os

        configured = os.environ.get("SHELF_CONFIG")
        assert configured is not None
        pinned_path = Path(configured)
        assert not pinned_path.exists()
        assert pinned_path != Path.home() / ".config" / "agent-shelf" / "config.env"


class TestConfigFileLoader:
    """~/.config/agent-shelf/config.env（SHELF_CONFIG で上書き可）の読み込み。

    優先順位はプロセス環境変数 > config.env > ハードコード既定（設計書の
    「プロセス環境変数が常に優先」要求）。setdefault ベースで適用するため、
    この優先順位は自然に成り立つ（既に os.environ にあるキーは上書きしない）。
    """

    def test_resolve_config_path_defaults_to_home_dot_config(self, monkeypatch):
        with monkeypatch.context() as m:
            m.delenv("SHELF_CONFIG", raising=False)
            importlib.reload(config)
            assert config.resolve_config_path() == (
                Path.home() / ".config" / "agent-shelf" / "config.env"
            )
        importlib.reload(config)

    def test_resolve_config_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.env"
        with monkeypatch.context() as m:
            m.setenv("SHELF_CONFIG", str(custom))
            importlib.reload(config)
            assert config.resolve_config_path() == custom
        importlib.reload(config)

    def test_parse_config_file_returns_empty_dict_when_file_missing(self, tmp_path):
        missing = tmp_path / "does-not-exist.env"
        assert config.parse_config_file(missing) == {}

    def test_parse_config_file_reads_key_value_lines(self, tmp_path):
        path = tmp_path / "config.env"
        path.write_text("SHELF_TOP_K=7\nSHELF_DEFAULT_BACKEND=ollama\n", encoding="utf-8")
        assert config.parse_config_file(path) == {
            "SHELF_TOP_K": "7",
            "SHELF_DEFAULT_BACKEND": "ollama",
        }

    def test_parse_config_file_ignores_comments_and_blank_lines(self, tmp_path):
        path = tmp_path / "config.env"
        path.write_text(
            "# comment\n\nSHELF_TOP_K=7\n   \n# another comment\nSHELF_OLLAMA_MODEL=llama3\n",
            encoding="utf-8",
        )
        assert config.parse_config_file(path) == {
            "SHELF_TOP_K": "7",
            "SHELF_OLLAMA_MODEL": "llama3",
        }

    def test_config_file_value_applied_when_process_env_not_set(self, monkeypatch, tmp_path):
        config_file = tmp_path / "config.env"
        config_file.write_text("SHELF_TOP_K=7\n", encoding="utf-8")
        with monkeypatch.context() as m:
            m.delenv("SHELF_TOP_K", raising=False)
            m.setenv("SHELF_CONFIG", str(config_file))
            importlib.reload(config)
            assert config.TOP_K == 7
        importlib.reload(config)

    def test_process_env_overrides_config_file_value(self, monkeypatch, tmp_path):
        config_file = tmp_path / "config.env"
        config_file.write_text("SHELF_TOP_K=7\n", encoding="utf-8")
        with monkeypatch.context() as m:
            m.setenv("SHELF_TOP_K", "3")
            m.setenv("SHELF_CONFIG", str(config_file))
            importlib.reload(config)
            assert config.TOP_K == 3
        importlib.reload(config)

    def test_missing_config_file_falls_back_to_hardcoded_default(self, monkeypatch, tmp_path):
        with monkeypatch.context() as m:
            m.delenv("SHELF_TOP_K", raising=False)
            m.setenv("SHELF_CONFIG", str(tmp_path / "does-not-exist.env"))
            importlib.reload(config)
            assert config.TOP_K == 10
        importlib.reload(config)
