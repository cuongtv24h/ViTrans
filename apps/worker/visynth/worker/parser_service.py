"""Dịch vụ parser chạy trong sandbox (SPEC §6.1, §20.4).

Vì sao có tiến trình riêng: bộ bóc tách phải chạy cách ly khỏi API — **không có tuyến ra internet**
(container chỉ nối mạng `internal`, không có `uplink`), hệ tệp chỉ đọc + `tmpfs` cho tệp tạm, chạy
non-root, giới hạn CPU/RAM, **timeout 120 giây** và trần dung lượng 50 MB. API chỉ gửi byte thô qua
mạng nội bộ rồi nhận JSON đã bóc tách; nếu parser chết thì API vẫn sống và trả lỗi rõ ràng.

Ở đây dùng `http.server` của thư viện chuẩn (không kéo FastAPI vào tiến trình ít tin cậy nhất) và
**không** giữ bất kỳ khoá nhà cung cấp nào — tiến trình này không bao giờ gọi LLM.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from visynth.extract import MAX_FILE_BYTES, ExtractionError, extract

log = logging.getLogger("visynth.parser")

#: §6.1: timeout 120 giây cho mỗi lần bóc tách (tệp lớn nhất cũng phải xong trong mức đó).
PARSE_TIMEOUT_S = 120


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):  # pragma: no cover - chỉ chạy trong tiến trình thật
    raise _Timeout


def parse_bytes(
    raw: bytes,
    *,
    filename: str | None,
    title: str | None,
    max_bytes: int = MAX_FILE_BYTES,
    timeout_s: int = PARSE_TIMEOUT_S,
) -> dict:
    """Bóc tách byte → JSON thuần (đúng hợp đồng `documents` + `doc_paragraphs` + `doc_sections`)."""
    if len(raw) > max_bytes:
        raise ExtractionError("file_too_large", f"tệp vượt {max_bytes // (1024 * 1024)} MB")
    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(max(1, int(timeout_s)))
    except (ValueError, AttributeError):  # pragma: no cover - ngoài luồng chính
        previous = None
    try:
        extraction = extract(raw, filename=filename, title=title)
    except _Timeout as exc:
        raise ExtractionError("parse_timeout", f"bóc tách quá {timeout_s} giây") from exc
    finally:
        if previous is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    return extraction.as_dict()


class ParserHandler(BaseHTTPRequestHandler):
    """`POST /parse` (byte thô) và `GET /healthz`. Không nhận/không log nội dung tài liệu."""

    server_version = "visynth-parser/1"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- tiện ích
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - chữ ký của thư viện
        log.debug("parser: " + fmt, *args)

    # ---------------------------------------------------------------- route
    def do_GET(self) -> None:  # noqa: N802 - tên do thư viện quy định
        if urlparse(self.path).path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/parse":
            self._send(404, {"code": "not_found"})
            return
        query = parse_qs(parsed.query)
        max_bytes = int(self.server.max_bytes)  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(400, {"code": "empty_body"})
            return
        if length > max_bytes:
            # Đọc phần tối thiểu rồi từ chối, không nạp cả tệp vào bộ nhớ.
            self.rfile.read(min(length, 8192))
            self._send(413, {"code": "file_too_large"})
            return
        raw = self.rfile.read(length)
        try:
            payload = parse_bytes(
                raw,
                filename=(query.get("filename") or [None])[0],
                title=(query.get("title") or [None])[0],
                max_bytes=max_bytes,
                timeout_s=int(self.server.timeout_s),  # type: ignore[attr-defined]
            )
        except ExtractionError as exc:
            status = 413 if exc.code == "file_too_large" else 422
            self._send(status, {"code": exc.code, "message": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - không để lộ stack trace ra ngoài
            log.warning("bóc tách hỏng: %s", exc)
            self._send(500, {"code": "parse_failed", "message": str(exc)[:200]})
            return
        self._send(200, payload)


class ParserServer(ThreadingHTTPServer):
    """HTTP server có trần dung lượng/timeout truyền vào (dùng cho cả test lẫn chạy thật)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, max_bytes: int = MAX_FILE_BYTES, timeout_s: int = PARSE_TIMEOUT_S):
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        super().__init__(address, ParserHandler)


def serve(
    host: str = "0.0.0.0",  # noqa: S104 - trong container, chỉ mạng `internal` nhìn thấy
    port: int = 8080,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    timeout_s: int = PARSE_TIMEOUT_S,
) -> None:  # pragma: no cover - vòng lặp dài
    server = ParserServer((host, port), max_bytes=max_bytes, timeout_s=timeout_s)
    log.info("parser sandbox nghe %s:%s (trần %d MB, timeout %ds)", host, port, max_bytes // 1024 // 1024, timeout_s)
    server.serve_forever()


def serve_in_thread(*, max_bytes: int = MAX_FILE_BYTES, timeout_s: int = PARSE_TIMEOUT_S):
    """Dựng server trên cổng ngẫu nhiên của 127.0.0.1 (dùng cho test). Trả `(server, base_url)`."""
    server = ParserServer(("127.0.0.1", 0), max_bytes=max_bytes, timeout_s=timeout_s)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"
