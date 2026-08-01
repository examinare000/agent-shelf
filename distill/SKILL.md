# distill/extract.py

Claude Code のトランスクリプト（`~/.claude/projects/**/*.jsonl`）から、人間が
実際に打った発話だけを抽出し、蒸留（嗜好抽出）用の Markdown ダイジェストを作る。

## 何をするか

- skill/command 注入・system-reminder・ポリシー文・サブエージェント内部発話・
  tool 結果などのノイズを除外し、人間発話だけを残す。
- `sk-`/`ghp_`/AWS キー/JWT/`password=...` 等の資格情報らしき文字列をマスクする。
- 1発話が `--max-chars`（既定 1500 文字）を超える場合は末尾を切り詰める。
- ここで行うのは抽出・整形のみ。嗜好の蒸留自体は、このファイルではなく Claude が
  出力された digest を読んで別途行う。

## 実行方法

```
python3 distill/extract.py            # 前回の続きから増分抽出
python3 distill/extract.py --all      # 状態を無視して全件を再抽出
python3 distill/extract.py --since 2026-01-01T00:00:00Z
python3 distill/extract.py --max-chars 2000
```

## 出力先・state ファイル

- 出力: `distill/out/digest-<until>.md`（`<until>` は抽出範囲の終端日付、または `all`）
- state: `distill/.extract-state.json` に最後に処理した timestamp（`last_ts`）を
  記録し、次回実行時の増分抽出の起点にする（`--all` 指定時は state を更新しない）。
