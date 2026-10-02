"""ViSynth — pipeline chuyển ngữ & tổng hợp tài liệu sang tiếng Việt.

M0 (prototype CLI) triển khai dần theo `docs/BUILD_PLAN.md`:
  - W1: hợp đồng LLM client (kèm bản giả), bóc tách TXT/DOCX, chia segment, ước tính chi phí.
  - W2: các giai đoạn LLM (P1-P9) + kiểm tra tất định.
  - W3: nối LLM Pool thật.
"""

__version__ = "0.1.0"
