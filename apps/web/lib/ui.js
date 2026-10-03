// Tiện ích dựng DOM + định dạng tiếng Việt. Không dùng framework, KHÔNG dùng innerHTML cho dữ liệu
// người dùng (mọi thứ đi qua `textContent` hoặc `h()`), nên nội dung tài liệu không thể chèn mã.

/** Tạo phần tử: `h("a", { href: "#/x", class: "y" }, "chữ")`. */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "html") el.innerHTML = value; // CHỈ dùng với chuỗi do mã này tự sinh
    else if (key === "on") for (const [event, handler] of Object.entries(value)) el.addEventListener(event, handler);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "value") el.value = value;
    else if (key === "checked" || key === "disabled" || key === "hidden" || key === "selected") el[key] = Boolean(value);
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

export function append(root, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    root.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return root;
}

/** Xoá sạch rồi dựng lại nội dung — đủ nhanh cho quy mô M2 và không cần diff.
    Tên `mount` (không phải `render`) để các trang còn dùng được `render` làm tên hàm vào của trang. */
export function mount(root, ...children) {
  root.replaceChildren();
  append(root, children);
  return root;
}

export function toast(message, { error = false, ms = 5000 } = {}) {
  const box = document.getElementById("toast");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("error", error);
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    box.hidden = true;
  }, ms);
}

export const nf = new Intl.NumberFormat("vi-VN");
export const nf2 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 });

export function words(value) {
  return `${nf.format(value || 0)} từ`;
}

export function usd(value) {
  return `$${nf2.format(value || 0)}`;
}

export function when(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return date.toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" });
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} giây`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} phút`;
  return `${Math.floor(m / 60)} giờ ${m % 60} phút`;
}

const JOB_STATUS = {
  queued: ["Đang xếp hàng", ""],
  running: ["Đang chạy", "warn"],
  awaiting_glossary: ["Chờ duyệt thuật ngữ", "warn"],
  succeeded: ["Xong", "ok"],
  failed: ["Lỗi", "danger"],
  canceled: ["Đã huỷ", ""],
  expired: ["Hết hạn", ""],
};

export function statusBadge(status) {
  const [label, kind] = JOB_STATUS[status] || [status, ""];
  return h("span", { class: `badge ${kind}`.trim(), text: label });
}

export const LEVELS = {
  full_translation: "Dịch đầy đủ (giữ nguyên văn từng đoạn)",
  detailed_synthesis: "Tổng hợp chi tiết",
  deep_synthesis: "Tổng hợp chuyên sâu",
  executive_brief: "Bản tóm tắt điều hành",
};

export const STAGE_LABEL = {
  extract: "Đọc tài liệu",
  profile: "Phân tích cấu trúc & thuật ngữ",
  glossary: "Chốt thuật ngữ",
  translate: "Dịch từng đoạn",
  map: "Lập dàn ý có trích dẫn",
  consolidate: "Hợp nhất & khử trùng lặp",
  write: "Viết bản tiếng Việt",
  verify: "Kiểm chứng trích dẫn",
  repair: "Sửa lỗi được phát hiện",
  export: "Kết xuất tệp",
};

export const FLAG_REASONS = {
  sai: "Dịch sai / sai lệch nguồn",
  thieu: "Thiếu ý",
  kho_doc: "Khó đọc",
  khac: "Lý do khác",
};

