"""Kiểm tra SPEC.md khớp nguồn `tools/spec_src/` (dùng trong CI).

Phụ lục E được BỎ QUA khi so khớp: đó là kết quả chạy test tại chỗ (số test, thời gian), phụ thuộc
môi trường nên không thể tái lập từng ký tự. Mọi phần khác phải khớp tuyệt đối, nhờ vậy quy tắc
"không sửa tay SPEC.md" vẫn được cưỡng chế.

    python tools/check_spec_sync.py
"""
from __future__ import annotations

import difflib
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPENDIX_E = re.compile(r"^## Phụ lục E\..*", re.M | re.S)


def normalized(text: str) -> str:
    return APPENDIX_E.sub("## Phụ lục E. (bỏ qua khi so khớp)\n", text)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "SPEC.dung-lai.md"
        rc = subprocess.run([sys.executable, "tools/build_spec.py", "--out", str(out)], cwd=ROOT).returncode
        if rc != 0:
            return rc
        committed = (ROOT / "SPEC.md").read_text(encoding="utf-8")
        rebuilt = out.read_text(encoding="utf-8")
    if normalized(committed) == normalized(rebuilt):
        print("ok   SPEC.md khớp nguồn tools/spec_src (đã bỏ qua Phụ lục E)")
        return 0
    diff = difflib.unified_diff(
        normalized(committed).splitlines(),
        normalized(rebuilt).splitlines(),
        "SPEC.md (đang commit)",
        "SPEC.md (dựng lại từ nguồn)",
        lineterm="",
        n=2,
    )
    print("\n".join(list(diff)[:80]))
    print("\nLỖI: SPEC.md lệch khỏi tools/spec_src — chạy 'python tools/build_spec.py' rồi commit lại.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
