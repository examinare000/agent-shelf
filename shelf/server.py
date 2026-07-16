"""MCP ツール ask / list_notebooks / consult の薄いラッパ。

ロジックは一切持たず ShelfService へ委譲する。Claude が明示的にツールを呼んだ時
だけ実行されるため、フックによる自動注入と違って受動的なトークンコストがゼロに
なる（design doc §0 のスコープ方針）。公開ツールはこの3つ: 資料投入・notebook
作成/削除は人間操作の CLI（cli.py）に閉じ、生チャンクを返す search は追加しない
（design doc §4-C「なぜ add_source/search を MCP に公開しないか」）。consult は
ルーティングという新しい capability であり、クライアント側では list_notebooks
と ask の組み合わせでは実現できないため、サーバ側の1ツールとして公開する
（design doc §5-D）。
"""
from __future__ import annotations

from collections.abc import Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from shelf.service import ShelfService


def build_transport_security(allowed_hosts: Sequence[str]) -> TransportSecuritySettings:
    """許可ホスト一覧から TransportSecuritySettings を組み立てる。

    cli.py は import ガード（mcp を import してよいのは server.py のみ、design doc
    §3/§6）に抵触するため TransportSecuritySettings を直接構築できない。streamable-http
    でクロスデバイス接続する際の Host/Origin 許可リスト構築窓口をこの関数に集約する。

    allowed_origins は allowed_hosts の各エントリを "http://<entry>" 形式にしたもの
    （呼び出し元が2つのリストを別々に管理せず、1つの許可ホスト一覧から導出できる
    ようにする）。DNS リバインディング保護自体は無効化しない
    （enable_dns_rebinding_protection=True を明示: VPN 境界内でもブラウザ経由攻撃の
    緩和として保持する価値があるため。design doc §1「クロスデバイス接続」）。
    """
    hosts = list(allowed_hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )


def create_server(service: ShelfService) -> FastMCP:
    mcp = FastMCP("shelf")

    @mcp.tool()
    def ask(notebook: str, question: str) -> dict:
        """指定した notebook（書籍・資料コーパス）に質問し、根拠付きの回答を得る。

        生の資料本文は返さず、エンジンが合成した回答テキストと軽量な引用情報
        (source/section/page/quote) だけを返す。利用可能な notebook は
        list_notebooks で確認できる。
        """
        return service.ask(notebook, question)

    @mcp.tool()
    def list_notebooks() -> list[dict]:
        """利用可能な notebook（資料コーパス）の一覧を返す。ask を呼ぶ前の discovery に使う。"""
        return service.list_notebooks()

    @mcp.tool()
    def consult(question: str) -> dict:
        """notebook を指定せず質問すると、司書が適切な notebook（専門家）を選んで回答を集める。

        どの notebook に問うべきかあらかじめ分かっている場合は ask を使う。返却には
        「どの notebook から回答を得たか」と「抜粋（citations）と学び（insights）の分離」を含む。
        """
        return service.consult(question)

    return mcp
