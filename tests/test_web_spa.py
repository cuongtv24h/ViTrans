"""SPA không bước build (M2): phục vụ tĩnh, đồ thị import, cú pháp, và các trang bắt buộc.

Vì không có bundler/typechecker, những lỗi mà bước build thường bắt (import sai đường dẫn, cú pháp
hỏng, quên nối một trang) sẽ lọt ra tới trình duyệt. Bộ test này thay vai trò đó:

* `node --check` mọi tệp JS (bỏ qua nếu máy không có Node);
* mọi `import` tương đối phải trỏ tới tệp CÓ THẬT (bắt lỗi đổi tên tệp mà quên sửa import);
* không có import trần/URL ngoài (SPA không được kéo thư viện từ CDN — vừa phá quyền riêng tư vừa
  làm hỏng trang khi mạng chặn);
* các trang mà M2 yêu cầu phải tồn tại trong bảng định tuyến;
* API phải thắng tệp tĩnh: `/api/v1/…` không bị `index.html` nuốt mất.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
JS_FILES = sorted(WEB.rglob("*.js"))


@pytest.fixture
def client(pg_schema):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    settings = Settings(db_dsn=pg_schema, session_secret="test-secret", web_dir=WEB)
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client
    application.state.db.close()


def test_spa_shell_is_served_with_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="view"' in response.text
    for asset in ("/app.js", "/styles.css"):
        assert client.get(asset).status_code == 200, asset


def test_api_routes_win_over_static_mount(client):
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    # Đường dẫn API không tồn tại phải trả 404 JSON, KHÔNG được rơi về index.html
    missing = client.get("/api/v1/khong-ton-tai")
    assert missing.status_code == 404


def test_every_relative_import_resolves_to_a_real_file():
    pattern = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]""")
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for target in pattern.findall(source):
            assert not target.startswith(("http://", "https://")), f"{path.name} kéo thư viện ngoài: {target}"
            assert target.startswith("."), f"{path.name} dùng import trần (không có bundler): {target}"
            resolved = (path.parent / target).resolve()
            assert resolved.is_file(), f"{path.name} trỏ tới tệp không tồn tại: {target}"


def test_dynamic_run_once_imports_also_resolve():
    """`import('./views/x.js')` cũng phải kiểm — đây là kiểu import dễ sai nhất khi đổi tên tệp."""
    pattern = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
    for path in JS_FILES:
        for target in pattern.findall(path.read_text(encoding="utf-8")):
            assert (path.parent / target).resolve().is_file(), f"{path.name}: {target}"


@pytest.mark.skipif(shutil.which("node") is None, reason="cần Node để kiểm cú pháp (chỉ là công cụ kiểm tra)")
def test_javascript_parses():
    for path in JS_FILES:
        result = subprocess.run(  # noqa: S603 - tham số cố định, không có đầu vào người dùng
            ["node", "--input-type=module", "--check", "-"],
            input=path.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name} lỗi cú pháp:\n{result.stderr[:400]}"


def test_index_html_only_references_local_assets():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'src="/app.js"' in html and "/styles.css" in html
    # Không được trỏ ra ngoài (CDN/font): vừa phá quyền riêng tư vừa làm trang hỏng khi mạng chặn.
    assert not re.search(r'(?:src|href)="https?://', html), "index.html trỏ tài nguyên ra ngoài"


def test_required_m2_screens_are_routed():
    """Bảng định tuyến phải có đủ các màn hình M2 yêu cầu — thiếu là hỏng tiêu chí nghiệm thu."""
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    for route in ("#/tai-lieu", "#/glossary", "#/tai-khoan", "#/phap-ly"):
        assert route in app_js, f"thiếu tuyến {route}"
    assert "views/job.js" in app_js and "views/report.js" in app_js, "thiếu trang chi tiết job/báo cáo"
    assert "#/admin/pool" in app_js
    # Các trang khác phải dẫn tới hai trang chi tiết đó (không có trang mồ côi).
    assert "#/job/" in (WEB / "views" / "wizard.js").read_text(encoding="utf-8")
    assert "#/bao-cao/" in (WEB / "views" / "home.js").read_text(encoding="utf-8")

    wizard = (WEB / "views" / "wizard.js").read_text(encoding="utf-8")
    for needle in ("Tải tài liệu", "Mức dịch & cấu hình", "Xác nhận & chạy"):
        assert needle in wizard, f"wizard thiếu bước {needle}"

    report = (WEB / "views" / "report.js").read_text(encoding="utf-8")
    assert "export?format=${format}" in report and '["md", "Markdown"]' in report
    for fmt in ("md", "docx", "pdf", "html"):
        assert f'["{fmt}", ' in report, f"trang đọc thiếu nút xuất {fmt}"
    assert "bilingual=true" in report
    assert "/paragraphs?" in report, "trích dẫn phải mở được nguyên văn đoạn nguồn"

    job = (WEB / "views" / "job.js").read_text(encoding="utf-8")
    assert "/glossary/confirm" in job and "EventSource" in job

    admin = (WEB / "views" / "admin.js").read_text(encoding="utf-8")
    for needle in ("/admin/pool/status", "/admin/pool/groups/", "glossary-review", "/admin/style-cores"):
        assert needle in admin, f"khu quản trị thiếu {needle}"


def test_no_secret_storage_in_browser():
    """Không được giấu token/khoá trong `localStorage`/`sessionStorage` — XSS sẽ lấy hết."""
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        # Chỉ bắt LỜI GỌI thật, không bắt tên trong chú thích (chú thích có nói về localStorage).
        for sink in ("localStorage.", "sessionStorage.", "document.cookie"):
            assert sink not in source, f"{path.name} dùng {sink}"
