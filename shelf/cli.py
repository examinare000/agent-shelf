"""shelf コマンドのエントリポイント。実 Store/FastEmbedEmbedder/backend/converter/mask を
組み立てて ShelfService へ注入する。

引数解析(build_parser)はネットワーク・DB非依存の純粋ロジックなので単体テスト対象。
各サブコマンドの実行(main)は実際の SQLite ファイル・ONNX モデル・サブスク CLI に
触れるため、単体テストでは検証せずスモークテストで確認する(recall/recall/cli.py・
design doc §6 と同じ理由)。

notebook 作成・資料投入・削除を MCP に公開せず CLI(人間操作)へ限定するのが
design doc §4-C の設計判断であり、本ファイルがその受け皿になる。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from shelf import config, setup
from shelf.server import build_transport_security, create_server
from shelf.service import ShelfService
from shelf.store import Store, UnknownNotebookError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelf", description="書籍・資料コーパスへの委譲QA")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="MCP サーバを起動する(既定 stdio)")
    serve_parser.add_argument(
        "--http",
        action="store_true",
        help="streamable-http トランスポートで起動する(既定は stdio)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="--http 指定時の bind ホスト(既定 127.0.0.1。Tailscale 内 bind 前提・"
        "認証は VPN 境界に委ねる)",
    )
    serve_parser.add_argument(
        "--port", type=int, default=8765, help="--http 指定時の bind ポート(既定 8765)"
    )
    serve_parser.add_argument(
        "--allowed-host",
        dest="allowed_host",
        action="append",
        default=None,
        help="DNS リバインディング保護の許可 Host を追加指定する(繰り返し指定可)。"
        "既定では bind 先(host:port と host)のみ許可される。Tailscale MagicDNS 名"
        "(例 avalon.tailXXXX.ts.net:8765)経由でアクセスする場合に指定する",
    )

    ls_parser = sub.add_parser("ls", help="notebook 一覧、または指定時は document 一覧")
    ls_parser.add_argument("notebook", nargs="?", default=None)

    new_parser = sub.add_parser("new", help="notebook を作成する")
    new_parser.add_argument("notebook")
    new_parser.add_argument("--desc", dest="description", default=None, help="notebook の説明")
    new_parser.add_argument(
        "--backend",
        choices=["codex", "gemini", "agy", "ollama"],
        default=None,
        help="使用するエンジン",
    )

    add_parser = sub.add_parser("add", help="資料(ファイルパス・ディレクトリ・URL)を投入する")
    add_parser.add_argument("notebook")
    add_parser.add_argument("origin", help="ファイルパス・ディレクトリ・URL")
    add_parser.add_argument(
        "--desc", dest="description", default=None, help="資料の説明（省略時は codex で自動生成）"
    )
    add_parser.add_argument(
        "--no-summary", action="store_true", help="説明の自動生成を行わない"
    )

    rm_parser = sub.add_parser("rm", help="notebook全体、または指定documentを削除する")
    rm_parser.add_argument("notebook")
    rm_parser.add_argument("--doc", dest="doc_id", default=None, help="削除するdocument ID")
    rm_parser.add_argument("--yes", action="store_true", help="確認プロンプトをスキップする")

    index_parser = sub.add_parser("index", help="notebookを索引化する")
    index_parser.add_argument("notebook")
    index_parser.add_argument(
        "--all", action="store_true", help="状態を無視して全ファイルを再構築する"
    )

    ask_parser = sub.add_parser("ask", help="デバッグ用: notebookに質問する")
    ask_parser.add_argument("notebook")
    ask_parser.add_argument("question")

    consult_parser = sub.add_parser("consult", help="司書がルーティングしてnotebookを選び質問に答える")
    consult_parser.add_argument("question")

    digest_parser = sub.add_parser("digest", help="指定notebookの資料から学びノートを生成する")
    digest_parser.add_argument("notebook")
    digest_parser.add_argument(
        "--doc-id", dest="doc_id", default=None, help="特定のdocumentのみ生成する"
    )
    digest_parser.add_argument(
        "--force", action="store_true", help="既存の学びノートを上書きする"
    )

    shelve_parser = sub.add_parser("shelve", help="ディレクトリから自動分類投入する")
    shelve_parser.add_argument("directory", help="投入対象ディレクトリのパス")
    shelve_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="計画のみを出力し、永続化しない",
    )

    setup_parser = sub.add_parser("setup", help="対話式でbackendの初期設定(config.env)を生成する")
    setup_parser.add_argument(
        "--yes", action="store_true", help="全て既定値で非対話実行する"
    )
    setup_parser.add_argument(
        "--answers-file",
        dest="answers_file",
        default=None,
        help="回答をJSONファイルから注入する(テスト用の非対話経路)",
    )

    persona_parser = sub.add_parser("persona", help="notebookの専門家ペルソナを表示・設定する")
    persona_parser.add_argument("notebook")
    persona_group = persona_parser.add_mutually_exclusive_group()
    persona_group.add_argument(
        "--set", dest="set_persona", default=None, help="ペルソナテキストを設定する"
    )
    persona_group.add_argument(
        "--clear", action="store_true", help="ペルソナをクリアする(None に設定)"
    )

    return parser


def _build_store() -> Store:
    return Store(config.DB_PATH)


def _build_service() -> ShelfService:
    """実部品(Store・FastEmbedEmbedder・backend_factory・convert・mask)を組んだ
    ShelfService を構築する。fastembed 等の重い依存はここで関数内 import に留め、
    build_parser 単体テストがロードしないようにする(recall の作法)。
    """
    from shelf import convert
    from shelf.embedder import FastEmbedEmbedder
    from shelf.engines import create_backend
    from shelf.masking import mask

    store = _build_store()
    embedder = FastEmbedEmbedder(config.EMBED_MODEL)

    def backend_factory(name: str):
        return create_backend(name, timeout=config.ANSWER_TIMEOUT)

    return ShelfService(
        store,
        embedder,
        backend_factory,
        config.CORPUS_DIR,
        default_backend=config.DEFAULT_BACKEND,
        top_k=config.TOP_K,
        deep_dive=config.DEEP_DIVE,
        mask=mask,
        converter=convert,
        router_backend=config.ROUTER_BACKEND,
        route_top_n=config.ROUTE_TOP_N,
        route_fallback=config.ROUTE_FALLBACK,
        digest_max_notes=config.DIGEST_MAX_NOTES,
        digest_input_max_chars=config.DIGEST_INPUT_MAX_CHARS,
        shelve_backend=config.SHELVE_BACKEND,
    )


def _print_notebooks(service: ShelfService) -> None:
    for nb in service.list_notebooks():
        print(
            f"{nb['notebook']}\t{nb['description'] or ''}\tbackend={nb['backend']}\t"
            f"sources={nb['sources']}\tchunks={nb['chunks']}"
        )


def _print_documents(store: Store, notebook: str) -> None:
    for doc in store.list_documents(notebook):
        print(f"{doc['id']}\t{doc['origin']}\t{doc['origin_type']}\t{doc['added_at']}")


def _confirm(prompt: str, skip: bool) -> bool:
    if skip:
        return True
    answer = input(f"{prompt} [y/N]: ")
    return answer.strip().lower() == "y"


def _cmd_setup(args: argparse.Namespace) -> None:
    """--answers-file(テスト用注入) > --yes(全既定値) > 対話式 の優先順位で回答を
    集め、config.env を書き出す。answers-file が最も具体的な指定であるため、
    --yes と同時指定されても answers-file を優先する。
    """
    if args.answers_file is not None:
        answers = setup.load_answers_file(Path(args.answers_file))
    elif args.yes:
        answers = setup.default_answers()
    else:
        answers = setup.collect_answers_interactively()

    values = setup.answers_to_config_values(answers)
    text = setup.build_config_env_text(values)
    path = config.resolve_config_path()
    setup.write_config_env(path, text)
    print(f"設定を書き出しました: {path}")
    print(text)


def _cmd_rm(args: argparse.Namespace) -> None:
    store = _build_store()

    if args.doc_id is not None:
        doc = store.get_document(args.doc_id)
        if doc is None:
            print(f"document が見つかりません: {args.doc_id}")
            return
        # get_document は doc_id をグローバル検索するため、positional の notebook と
        # 対象 document の所属 notebook が食い違っていても削除できてしまっていた
        # (重大指摘#1)。誤った notebook からの削除操作を拒否する。
        if doc["notebook"] != args.notebook:
            print(
                f"document '{args.doc_id}' は notebook '{args.notebook}' に属していません"
                f"(所属: '{doc['notebook']}')"
            )
            return
        print(f"削除対象: document {doc['id']} ({doc['origin']})")
        if not _confirm("削除しますか?", args.yes):
            print("キャンセルしました")
            return
        store.delete_document(args.doc_id)
        (config.CORPUS_DIR / doc["normalized_path"]).unlink(missing_ok=True)
        print(f"削除しました: {args.doc_id}")
        return

    nb = store.get_notebook(args.notebook)
    if nb is None:
        print(f"notebook が見つかりません: {args.notebook}")
        return
    docs = store.list_documents(args.notebook)
    print(f"削除対象: notebook '{args.notebook}'(document {len(docs)}件)")
    if not _confirm("削除しますか?", args.yes):
        print("キャンセルしました")
        return
    store.delete_notebook(args.notebook)
    shutil.rmtree(config.CORPUS_DIR / args.notebook, ignore_errors=True)
    print(f"削除しました: notebook '{args.notebook}'")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        server = create_server(_build_service())
        if args.http:
            # Tailscale VPN 内での bind を前提とし、認証は VPN 境界に委ねる
            # (design doc §1)。エンドポイントは mcp SDK の既定 "/mcp"。
            server.settings.host = args.host
            server.settings.port = args.port
            # mcp SDK の DNS リバインディング保護は既定で localhost 系 Host しか
            # 許可しないため、bind 先が非 localhost だと「Invalid Host header」で
            # initialize が弾かれる(実機検証で確認)。bind 先自身(host:port と host)
            # を既定の許可リストとし、--allowed-host で Tailscale MagicDNS 名等を
            # 追加できるようにする。保護自体は無効化しない(build_transport_security
            # の docstring参照)。
            allowed_hosts = [f"{args.host}:{args.port}", args.host]
            if args.allowed_host:
                allowed_hosts.extend(args.allowed_host)
            server.settings.transport_security = build_transport_security(allowed_hosts)
            server.run(transport="streamable-http")
        else:
            server.run()
    elif args.command == "ls":
        if args.notebook is None:
            _print_notebooks(_build_service())
        else:
            _print_documents(_build_store(), args.notebook)
    elif args.command == "new":
        _build_service().create_notebook(
            args.notebook, description=args.description, backend=args.backend
        )
        print(f"notebook を作成しました: {args.notebook}")
    elif args.command == "add":
        result = _build_service().add_source(
            args.notebook,
            args.origin,
            description=args.description,
            auto_summary=not args.no_summary,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "setup":
        _cmd_setup(args)
    elif args.command == "rm":
        _cmd_rm(args)
    elif args.command == "index":
        stats = _build_service().index(args.notebook, full=args.all)
        print(
            f"indexed={stats.indexed} skipped={stats.skipped} pruned={stats.pruned} "
            f"chunks_written={stats.chunks_written} errors={stats.errors}"
        )
    elif args.command == "ask":
        result = _build_service().ask(args.notebook, args.question)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "consult":
        result = _build_service().consult(args.question)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "digest":
        result = _build_service().digest(args.notebook, args.doc_id, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "shelve":
        result = _build_service().shelve(args.directory, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "persona":
        service = _build_service()
        if args.set_persona is not None:
            try:
                service.set_persona(args.notebook, args.set_persona)
                print(f"ペルソナを設定しました: {args.notebook}")
            except UnknownNotebookError:
                print(f"notebook が見つかりません: {args.notebook}")
            except ValueError as e:
                print(f"エラー: {e}")
        elif args.clear:
            try:
                service.set_persona(args.notebook, None)
                print(f"ペルソナをクリアしました: {args.notebook}")
            except UnknownNotebookError:
                print(f"notebook が見つかりません: {args.notebook}")
            except ValueError as e:
                print(f"エラー: {e}")
        else:
            # 引数なしの場合は現在のペルソナを表示
            nb = _build_store().get_notebook(args.notebook)
            if nb is None:
                print(f"notebook が見つかりません: {args.notebook}")
            else:
                persona = nb.get("persona")
                if persona:
                    print(f"ペルソナ ({args.notebook}): {persona}")
                else:
                    print(f"ペルソナ ({args.notebook}): (未設定)")


if __name__ == "__main__":
    main()
