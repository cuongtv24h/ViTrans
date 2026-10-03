// Lớp gọi API duy nhất của SPA.
//
// Quy ước:
//  * Cùng gốc (`/api/v1`) nên cookie phiên HttpOnly tự đi kèm; KHÔNG lưu token vào `localStorage`
//    (token trong localStorage là món quà cho XSS — §14).
//  * Lỗi trả về theo RFC 7807 (problem+json) với `code` đọc được bằng máy ⇒ ném `ApiError` để
//    giao diện hiển thị đúng câu tiếng Việt của API thay vì "422 Unprocessable".

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, body) {
    const detail = body && (body.detail || body.message);
    // 429 là lỗi DUY NHẤT người dùng tự sửa được bằng cách chờ: nói rõ chờ bao lâu, nếu không họ sẽ
    // bấm lại liên tục và tự khoá mình lâu hơn. Gắn ngay vào `message` để mọi chỗ `toast(error.message)`
    // đều hiển thị đúng mà không phải sửa từng màn hình.
    const retryAfterS = Number((body && body.retry_after_s) || 0);
    const hint = retryAfterS > 0 ? ` (thử lại sau ${Math.ceil(retryAfterS)} giây)` : "";
    super((detail || `Lỗi ${status}`) + hint);
    this.name = "ApiError";
    this.status = status;
    this.code = (body && body.code) || "error";
    this.retryAfterS = retryAfterS;
    this.body = body || {};
  }

  /** Chờ hết hạn mức rồi tự gọi lại — dùng cho nút người dùng chủ động bấm lại (không tự động nền). */
  get waitMs() {
    return Math.max(0, Math.ceil(this.retryAfterS * 1000));
  }
}

/** Gọi API và trả JSON. `body` là object ⇒ JSON; `form` là FormData ⇒ multipart. */
export async function api(path, { method = "GET", body, form, headers = {}, signal } = {}) {
  const init = { method, headers: { ...headers }, credentials: "same-origin", signal };
  if (form) {
    init.body = form;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${BASE}${path}`, init);
  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { detail: text.slice(0, 300) };
    }
  }
  if (!response.ok) throw new ApiError(response.status, parsed);
  return parsed;
}

export const get = (path, options) => api(path, { ...options, method: "GET" });
export const post = (path, body, options) => api(path, { ...options, method: "POST", body });
export const patch = (path, body, options) => api(path, { ...options, method: "PATCH", body });
export const put = (path, body, options) => api(path, { ...options, method: "PUT", body });
export const del = (path, options) => api(path, { ...options, method: "DELETE" });

/** Mã mời/tài liệu dài dễ gõ sai — chuẩn hoá trước khi gửi. */
export const normCode = (value) => String(value || "").trim().toUpperCase();

/** Idempotency-Key cho `POST /jobs`: tạo một lần cho mỗi lần bấm "tạo job" (bấm lại không trừ tiền hai lần). */
export function newIdempotencyKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Tải tệp xuất về máy mà vẫn giữ tên do máy chủ đặt. */
export async function download(path, filename) {
  const response = await fetch(`${BASE}${path}`, { credentials: "same-origin" });
  if (!response.ok) {
    const text = await response.text();
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { detail: text.slice(0, 200) };
    }
    throw new ApiError(response.status, parsed);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "visynth";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
