# Dockerfile dùng cho MỌI tiến trình Python của ViSynth: `api`, `worker`, `scheduler`, `parser`.
# Một image, nhiều lệnh — nhờ vậy không có hai bản mã lệch nhau giữa các container (SPEC §20.2).
#
# Nguyên tắc: chạy non-root, không chứa bí mật trong image, chỉ cài thứ cần cho lúc chạy.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VISYNTH_UPLOAD_DIR=/data/uploads

# `libmagic` để nhận dạng tệp; `curl` cho healthcheck; `tini` làm init (dọn tiến trình con);
# `fonts-dejavu-core` để kết xuất PDF tiếng Việt (fpdf2 không kèm font Unicode).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 curl tini fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài phụ thuộc trước để tận dụng cache lớp của Docker.
COPY pyproject.toml README.md ./
COPY apps ./apps
COPY eval ./eval
RUN pip install --no-cache-dir .

# Tài liệu/đặc tả là DỮ LIỆU LÚC CHẠY: prompt, JSON Schema, DDL, mẫu khai báo pool.
COPY docs/prompts ./docs/prompts
COPY docs/schemas ./docs/schemas
COPY docs/db ./docs/db
COPY docs/examples ./docs/examples
COPY docs/api ./docs/api
COPY docs/reference ./docs/reference
# Migration Alembic nằm trong `apps/api` nên đã có ở lớp COPY apps phía trên.
# Script vận hành (chạy migration rồi mở cổng, vòng lặp việc định kỳ) đi kèm image để mọi bản
# triển khai chạy ĐÚNG một quy trình; `.dockerignore` chặn `infra/secrets` và `infra/backup`.
COPY infra ./infra
RUN chmod +x /app/infra/*.sh

# Người dùng không đặc quyền; `/data` là nơi duy nhất ghi được (volume).
RUN useradd --system --uid 10001 --home /data --shell /usr/sbin/nologin visynth \
 && mkdir -p /data/uploads /data/backup \
 && chown -R visynth:visynth /data /app
USER 10001:10001

ENV VISYNTH_DATA_DIR=/data \
    PATH="/usr/local/bin:${PATH}"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-c", "import visynth, visynth_api; print('ViSynth image — hãy chỉ định lệnh (xem infra/docker-compose.yml)')"]
