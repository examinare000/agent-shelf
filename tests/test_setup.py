"""shelf setup（対話式 config.env 生成）の純粋関数・非対話経路のテスト。

対話プロンプト自体（input() 呼び出し）は collect_answers_interactively に
input_func/print_func を注入できるようにし、シナリオ台本（決め打ちの回答列）で
検証する。実 ollama デーモン・実 CLI・実ネットワークには一切触れない
（is_reachable/is_command_available をモンキーパッチで差し替える）。
"""
from __future__ import annotations

import json

import pytest

from shelf import config, setup


class TestDefaultAnswers:
    """--yes（非対話・全既定値）が使う既定回答一式。config.py のハードコード既定と
    一致させることで、「--yes は今日の既定値をそのまま config.env に明示化するだけ」
    という設計を保つ（config.env が存在しない状態と機能的に等価な内容になる）。
    """

    def test_matches_config_module_defaults(self):
        answers = setup.default_answers()

        assert answers["use_ollama"] is True
        assert answers["ollama_url"] == config.OLLAMA_URL
        assert answers["ollama_model"] == config.OLLAMA_MODEL
        assert answers["provider"] == config.DEFAULT_BACKEND
        assert answers["router_backend"] == config.ROUTER_BACKEND
        assert answers["granularity"] == "standard"
        assert answers["digest_max_notes"] is None
        assert answers["digest_input_max_chars"] is None
        assert answers["top_k"] is None
        assert answers["corpus_dir"] == str(config.CORPUS_DIR)
        assert answers["db_path"] == str(config.DB_PATH)


class TestResolveGranularity:
    @pytest.mark.parametrize(
        "preset, expected",
        [
            ("coarse", {"digest_max_notes": 3, "digest_input_max_chars": 2000, "top_k": 5}),
            ("standard", {"digest_max_notes": 5, "digest_input_max_chars": 4000, "top_k": 10}),
            ("fine", {"digest_max_notes": 10, "digest_input_max_chars": 8000, "top_k": 20}),
        ],
    )
    def test_preset_values(self, preset, expected):
        answers = {**setup.default_answers(), "granularity": preset}
        assert setup.resolve_granularity(answers) == expected

    def test_unknown_preset_falls_back_to_standard(self):
        answers = {**setup.default_answers(), "granularity": "not-a-real-preset"}
        assert setup.resolve_granularity(answers) == {
            "digest_max_notes": 5,
            "digest_input_max_chars": 4000,
            "top_k": 10,
        }

    def test_explicit_overrides_win_over_preset(self):
        answers = {
            **setup.default_answers(),
            "granularity": "coarse",
            "digest_max_notes": 99,
        }
        resolved = setup.resolve_granularity(answers)
        assert resolved["digest_max_notes"] == 99
        # 明示指定していないキーはプリセット値のまま
        assert resolved["digest_input_max_chars"] == 2000
        assert resolved["top_k"] == 5


class TestResolveShelveBackend:
    def test_uses_ollama_when_use_ollama_is_true(self):
        answers = {**setup.default_answers(), "use_ollama": True, "provider": "codex"}
        assert setup.resolve_shelve_backend(answers) == "ollama"

    def test_falls_back_to_provider_when_use_ollama_is_false(self):
        answers = {**setup.default_answers(), "use_ollama": False, "provider": "gemini"}
        assert setup.resolve_shelve_backend(answers) == "gemini"


class TestAnswersToConfigValues:
    def test_default_answers_produce_config_matching_hardcoded_defaults(self):
        values = setup.answers_to_config_values(setup.default_answers())

        assert values == {
            "SHELF_OLLAMA_URL": config.OLLAMA_URL,
            "SHELF_OLLAMA_MODEL": config.OLLAMA_MODEL,
            "SHELF_SHELVE_BACKEND": "ollama",
            "SHELF_DEFAULT_BACKEND": config.DEFAULT_BACKEND,
            "SHELF_ROUTER_BACKEND": config.ROUTER_BACKEND,
            "SHELF_DIGEST_MAX_NOTES": "5",
            "SHELF_DIGEST_INPUT_MAX_CHARS": "4000",
            "SHELF_TOP_K": "10",
            "SHELF_CORPUS_DIR": str(config.CORPUS_DIR),
            "SHELF_DB_PATH": str(config.DB_PATH),
        }

    def test_custom_answers_are_reflected(self):
        answers = {
            **setup.default_answers(),
            "use_ollama": False,
            "provider": "gemini",
            "router_backend": "ollama",
            "granularity": "fine",
            "corpus_dir": "/tmp/corpus",
            "db_path": "/tmp/shelf.db",
        }

        values = setup.answers_to_config_values(answers)

        assert values["SHELF_SHELVE_BACKEND"] == "gemini"
        assert values["SHELF_DEFAULT_BACKEND"] == "gemini"
        assert values["SHELF_ROUTER_BACKEND"] == "ollama"
        assert values["SHELF_DIGEST_MAX_NOTES"] == "10"
        assert values["SHELF_TOP_K"] == "20"
        assert values["SHELF_CORPUS_DIR"] == "/tmp/corpus"
        assert values["SHELF_DB_PATH"] == "/tmp/shelf.db"


class TestBuildConfigEnvText:
    def test_produces_key_value_lines_for_every_value(self):
        values = {"SHELF_TOP_K": "10", "SHELF_DEFAULT_BACKEND": "codex"}
        text = setup.build_config_env_text(values)

        assert "SHELF_TOP_K=10" in text
        assert "SHELF_DEFAULT_BACKEND=codex" in text

    def test_output_is_parseable_by_config_parse_config_file(self, tmp_path):
        values = setup.answers_to_config_values(setup.default_answers())
        text = setup.build_config_env_text(values)
        path = tmp_path / "config.env"
        path.write_text(text, encoding="utf-8")

        assert config.parse_config_file(path) == values


class TestLoadAnswersFile:
    def test_missing_keys_fall_back_to_defaults(self, tmp_path):
        path = tmp_path / "answers.json"
        path.write_text(json.dumps({"provider": "gemini"}), encoding="utf-8")

        answers = setup.load_answers_file(path)

        assert answers["provider"] == "gemini"
        assert answers["use_ollama"] == setup.default_answers()["use_ollama"]
        assert answers["granularity"] == setup.default_answers()["granularity"]

    def test_full_answers_file_is_used_verbatim(self, tmp_path):
        full = {
            "use_ollama": False,
            "ollama_url": "http://192.168.1.5:11434",
            "ollama_model": "llama3:70b",
            "provider": "agy",
            "router_backend": "codex",
            "granularity": "custom",
            "digest_max_notes": 7,
            "digest_input_max_chars": 1234,
            "top_k": 15,
            "corpus_dir": "/data/corpus",
            "db_path": "/data/shelf.db",
        }
        path = tmp_path / "answers.json"
        path.write_text(json.dumps(full), encoding="utf-8")

        assert setup.load_answers_file(path) == full


class TestWriteConfigEnv:
    def test_creates_parent_directories_and_writes_text(self, tmp_path):
        target = tmp_path / "nested" / "agent-shelf" / "config.env"

        setup.write_config_env(target, "SHELF_TOP_K=10\n")

        assert target.read_text(encoding="utf-8") == "SHELF_TOP_K=10\n"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "config.env"
        target.write_text("old content", encoding="utf-8")

        setup.write_config_env(target, "SHELF_TOP_K=5\n")

        assert target.read_text(encoding="utf-8") == "SHELF_TOP_K=5\n"


class TestDetectOllama:
    """detect_ollama は HTTP 疎通 OR `ollama` コマンド存在のどちらかで真になる。"""

    def test_true_when_http_reachable(self, monkeypatch):
        monkeypatch.setattr(setup, "is_reachable", lambda url, timeout=1.0: True)
        monkeypatch.setattr(setup, "is_command_available", lambda name: False)

        assert setup.detect_ollama("http://127.0.0.1:11434") is True

    def test_true_when_command_available_but_http_unreachable(self, monkeypatch):
        monkeypatch.setattr(setup, "is_reachable", lambda url, timeout=1.0: False)
        monkeypatch.setattr(setup, "is_command_available", lambda name: True)

        assert setup.detect_ollama("http://127.0.0.1:11434") is True

    def test_false_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(setup, "is_reachable", lambda url, timeout=1.0: False)
        monkeypatch.setattr(setup, "is_command_available", lambda name: False)

        assert setup.detect_ollama("http://127.0.0.1:11434") is False


class TestCollectAnswersInteractively:
    """input_func に決め打ちの回答列を注入し、実 stdin には触れずに検証する。"""

    def test_all_enter_accepts_all_defaults(self, monkeypatch):
        monkeypatch.setattr(setup, "is_reachable", lambda url, timeout=1.0: True)
        monkeypatch.setattr(setup, "is_command_available", lambda name: True)
        scripted = iter([""] * 20)  # 何を聞かれても Enter（既定値採用）
        answers = setup.collect_answers_interactively(
            input_func=lambda _prompt: next(scripted), print_func=lambda *a, **k: None
        )

        assert answers == setup.default_answers()

    def test_custom_entries_override_defaults(self, monkeypatch):
        monkeypatch.setattr(setup, "is_reachable", lambda url, timeout=1.0: False)
        monkeypatch.setattr(setup, "is_command_available", lambda name: False)
        scripted = iter(
            [
                "n",  # ollama を使うか -> いいえ
                "gemini",  # provider
                "",  # router_backend(既定=同じ)
                "fine",  # granularity
                "/tmp/mycorpus",  # corpus_dir
                "/tmp/my.db",  # db_path
            ]
        )
        answers = setup.collect_answers_interactively(
            input_func=lambda _prompt: next(scripted), print_func=lambda *a, **k: None
        )

        assert answers["use_ollama"] is False
        assert answers["provider"] == "gemini"
        assert answers["router_backend"] == ""
        assert answers["granularity"] == "fine"
        assert answers["corpus_dir"] == "/tmp/mycorpus"
        assert answers["db_path"] == "/tmp/my.db"
