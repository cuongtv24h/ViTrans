#!/bin/sh
# Việc định kỳ trên VPS (§20.2, §20.7). Một vòng lặp đơn giản thay cho cron trong container
# (không cần mount `docker.sock`, không cần cấp quyền đặc biệt).
#
#   mỗi phút : `ops reap`  — reclaim_stale_tasks + pool_reap_leases
#   mỗi giờ  : `ops purge` — purge_expired_documents (nội dung gốc quá 14 ngày)
#   mỗi 5'   : `ops health --json` — in ra log để hệ giám sát ngoài máy bắt tín hiệu
#
# Mã thoát của `ops health` (3 = có dấu hiệu cần người xem) KHÔNG làm chết vòng lặp: log giữ nguyên
# để Uptime Kuma/healthchecks.io đọc, còn container vẫn sống.
set -u

tick=0
while :; do
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  if python -m visynth.cli ops reap --json; then :; else
    echo "$stamp LỖI: ops reap thất bại" >&2
  fi

  if [ $((tick % 60)) -eq 0 ]; then
    if python -m visynth.cli ops purge --json; then :; else
      echo "$stamp LỖI: ops purge thất bại" >&2
    fi
  fi

  if [ $((tick % 5)) -eq 0 ]; then
    python -m visynth.cli ops health --json || true
  fi

  tick=$((tick + 1))
  sleep 60
done
