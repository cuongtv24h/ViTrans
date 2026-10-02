# `apps/web` — Next.js (M2)

Chưa có mã. Việc của M2: wizard 3 bước (kèm chế độ riêng tư và đồng ý), cổng glossary, trang đọc có
trích dẫn, xuất MD/DOCX/PDF, quản lý glossary, Admin (kèm `/admin/pool` — một cách nhập khoá duy nhất,
SPEC §17.14), giao diện duyệt Lõi văn phong.

Nguyên tắc: mọi lời gọi API đi qua đường dẫn tương đối để dev server proxy sang `apps/api`
(không hard-code `localhost`).
