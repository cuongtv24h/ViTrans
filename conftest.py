"""Cấu hình chung cho pytest của mã sản phẩm (chạy được cả khi chưa `pip install -e .`)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# `docs/` để test đối chiếu với bộ đặc tả (reference/estimator.py, reference/llm_pool.py).
for p in (ROOT / "apps" / "worker", ROOT, ROOT / "docs"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
