#!/bin/sh
# Khôi phục từ bản sao lưu `age` (§20.6) — chạy trong container `backup`:
#
#   docker compose run --rm backup /app/infra/restore.sh /backup/daily/2026-10-03.dump.age
#
# Runbook đầy đủ (§20.6): thuê VPS mới → cài Docker → lấy `POOL_MASTER_KEY` và khoá riêng `age` từ
# nơi cất → kéo cấu hình từ git → chạy script này → bật dịch vụ. **Diễn tập hằng quý, đo thời gian
# thật** (AC-25: ≤ 4 giờ) và ghi lại con số vào runbook.
set -eu

FILE="${1:-}"
: "${PGHOST:?thiếu PGHOST}"
: "${AGE_IDENTITY:?đặt AGE_IDENTITY=/duong/dan/khoa-rieng.txt (khoá riêng age, KHÔNG nằm trên máy chủ)}"

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "Dùng: $0 <tệp .dump.age>" >&2
  exit 2
fi

START=$(date +%s)
echo "== Khôi phục từ $FILE lúc $(date -u +%FT%TZ)"
echo "1/5 giải mã…"
age --decrypt --identity "$AGE_IDENTITY" --output /tmp/visynth.dump "$FILE"

echo "2/5 tạo lại lược đồ (khôi phục vào CSDL đang chạy)…"
psql -v ON_ERROR_STOP=1 -c "SELECT 1" >/dev/null
pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$PGDATABASE" /tmp/visynth.dump
rm -f /tmp/visynth.dump

echo "3/5 áp migration (bản sao lưu có thể cũ hơn mã đang chạy)…"
python -m visynth.cli db migrate || echo "CẢNH BÁO: migration lỗi — kiểm tra bằng tay trước khi bật dịch vụ" >&2

echo "4/5 dọn trạng thái chạy dở (task mồ côi, chỗ đặt hết hạn)…"
python -m visynth.cli ops reap --json

echo "5/5 đối chiếu để người trực tự kiểm:"
psql -v ON_ERROR_STOP=1 -At -c "
  SELECT 'người dùng: ' || count(*) FROM users
  UNION ALL SELECT 'job: ' || count(*) FROM jobs
  UNION ALL SELECT 'bản ghi tín dụng: ' || count(*) FROM credit_ledger
  UNION ALL SELECT 'tổng tín dụng đang có: ' || coalesce(sum(delta), 0) FROM credit_ledger
  UNION ALL SELECT 'báo cáo: ' || count(*) FROM reports
  UNION ALL SELECT 'khoá API (chỉ bản mã hoá): ' || count(*) FROM llm_credentials WHERE secret_enc IS NOT NULL
  UNION ALL SELECT 'cấu hình pool: ' || coalesce((SELECT value #>> '{}' FROM app_settings WHERE key = 'pool_version'), 'chưa có')
"
psql -v ON_ERROR_STOP=1 -c "SELECT * FROM reclaim_stale_tasks('3 minutes')" >/dev/null

MIN=$(( ($(date +%s) - START) / 60 ))
echo "== Xong sau ${MIN} phút (AC-25: mục tiêu ≤ 240 phút). Ghi con số này vào runbook."
echo "   Việc còn lại theo §20.6: kiểm tra vài báo cáo mở được, chạy AC-01 (tải lên → tạo job), rồi mới mở cổng 443."
