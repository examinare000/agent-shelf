"""司書オーケストレーションの配線層（設計書 §6-D）。

routing.py が持つ「判断」（プロンプト構成・structured 出力パース・幻覚除去・
top-N クランプ・フォールバック）はそのまま流用し、Librarian は「1 回の
AnswerBackend structured 呼び出し」という配線だけを担う薄いクラスにする。
store・corpus・sqlite3・subprocess・fastembed を一切知らないことで、
FakeAnswerBackend だけで route() の全経路（成功・パース失敗・backend 呼び出し
失敗）を単体テストできる（設計書 §3 依存方向・§9-A テスト戦略）。
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from shelf.ports import AnswerBackend, NotebookCard, RouteTarget, RoutingDecision
from shelf.routing import ROUTING_SCHEMA, apply_fallback, build_routing_prompt, parse_routing


class Librarian:
    """司書ルーティングの薄い配線クラス（設計書 §6-D）。

    依存は AnswerBackend ポートと routing.py（純粋関数）のみ。カタログは
    NotebookCard のリストとして呼び出し側（service.py）から渡され、Librarian
    自身は store に一切触れない（Librarian と store の循環依存を断つための境界。
    設計書 §3「カタログは service が組み立てて Librarian に渡す」）。
    """

    def __init__(
        self,
        backend: AnswerBackend,
        *,
        workdir: Path,
        top_n: int,
        fallback: str,
    ) -> None:
        self._backend = backend
        self._workdir = workdir
        self._top_n = top_n
        self._fallback = fallback

    def route(self, question: str, catalog: Sequence[NotebookCard]) -> list[RouteTarget]:
        """質問 + カタログから最終的なルーティング対象を返す（設計書 §6-D）。

        backend 呼び出し失敗（RawAnswer.ok=False）はエラーで潰さず、パース失敗
        （parse_ok=False）と同じフォールバック経路（routing.apply_fallback の
        分岐3: 「parse_ok=false または targets 空」）へ流す。answerable=True を
        立てるのは、answerable=False 分岐（専門家を絶対に呼ばない・分岐2）ではなく
        fallback 設定（既定 conservative=対象ゼロ／all=カタログ横断）に判断を
        委ねるため。これにより backend の一時的な不調が例外として呼び出し元へ
        伝播せず、常に安全側（既定は対象ゼロ）へ倒れる。
        """
        prompt = build_routing_prompt(question, catalog)
        raw = self._backend.answer(prompt, workdir=self._workdir, schema=ROUTING_SCHEMA)

        if raw.ok:
            decision = parse_routing(raw.text)
        else:
            decision = RoutingDecision(answerable=True, parse_ok=False, targets=[])

        return apply_fallback(decision, catalog, question, self._top_n, self._fallback)
