# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-17

### Added
- **Digest map-reduce パイプライン**: 大規模資料から効率的に学びノートを抽出するため、単発 LLM 呼び出しから分割-集約パイプラインへ移行。ウィンドウ単位での抽出（map）→ 文書全体での統合・厳選（reduce）で精度と網羅性を向上
  - ウィンドウサイズ `SHELF_DIGEST_MAP_WINDOW_CHARS`（既定8000字）
  - Map フェーズ出力上限 `SHELF_DIGEST_MAP_NOTES`（既定5）
  - Reduce フェーズ出力上限 `SHELF_DIGEST_MAX_NOTES` を既定 5 から 20 に拡大
- **Digest 専用バックエンド**: `SHELF_DIGEST_BACKEND` で map-reduce フェーズを低コスト LLM（ローカル ollama など）へ逃がす機能
- **ハイブリッド検索**: cosine ベクトル検索 + FTS5 trigram BM25 の RRF 統合。日本語自然文の検索精度を向上。`SHELF_HYBRID_SEARCH` で制御（既定true）
- **ドキュメントタグ**: Digest 時に自動付与されるタグ（既存カタログから選別、NFKC/lower 正規化）。司書の情報源選別精度を向上
- **学ぶノート根拠接地**: 各 study_note に source_chunk_ids / section / page が記録され、回答の根拠トレーサビリティが向上
- **Insights レスポンスの強化**: ask/consult の各 insight エントリに section/page が付与（検索結果の位置情報が明示化）

### Changed
- **`SHELF_DIGEST_MAX_NOTES` 既定値**: 5 → 20 に拡大（文書全体の学びノート保有量を増加）
- **粒度プリセットの調整**: GRANULARITY_PRESETS で digest_max_notes の目盛りを 3/5/10 → 10/20/40 に更新（新既定20ベース）

### Removed
- **環境変数 `SHELF_DIGEST_INPUT_MAX_CHARS` の廃止**: 旧単発パイプライン専用の設定を削除。新パイプラインでは `SHELF_DIGEST_MAP_WINDOW_CHARS` で制御

### Migration
- DB スキーマは自動マイグレーション（Store 初期化時に冪等実行）
- 既存の旧世代学びノート（pipeline=1）は自動認識され、新パイプラインでの再生成対象に
- 既存デプロイの移行手順: デプロイ → サーバ再起動 → notebook ごとに `shelf digest <nb>` → `shelf ask`/`shelf consult` でスモーク確認

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
