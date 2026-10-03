// Kiểm phần suy luận trích dẫn của SPA bằng Node (không cần trình duyệt, không bước build).
// Chạy: node tests/js/cites_test.mjs
//
// Vì sao cần: lỗi thật đã xảy ra — trang đọc gửi thẳng id đơn vị tri thức (`U-0001`) vào
// `/documents/{id}/paragraphs?pids=`, nên mọi trích dẫn đều hiện "Không tìm thấy đoạn này".
// Phần bù trừ nằm ở `unit_sources` do API trả về; tệp này khoá hành vi đó lại.

import assert from "node:assert/strict";
import { citeNumbers, citeTexts, sourcePids } from "../../apps/web/lib/cites.js";

const report = {
  id: "r1",
  document_id: "d1",
  unit_sources: { "U-0001": ["P000002"], "U-0002": ["P000004", "P000007"], "U-0003": ["P000004"] },
  sections: [
    {
      section_id: "S01",
      blocks: [
        { block_id: "S01.b01", markdown_vi: "a", cites: ["U-0002", "U-0001"] },
        { block_id: "S01.b02", markdown_vi: "b", cites: ["U-0002"] },
      ],
    },
    { section_id: "S02", blocks: [{ block_id: "S02.b01", markdown_vi: "c", cites: ["U-0003"] }] },
  ],
};

// 1) `pid` cần tải: tra qua `unit_sources`, giữ thứ tự xuất hiện, không trùng.
assert.deepEqual(sourcePids(report), ["P000004", "P000007", "P000002"]);

// 2) Số hiển thị theo lần xuất hiện đầu tiên (SPEC §13.4: không hiện id nội bộ).
const numbers = citeNumbers(report);
assert.deepEqual([...numbers.entries()], [
  ["U-0002", 1],
  ["U-0001", 2],
  ["U-0003", 3],
]);

// 3) Nguyên văn: đơn vị dựa vào nhiều đoạn thì ghép lại; đơn vị trỏ vào đoạn đã xoá thì bỏ qua.
const paragraphs = new Map([
  ["P000002", "Câu nguồn của U-0001."],
  ["P000004", "Đoạn đầu của U-0002."],
]);
const texts = citeTexts(report, paragraphs);
assert.equal(texts.get("U-0001"), "Câu nguồn của U-0001.");
assert.equal(texts.get("U-0002"), "Đoạn đầu của U-0002.");
assert.equal(texts.get("U-0003"), "Đoạn đầu của U-0002.");
assert.equal(texts.get("P000002"), "Câu nguồn của U-0001.", "vẫn tra được theo pid trực tiếp");
assert.equal(texts.has("U-9999"), false, "không bịa đoạn cho đơn vị không có nguồn");

// 4) Báo cáo cũ/mức dịch đầy đủ: `cites` là pid thẳng, không có `unit_sources` — vẫn phải chạy.
const legacy = {
  sections: [{ section_id: "S01", blocks: [{ block_id: "S01.b01", markdown_vi: "x", cites: ["P000001"] }] }],
};
assert.deepEqual(sourcePids(legacy), ["P000001"]);
assert.deepEqual([...citeNumbers(legacy).values()], [1]);
assert.equal(citeTexts(legacy, new Map([["P000001", "Nguyên văn."]])).get("P000001"), "Nguyên văn.");
assert.deepEqual(sourcePids({ sections: [] }), [], "báo cáo rỗng không được lỗi");

console.log("cites_test: tất cả đạt");
