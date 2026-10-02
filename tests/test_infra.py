"""Kiểm cấu trúc hạ tầng VPS (SPEC §20) — chạy được OFFLINE, không cần Docker.

Sandbox phát triển không có mạng nên không thể `docker compose up` ở đây. Bù lại, những bất biến
quan trọng nhất của §20 là bất biến **cấu trúc**, kiểm được bằng cách đọc tệp:

* chỉ `caddy` được publish cổng; PostgreSQL và worker không lộ ra ngoài (kể cả khi ufw bị Docker đi vòng);
* không container nào mount `docker.sock`, không container nào chạy root, đều `read_only` + `cap_drop: [ALL]`;
* `worker` KHÔNG có tuyến ra internet trực tiếp — chỉ qua proxy `egress` có danh sách tên miền;
* khoá chủ đi vào bằng Docker secret, không nằm trong `.env`, `.env.example` hay image;
* script sao lưu loại trừ nội dung tài liệu và TỪ CHỐI chạy nếu thấy `POOL_MASTER_KEY`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
COMPOSE = yaml.safe_load((INFRA / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


def _app_services() -> dict:
    """Các service dùng chung lớp `x-app` (api, worker, scheduler)."""
    return {name: svc for name, svc in SERVICES.items() if svc.get("image", "").startswith("visynth-app")}


# ------------------------------------------------------------------ cổng và mạng


def test_only_caddy_publishes_ports():
    published = {name: svc.get("ports") for name, svc in SERVICES.items() if svc.get("ports")}
    assert set(published) == {"caddy"}, f"chỉ caddy được mở cổng, đang có: {sorted(published)}"
    assert {p.split(":")[0] for p in published["caddy"]} == {"80", "443"}
    # PostgreSQL và Redis KHÔNG bao giờ publish (ufw có thể bị Docker đi vòng qua — §20.4)
    assert "ports" not in SERVICES["postgres"]
    assert "ports" not in SERVICES["worker"]


def test_worker_has_no_direct_internet_route():
    internal = COMPOSE["networks"]["internal"]
    assert internal.get("internal") is True, "mạng `internal` phải chặn định tuyến ra ngoài"
    assert SERVICES["postgres"]["networks"] == ["internal"]
    # Worker chỉ nằm ở mạng kín; ra internet bằng proxy `egress` qua biến HTTPS_PROXY.
    worker_networks = SERVICES["worker"].get("networks") or COMPOSE["x-app"]["networks"]
    assert worker_networks == ["internal"]
    env = SERVICES["worker"]["environment"]
    assert env["HTTPS_PROXY"].startswith("http://egress:")
    assert "postgres" in env["NO_PROXY"], "không được proxy hoá lời gọi tới PostgreSQL nội bộ"
    # Chỉ proxy và caddy được nằm ở mạng có internet
    assert set(SERVICES["egress"]["networks"]) == {"internal", "uplink"}
    assert set(SERVICES["caddy"]["networks"]) == {"internal", "uplink"}


def test_no_docker_socket_and_all_containers_hardened():
    for name, svc in SERVICES.items():
        volumes = svc.get("volumes") or []
        assert not any("docker.sock" in str(v) for v in volumes), f"{name} mount docker.sock"
        assert svc.get("read_only") is True, f"{name} phải read_only"
        assert "ALL" in svc.get("cap_drop", []), f"{name} phải cap_drop: [ALL]"
        assert "no-new-privileges:true" in svc.get("security_opt", []), f"{name} phải no-new-privileges"
        assert svc.get("mem_limit"), f"{name} phải có mem_limit (một tài liệu xấu không kéo sập máy)"
        assert svc.get("pids_limit"), f"{name} phải có pids_limit"
        assert svc.get("restart") == "unless-stopped", f"{name} phải restart: unless-stopped"


def test_app_image_runs_non_root_with_unique_worker_ids():
    dockerfile = (INFRA / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER 10001:10001$", dockerfile, re.M), "image ứng dụng phải chạy non-root"
    assert "docker.sock" not in dockerfile
    # Mỗi bản sao worker phải có worker_id riêng (khoá `locked_by` trong job_tasks)
    cmd = SERVICES["worker"]["command"]
    assert "HOSTNAME" in " ".join(cmd)
    # Số bản worker cấu hình được, mặc định 2 (§20.2)
    assert SERVICES["worker"]["deploy"]["replicas"] == "${WORKER_REPLICAS:-2}"


def test_app_services_start_after_postgres_is_healthy():
    for name, svc in _app_services().items():
        assert svc["depends_on"]["postgres"]["condition"] == "service_healthy", name
    assert SERVICES["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert SERVICES["api"]["healthcheck"]["test"][:2] == ["CMD", "python"]


def test_scheduler_runs_the_documented_maintenance_jobs():
    scheduler = (INFRA / "scheduler.sh").read_text(encoding="utf-8")
    assert "ops reap" in scheduler, "mỗi phút: reclaim_stale_tasks + pool_reap_leases"
    assert "ops purge" in scheduler, "mỗi giờ: purge_expired_documents"
    assert "ops health" in scheduler, "tín hiệu cho giám sát ngoài máy"
    assert SERVICES["scheduler"]["command"] == ["/app/infra/scheduler.sh"]


# ------------------------------------------------------------------ bí mật


def test_pool_master_key_is_a_secret_never_in_env_files_or_image():
    secret = COMPOSE["secrets"]["pool_master_key"]["file"]
    assert secret == "./secrets/pool_master_key"
    assert "pool_master_key" in SERVICES["api"]["secrets"] + SERVICES["worker"]["secrets"]
    assert "pool_master_key" not in SERVICES["backup"].get("secrets", []), "container sao lưu không được thấy khoá chủ"
    for name, svc in _app_services().items():
        env = svc.get("environment", {})
        assert env.get("VISYNTH_POOL_MASTER_KEY_FILE") == "/run/secrets/pool_master_key", name
        assert "POOL_MASTER_KEY" not in env, f"{name}: khoá chủ không đi qua biến môi trường"
    # `.env` và `.env.example` không được chứa khoá chủ hay khoá nhà cung cấp
    example = (INFRA / ".env.example").read_text(encoding="utf-8")
    assert "POOL_MASTER_KEY=" not in example.replace("VISYNTH_POOL_MASTER_KEY_FILE=", "")
    for pattern in ("AIza", "nvapi-", "sk-", "age1"):
        assert not re.search(rf"^\s*[A-Z_]+=\s*{pattern}\S+", example, re.M), f".env.example lộ {pattern}"
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for path in ("secrets/", "infra/secrets/", ".env", "*.key"):
        assert path in ignore, f".dockerignore phải chặn {path}"
    gitignore = (INFRA / ".gitignore").read_text(encoding="utf-8")
    assert "secrets/" in gitignore and ".env" in gitignore


def test_backup_covers_the_documented_rules():
    script = (INFRA / "backup.sh").read_text(encoding="utf-8")
    # Loại trừ nội dung tài liệu để không phá lời hứa xoá trong 24 giờ (§5.4)
    for table in ("doc_paragraphs", "doc_sections", "job_events"):
        assert f"--exclude-table-data={table}" in script, f"phải loại dữ liệu {table} khỏi bản sao lưu"
    assert "pg_dump --format=custom" in script
    assert "age --encrypt --recipient" in script, "bản sao lưu phải được mã hoá bằng khoá công khai age"
    assert "rclone copy" in script, "phải đẩy ra nhà cung cấp KHÁC (§20.6)"
    assert "POOL_MASTER_KEY" in script and "exit 1" in script, "phải từ chối chạy nếu thấy khoá chủ"
    assert "BACKUP_HEALTHCHECK_URL" in script, "phải có dead man's switch (§20.7)"
    assert "KEEP_WEEKLY" in script and "KEEP_DAILY" in script, "phải giữ 7 bản ngày + 4 bản tuần"


def test_restore_runbook_measures_time_and_cleans_running_state():
    script = (INFRA / "restore.sh").read_text(encoding="utf-8")
    assert "age --decrypt --identity" in script
    assert "pg_restore" in script and "db migrate" in script
    assert "ops reap" in script, "khôi phục xong phải thu hồi task mồ côi/chỗ đặt hết hạn (§20.6)"
    assert "credit_ledger" in script, "phải đối chiếu sổ tín dụng (dữ liệu tiền)"
    assert "AC-25" in script and "$(date +%s)" in script, "phải đo thời gian thật của diễn tập khôi phục"


def test_caddyfile_terminates_tls_for_the_api_only():
    text = (INFRA / "Caddyfile").read_text(encoding="utf-8")
    assert "{$VISYNTH_DOMAIN}" in text
    assert "reverse_proxy api:8000" in text
    assert "/api/v1/healthz" in text, "giám sát ngoài máy ping đường này"
    assert "flush_interval -1" in text, "SSE cần tắt đệm"
    assert "Strict-Transport-Security" in text


def test_egress_allowlist_lists_only_llm_providers():
    conf = (INFRA / "egress" / "squid.conf").read_text(encoding="utf-8")
    assert "acl allowed_domains dstdomain" in conf
    assert "http_access deny all" in conf, "mặc định phải là TỪ CHỐI"
    assert "generativelanguage.googleapis.com" in conf and "nvidia.com" in conf
    assert "cache deny all" in conf and "forwarded_for delete" in conf


@pytest.mark.parametrize("script", sorted(p.name for p in INFRA.glob("*.sh")))
def test_shell_scripts_are_valid_posix(script: str):
    path = INFRA / script
    assert path.read_text(encoding="utf-8").startswith("#!/bin/sh"), f"{script} phải chạy bằng /bin/sh"
    result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"{script}: {result.stderr}"
