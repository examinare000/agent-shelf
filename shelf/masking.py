"""秘密文字列マスクの単一ソース（shelf 版）。

なぜ importlib で distill/extract.py を直接読み込むのか:
mask のロジックを shelf 側で再実装すると、将来どちらかだけが更新されて
基準が drift する（=マスク漏れ）リスクがある。extract.py は改変禁止の
既存資産なので、モジュールとして読み込んで再エクスポートすることで
「ロジックの出どころは常に1つ」を保証する（recall/recall/masking.py と同方式）。

recall 側の is_human_prompt / extract_text / SKIP_PREFIXES は会話ログ専用の
関数であり、コーパス資料（shelf）には無関係なので持ち込まない。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# shelf/shelf/masking.py から見たリポジトリルート。shelf パッケージルート（parent.parent）で
# distill/extract.py に到達する（distill/ は shelf リポジトリに含まれる）。
_REPO_ROOT = Path(__file__).resolve().parent.parent

EXTRACT_PY_PATH = Path(
    os.environ.get("SHELF_EXTRACT_PY", _REPO_ROOT / "distill" / "extract.py")
)

# recall の "distill_extract" とはキャッシュキーを分ける。両プロジェクトは別々の
# venv/プロセスで動くため実害はないが、万一同一プロセスで両方 import された場合に
# EXTRACT_PY_PATH の env 上書きが食い違って混線しないようにするための保険。
_MODULE_NAME = "shelf_distill_extract"
if _MODULE_NAME in sys.modules:
    _ext = sys.modules[_MODULE_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_MODULE_NAME, EXTRACT_PY_PATH)
    if _spec is None or _spec.loader is None:  # pragma: no cover - 設定ミス時のみ到達
        raise ImportError(f"extract.py を読み込めません: {EXTRACT_PY_PATH}")
    _ext = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE_NAME] = _ext
    _spec.loader.exec_module(_ext)

mask = _ext.mask
