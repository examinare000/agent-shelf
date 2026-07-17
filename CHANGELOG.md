# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-07-17

### Added
- Initial public release of agent-shelf
- Local-first RAG MCP server with FastEmbed + SQLite index over curated documents
- Pluggable LLM engine abstraction supporting Codex, Gemini, Anthropic, and Ollama
- Librarian (司書) routing to select optimal notebook sources from multi-document queries
- Hybrid RAG synthesis: multi-engine template-based answer generation
- CLI ingest pipeline: notebook creation, document ingestion, index building
- Masking layer for sensitive data extraction via configurable `distill/extract.py`
- Environment variable config for database path, corpus directory, embedding model, timeouts, and router backends
- Comprehensive test suite with pytest and ruff code quality checks
- Python 3.11+ support with `uv` package manager

### Features
- **ローカルベース検索**: 従量 API 不使用、FastEmbed + SQLite で実装
- **エンジン抽象**: Codex（既定）・Gemini・Anthropic・Ollama（ローカル）をプラグイン可能に
- **ハイブリッド RAG**: 複数 LLM に検索結果を同時投入し、バックエンド毎に回答を合成
- **司書（Librarian）**: 複数 notebook から最適な情報源を自動選別
- **機微マスク**: 出力前に機微情報を削除（recall との連携対応）
