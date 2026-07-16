#!/usr/bin/env python3
"""Claude Code トランスクリプトから「人間が実際に打った発話」だけを抽出し、
蒸留用の圧縮ダイジェスト(Markdown)を出力する。

- 入力: ~/.claude/projects/**/*.jsonl
- 出力: distill/out/digest-<until>.md
- 状態: distill/.extract-state.json （最後に処理した timestamp を記録、増分処理）

ノイズ（skill/command 注入、ポリシー、サブエージェント内部、tool結果）は除外し、
資格情報らしき文字列はマスクする。蒸留(=嗜好抽出)は別途 Claude が SKILL.md 手順で行う。
"""
from __future__ import annotations
import argparse
import glob
import json
import re
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
STATE = HERE / ".extract-state.json"

# 人間の発話ではない=除外するための先頭パターン
SKIP_PREFIXES = (
    "<command-", "<local-command-", "<bash-", "<system-reminder",
    "<user-prompt-submit-hook", "Base directory for this skill",
    "## Policy", "あなたは", "**既にレビューは完了",
    "Caveat:", "<attachment", "<task-",
    "[Request interrupted",
)
# 機微情報マスク（簡易）
SECRET_RES = [
    (re.compile(r'(sk-[A-Za-z0-9]{12,})'), '<REDACTED-KEY>'),
    (re.compile(r'(gh[pousr]_[A-Za-z0-9]{20,})'), '<REDACTED-TOKEN>'),
    (re.compile(r'(AKIA[0-9A-Z]{12,})'), '<REDACTED-AWS>'),
    (re.compile(r'(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+'),
     r'\1=<REDACTED>'),
    (re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}'),
     '<REDACTED-JWT>'),
]


def mask(text: str) -> str:
    for rx, repl in SECRET_RES:
        text = rx.sub(repl, text)
    return text


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(parts)
    return ""


def is_human_prompt(d: dict) -> bool:
    if d.get("type") != "user":
        return False
    if "toolUseResult" in d:           # tool結果
        return False
    if d.get("isSidechain"):           # サブエージェント内部
        return False
    if d.get("userType") not in (None, "external"):
        return False
    return True


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_ts": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO timestamp。未指定なら状態ファイルの続きから")
    ap.add_argument("--all", action="store_true", help="状態を無視して全件")
    ap.add_argument("--max-chars", type=int, default=1500,
                    help="1発話の最大文字数（超過は切り詰め）")
    args = ap.parse_args()

    state = load_state()
    since = "" if args.all else (args.since or state.get("last_ts", ""))

    rows = []           # (timestamp, project, branch, text)
    max_ts = since
    for f in glob.glob(str(PROJECTS / "**" / "*.jsonl"), recursive=True):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not is_human_prompt(d):
                        continue
                    ts = d.get("timestamp", "")
                    if since and ts <= since:
                        continue
                    text = extract_text(d.get("message", {}).get("content")).strip()
                    if not text:
                        continue
                    if text.startswith(SKIP_PREFIXES):
                        continue
                    if len(text) < 2:
                        continue
                    text = mask(text)
                    if len(text) > args.max_chars:
                        text = text[:args.max_chars] + " …(truncated)"
                    proj = Path(f).parent.name
                    branch = d.get("gitBranch", "") or "-"
                    rows.append((ts, proj, branch, text))
                    if ts > max_ts:
                        max_ts = ts
        except OSError:
            continue

    rows.sort(key=lambda r: r[0])
    OUT_DIR.mkdir(exist_ok=True)
    until = (max_ts or "all")[:10] or "all"
    out = OUT_DIR / f"digest-{until}.md"

    lines = [f"# 発話ダイジェスト (since={since or 'BEGIN'} → {max_ts or 'END'})",
             f"\n抽出件数: {len(rows)} 発話\n",
             "蒸留手順は distill/SKILL.md を参照。"
             "下記はノイズ除去済みの人間発話のみ。\n"]
    cur = None
    for ts, proj, branch, text in rows:
        head = f"{proj} [{branch}]"
        if head != cur:
            lines.append(f"\n## {head}\n")
            cur = head
        lines.append(f"- `{ts[:16]}` {text}")
    out.write_text("\n".join(lines), encoding="utf-8")

    if not args.all and max_ts:
        STATE.write_text(json.dumps({"last_ts": max_ts}, ensure_ascii=False))

    print(f"wrote {out}  ({len(rows)} prompts, until {max_ts or 'END'})")


if __name__ == "__main__":
    main()
