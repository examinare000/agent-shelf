"""自動分類投入（`shelf shelve`）のオーケストレーション配線層（設計書 §13.1/§13.3）。

shelving.py が持つ「判断」（プロンプト構成・structured 出力パース・幻覚除去・
名前フォールバック・増分カタログ更新）はそのまま流用し、Shelver は「ファイルごとに
1 回の AnswerBackend structured 呼び出し」という配線だけを担う薄いクラスにする
（librarian.py の Librarian と厳密に同型・設計書 §13.1 決定 1）。store・sqlite3・
subprocess・config を一切知らないことで、FakeAnswerBackend だけで plan() の
全経路（成功・増分スレッディング・backend 呼び出し失敗）を単体テストできる
（設計書 §3 依存方向・§13.9 テスト戦略）。
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from shelf.ports import AnswerBackend, FileSummary, NotebookCard, ShelvePlan
from shelf.shelving import (
    CLASSIFY_SCHEMA,
    build_classification_prompt,
    classify_step,
    parse_classification,
)


class Shelver:
    """自動分類投入の薄い配線クラス（設計書 §13.1 決定 1）。

    依存は AnswerBackend ポートと shelving.py（純粋関数）のみ。カタログは
    NotebookCard のリストとして呼び出し側（service.py）から渡され、Shelver
    自身は store に一切触れない（Librarian と同一の循環遮断境界・設計書 §13.3）。

    notebook_backend は新規作成 notebook の backend 列に使う値
    （config.SHELVE_BACKEND 由来・設計書 §13.1 決定 6）。shelving.py 自体は
    config を知らない純粋関数のままにするため、Shelver がコンストラクタで
    受け取り、ファイルごとに classify_step へ都度渡す。
    """

    def __init__(
        self,
        backend: AnswerBackend,
        *,
        workdir: Path,
        notebook_backend: str,
    ) -> None:
        self._backend = backend
        self._workdir = workdir
        self._notebook_backend = notebook_backend

    def plan(
        self, summaries: Sequence[FileSummary], catalog: Sequence[NotebookCard]
    ) -> ShelvePlan:
        """要約群 + 初期カタログから分類計画を組み立てる（設計書 §13.1 決定 1/§13.6）。

        working カタログ（catalog のコピー）をループの外で保持し、新規 notebook が
        作られるたびに追記する（増分スレッディング・設計書 §13.5）。これにより
        2 番目以降のファイルのプロンプトには直前までに作成された notebook が
        反映され、モデルは同一主題を新設せず assign を選べる。呼び出し元の
        catalog は複製元のまま変更しない（list(catalog) でコピーを起こす）。

        backend.answer() 自体の失敗（RawAnswer.ok=False・例外）のみ plan.errors に
        落とし、当該ファイルをスキップして処理を継続する（失敗継続）。parse 失敗は
        shelving.classify_step が「新規作成への再解釈」で必ず吸収し StepResult は
        常に確定した assignment を返すため、ここでは分岐しない（設計書 §13.9）。
        """
        working_catalog = list(catalog)
        result = ShelvePlan()

        for summary in summaries:
            prompt = build_classification_prompt(summary, working_catalog)
            try:
                raw = self._backend.answer(prompt, workdir=self._workdir, schema=CLASSIFY_SCHEMA)
            except Exception as exc:  # noqa: BLE001 - backend 境界の例外を継続可能なエラーへ変換する
                result.errors.append({"origin": summary.origin, "error": str(exc)})
                continue

            if not raw.ok:
                result.errors.append(
                    {"origin": summary.origin, "error": raw.error or "分類の推論に失敗しました"}
                )
                continue

            decision = parse_classification(raw.text)
            step = classify_step(decision, summary, working_catalog, self._notebook_backend)

            result.assignments.append(step.assignment)
            if step.new_notebook is not None:
                result.created.append(step.new_notebook)
                working_catalog.append(
                    NotebookCard(
                        name=step.new_notebook.name,
                        description=step.new_notebook.description,
                        persona=None,
                        doc_count=0,
                    )
                )

        return result
