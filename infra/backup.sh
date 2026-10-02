#!/bin/sh
# Sao lưu hằng ngày: pg_dump -Fc → mã hoá `age` → đẩy sang kho đối tượng của NHÀ CUNG CẤP KHÁC (§20.6).
#
# Ba điều bắt buộc, đã cài thẳng vào script:
#   1. LOẠI TRỪ nội dung tài liệu: `doc_paragraphs`, `doc_sections`, `job_events` (chỉ lấy cấu trúc).
#      Nếu không, nội dung người dùng nằm trong bản sao lưu quá 14 ngày và phá lời hứa xoá (§5.4).
#   2. `POOL_MASTER_KEY` KHÔNG bao giờ vào bản sao lưu: script TỪ CHỐI chạy nếu biến đó có mặt trong
#      môi trường, và không đọc `/run/secrets` (§20.5).
#   3. Báo về "dead man's switch": thành công thì ping URL giám sát, thất bại thì ping `/fail`;
#      không có tín hiệu trong 26 giờ là hệ giám sát báo động (§20.7).
set -eu

: "${PGHOST:?thiếu PGHOST}"
: "${AGE_PUBLIC_KEY:?thiếu AGE_PUBLIC_KEY}"
: "${RCLONE_REMOTE:?thiếu RCLONE_REMOTE}"

if [ -n "${POOL_MASTER_KEY:-}" ] || [ -n "${VISYNTH_POOL_MASTER_KEY:-}" ]; then
  echo "LỖI: POOL_MASTER_KEY có trong môi trường container sao lưu — dừng để không lỡ tay đưa khoá vào bản sao lưu" >&2
  exit 1
fi

WORK=/tmp/visynth-backup
mkdir -p "$WORK" /backup/daily /backup/weekly
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
DAY=$(date -u +%Y-%m-%d)
DOW=$(date -u +%u)                       # 7 = Chủ nhật (tuần ISO)
DAILY_FILE="$WORK/visynth-$STAMP.dump.age"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"

notify_ok() {
  [ -n "${BACKUP_HEALTHCHECK_URL:-}" ] && curl -fsS -m 20 --retry 3 "$BACKUP_HEALTHCHECK_URL" >/dev/null || true
}
notify_fail() {
  [ -n "${BACKUP_HEALTHCHECK_URL:-}" ] && curl -fsS -m 20 --retry 3 "$BACKUP_HEALTHCHECK_URL/fail" >/dev/null || true
}
trap 'notify_fail' EXIT INT TERM

echo "$(date -u +%FT%TZ) sao lưu CSDL…"
# `--exclude-table-data`: chỉ bỏ DỮ LIỆU, vẫn giữ định nghĩa bảng để khôi phục ra lược đồ đầy đủ.
pg_dump --format=custom --compress=9 --no-owner --no-privileges \
  --exclude-table-data=doc_paragraphs \
  --exclude-table-data=doc_sections \
  --exclude-table-data=job_events \
  --file="$WORK/visynth.dump"

echo "$(date -u +%FT%TZ) mã hoá bằng age (khoá công khai trên máy, khoá riêng cất NGOÀI máy)…"
age --encrypt --recipient "$AGE_PUBLIC_KEY" --output "$DAILY_FILE" "$WORK/visynth.dump"
rm -f "$WORK/visynth.dump"

# Giữ 7 bản ngày + 4 bản tuần; bản tuần chụp nguyên trạng bản ngày của Chủ nhật.
cp "$DAILY_FILE" "/backup/daily/$DAY.dump.age"
if [ "$DOW" = "7" ]; then
  cp "$DAILY_FILE" "/backup/weekly/$DAY.dump.age"
fi

echo "$(date -u +%FT%TZ) đẩy ra $RCLONE_REMOTE (bật versioning, khoá ghi nếu có)…"
if ! command -v rclone >/dev/null 2>&1; then
  echo "LỖI: thiếu rclone — bản sao lưu đang nằm trên chính máy cần sao lưu" >&2
  exit 1
fi
rclone copy "$DAILY_FILE" "$RCLONE_REMOTE/daily/" --no-traverse
[ "$DOW" = "7" ] && rclone copy "/backup/weekly/$DAY.dump.age" "$RCLONE_REMOTE/weekly/" --no-traverse

# Tầng giữ bản sao: xoá bản cũ HƠN ngưỡng, cả trên máy và trên kho đối tượng.
find /backup/daily -name '*.dump.age' -mtime "+$KEEP_DAILY" -delete
find /backup/weekly -name '*.dump.age' -mtime "+$((KEEP_WEEKLY * 7))" -delete
rclone delete "$RCLONE_REMOTE/daily/" --min-age "${KEEP_DAILY}d" || true
rclone delete "$RCLONE_REMOTE/weekly/" --min-age "$((KEEP_WEEKLY * 7))d" || true

SIZE=$(du -h "$DAILY_FILE" | cut -f1)
echo "$(date -u +%FT%TZ) xong: $SIZE"
trap - EXIT INT TERM
notify_ok
