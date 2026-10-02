#!/bin/sh
# Vòng lặp sao lưu: mỗi ngày một lần vào `BACKUP_HOUR` (giờ UTC, mặc định 3 = 10 giờ sáng Hà Nội).
# Không dùng cron trong container để không phải mount `docker.sock` hay cấp quyền đặc biệt (§20.2).
set -u

HOUR="${BACKUP_HOUR:-3}"

while :; do
  now_h=$(date -u +%H)
  now_m=$(date -u +%M)
  if [ "$((10#$now_h))" = "$((10#$HOUR))" ] && [ "$((10#$now_m))" -lt 5 ]; then
    if /app/infra/backup.sh; then
      echo "$(date -u +%FT%TZ) sao lưu xong"
    else
      echo "$(date -u +%FT%TZ) LỖI: sao lưu thất bại — xem log phía trên" >&2
    fi
    sleep 3300   # ngủ hơn 55 phút để không chạy hai lần trong cùng khung giờ
  else
    sleep 60
  fi
done
