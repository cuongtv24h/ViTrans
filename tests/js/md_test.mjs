// Kiểm hàm thuần của SPA bằng Node (không cần trình duyệt, không bước build).
// Chạy: node tests/js/md_test.mjs   — pytest gọi tệp này trong tests/test_web_spa.py.
//
// Vì sao cần: SPA không có bundler/typechecker, nên phần xử lý trích dẫn (thứ người dùng dựa vào để
// đối chiếu nguồn) phải có test thật chạy được, không chỉ là "mắt thấy đúng".

import assert from "node:assert/strict";
import { ApiError } from "../../apps/web/lib/api.js";
import { splitTranslation, markdownToHtml, excerpt, SOURCE_ANCHOR } from "../../apps/web/lib/md.js";

// 1) Tách bản dịch theo neo nguồn: mỗi đoạn phải gắn ĐÚNG pid của nó
const translated = [
  "# Bài giảng",
  "",
  "> **Bản dịch do AI thực hiện.**",
  "",
  "## Chương 1",
  "",
  "Lò phản ứng đạt 1200 độ.",
  "",
  "<sub>Nguồn: **P000001**</sub>",
  "",
  "Các kỹ sư ghi lại độ giảm áp.",
  "",
  "<sub>Nguồn: **P000042**</sub>",
  "",
  "Đoạn cuối chưa có neo.",
].join("\n");

const blocks = splitTranslation(translated);
const headings = blocks.filter((b) => b.heading).map((b) => b.heading);
const paragraphs = blocks.filter((b) => (b.cites || []).length);
assert.deepEqual(headings, ["Bài giảng", "Chương 1"], "tiêu đề phải được tách riêng");
assert.deepEqual(paragraphs.map((b) => b.cites), [["P000001"], ["P000042"]], "pid phải gắn đúng đoạn");
assert.match(paragraphs[0].markdown, /Lò phản ứng đạt 1200 độ\./, "giữ nguyên văn bản dịch");
assert.match(paragraphs[1].markdown, /độ giảm áp/, "đoạn thứ hai đúng nội dung");
const tail = blocks.at(-1);
assert.deepEqual(tail.cites, [], "đoạn chưa có neo vẫn hiện, chỉ là không có trích dẫn");

// 2) Neo không hợp lệ (pid sai định dạng) KHÔNG được nhận là trích dẫn
assert.equal(SOURCE_ANCHOR.test("<sub>Nguồn: **P12**</sub>"), false);
assert.equal(SOURCE_ANCHOR.test("<sub>Nguồn: **P000012**</sub>"), true);

const weird = splitTranslation("Chữ bình thường\n\n<sub>Nguồn: **P12**</sub>");
assert.equal(weird.filter((b) => b.cites.length).length, 0, "pid sai định dạng bị bỏ qua");

// 3) Markdown phải THOÁT HTML trước khi chèn (nội dung tài liệu không được chạy như mã)
const dangerous = markdownToHtml('<img src=x onerror="alert(1)"> **đậm** `mã` [chữ](https://x.test)');
assert.ok(!dangerous.includes("<img"), "thẻ HTML trong nội dung phải bị thoát");
assert.ok(dangerous.includes("&lt;img"), "đã thoát đúng cách");
assert.ok(dangerous.includes("<strong>đậm</strong>"), "vẫn giữ đậm");
assert.ok(dangerous.includes("<code>mã</code>"), "vẫn giữ mã");
assert.ok(!dangerous.includes("<a href"), "liên kết ngoài bị bỏ vỏ, chỉ giữ chữ");

// 4) Cắt tóm tắt không cắt giữa từ
const shortened = excerpt("một hai ba bốn năm sáu bảy tám chín mười", 18);
assert.ok(shortened.endsWith("…") && !shortened.includes("  "), `cắt gọn: ${shortened}`);
assert.equal(excerpt("ngắn", 40), "ngắn");

// 5) 429 (giới hạn tốc độ, M3) phải nói rõ phải chờ bao lâu — nếu không người dùng bấm lại liên tục
//    và tự khoá mình lâu hơn.
const limited = new ApiError(429, { detail: "quá nhiều yêu cầu", code: "rate_limited", retry_after_s: 42.4 });
assert.equal(limited.code, "rate_limited");
assert.ok(limited.message.includes("43 giây"), `có đếm ngược: ${limited.message}`);
assert.equal(limited.waitMs, 42400);
const plain = new ApiError(500, { detail: "lỗi máy chủ" });
assert.equal(plain.message, "lỗi máy chủ", "lỗi khác không bị thêm chữ thừa");
assert.equal(plain.waitMs, 0);

console.log("md_test: tất cả đạt");
