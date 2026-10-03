// Bộ chuyển Markdown nhỏ cho trang đọc — chỉ hiểu tập con mà pipeline sinh ra, và LUÔN thoát HTML
// trước khi chèn. Không kéo thư viện ngoài (SPA không có bước build ⇒ không có bundler để kiểm).

const ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPE[c]);

/** Đánh dấu trong dòng: **đậm**, *nghiêng*, `mã`, [chữ](liên kết nội bộ). */
function inline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*]+)\*/g, "$1<em>$2</em>");
  out = out.replace(
    /\[([^\]]+)\]\((https?:)?\/\/[^)]*\)/g,
    "$1", // liên kết ra ngoài bị bỏ vỏ, chỉ giữ chữ (an toàn hơn cho tài liệu có nguồn lạ)
  );
  return out;
}

/** Markdown → HTML đã thoát. Chỉ dùng cho nội dung do hệ thống sinh. */
export function markdownToHtml(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const out = [];
  let listOpen = null;

  const closeList = () => {
    if (listOpen) {
      out.push(`</${listOpen}>`);
      listOpen = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      closeList();
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      const level = Math.min(6, Math.max(2, heading[1].length + 1)); // h1 là tiêu đề trang, không lặp lại
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d{1,3}[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const want = bullet ? "ul" : "ol";
      if (listOpen !== want) {
        closeList();
        out.push(`<${want}>`);
        listOpen = want;
      }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      continue;
    }
    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) {
      closeList();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }
    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("\n");
}

/** Cắt chữ cho thẻ tóm tắt mà không cắt giữa từ. */
export function excerpt(text, max = 180) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, clean.lastIndexOf(" ", max) || max)}…`;
}
