# ADR-0003: SDK 境界のテストダブルは実シグネチャへ結合させる

## 状態

採択（2026-08-02、deps/mcp-2.0-migration の mcp SDK 2.0 移行で確立）

## 文脈

mcp SDK 1.x→2.0 の破壊的変更（settings への代入から `run()` キーワード引数への移動）は、
`_FakeMcpServer.run(transport, **kwargs)` が任意の kwarg 名を黙って受理するため、
テストスイート 1282 件が全緑のまま実行時 `ValueError` で死ぬ変更だった。
CI が事前に赤くなったのは `mcp.server.fastmcp` のモジュール削除による import エラー
という別経路の偶然であり、テストダブル経由ではこの欠陥クラスを検出できなかった。

## 決定

外部 SDK との呼び出し境界を持つテストダブルには、本体コードが組み立てた引数を
実 SDK のシグネチャへ `inspect.signature(...).bind_partial(**kwargs)` で束縛する
結合テストを併設する（tests/test_cli.py の
`test_http_dispatch_kwargs_bind_to_real_sdk_run_streamable_http_async_signature`）。
ダブルの柔軟さ（`**kwargs` 受理）は維持しつつ、kwarg 名のドリフトだけを実物で固定する。

## 検討した選択肢

- **ダブルのみで検証し続ける**: 破壊的変更クラスが構造的に検出不能（今回実証）。→ 棄却。
- **実 MCPServer を起動する統合テスト**: 検出力は最大だがポート確保・非同期起動の
  コストと不安定さを常時負う。→ 棄却（実機 initialize 往復は初回起動時の手動確認に委ねる）。
- **`inspect.signature` による束縛のみ追加（採択）**: 誤字・改名・引数移動の 4 変異
  すべてで `TypeError` を検出することを実測済み。コストはテスト 1 件。
- 付随判断 — **`mcp>=2.0.0` に上限（`<3`）を付けるか**: uv.lock と CI の `uv sync` が
  バージョンを封じ込めており、メジャー更新は dependabot の lock bump がマージ前に
  CI で捕捉される（今回の検出経路そのもの）。上限は dependabot の提案を解決失敗に
  変えるだけで安全性の実質が変わらないため付けない。

## 参照

- 経緯の詳細: docs/trial-log/2026-08-02-brushup-plan.md（deps/mcp-2.0-migration セクション）
