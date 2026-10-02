#!/bin/sh
# Khởi động API: chạy migration TRƯỚC, rồi mới mở cổng (§20.7 bước "chạy migration").
# Migration là idempotent (Alembic `upgrade head`) nên nhiều bản API cùng khởi động cũng an toàn.
set -eu

: "${VISYNTH_DB_DSN:?thiếu VISYNTH_DB_DSN}"
: "${VISYNTH_SESSION_SECRET:?thiếu VISYNTH_SESSION_SECRET}"

echo "chờ PostgreSQL…"
i=0
until python -m visynth.cli db ping >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "LỖI: PostgreSQL không trả lời sau 60 lần thử" >&2
    exit 1
  fi
  sleep 2
done

echo "chạy migration…"
python -m visynth.cli db migrate

if [ -n "${VISYNTH_ADMIN_EMAIL:-}" ]; then
  echo "email quản trị đầu tiên: ${VISYNTH_ADMIN_EMAIL}"
fi

# `--proxy-headers` để log/redirect thấy đúng https khi đứng sau Caddy; tin header chỉ từ mạng nội bộ.
exec uvicorn visynth_api.app:create_app --factory \
  --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips '*'
