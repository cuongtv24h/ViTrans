// Trích dẫn của báo cáo: `report_blocks.cites` chứa id ĐƠN VỊ TRI THỨC (`U-0001`, SPEC §6.8), còn trang
// đọc cần `pid` đoạn nguồn (`P000002`) để mở nguyên văn phần "đối chiếu nguồn". API trả kèm `unit_sources`
// (đơn vị → các pid nó dựa vào) để nối hai không gian id đó lại.
//
// Phần này là hàm thuần, không chạm DOM: SPA không có bước build nên đây là chỗ duy nhất kiểm thử được
// bằng Node (tests/js/cites_test.mjs). Đã từng hỏng ở đây: gửi thẳng `U-0001` vào `/paragraphs?pids=`
// nên mọi trích dẫn đều hiện "Không tìm thấy đoạn này".

/** Danh sách `pid` đoạn nguồn cần tải cho cả báo cáo (không trùng, giữ thứ tự xuất hiện). */
export function sourcePids(report) {
  const byUnit = report.unit_sources || {};
  const pids = [];
  for (const section of report.sections || []) {
    for (const block of section.blocks || []) {
      for (const cite of block.cites || []) {
        // Báo cáo cũ (chưa có `unit_sources`) trích dẫn thẳng `pid` — giữ tương thích ngược.
        for (const pid of byUnit[cite] || [cite]) if (!pids.includes(pid)) pids.push(pid);
      }
    }
  }
  return pids;
}

/** Số hiển thị của từng trích dẫn theo lần xuất hiện đầu tiên — SPEC §13.4: không hiện id nội bộ. */
export function citeNumbers(report) {
  const numbers = new Map();
  for (const section of report.sections || []) {
    for (const block of section.blocks || []) {
      for (const cite of block.cites || []) {
        if (!numbers.has(cite)) numbers.set(cite, numbers.size + 1);
      }
    }
  }
  return numbers;
}

/** Nguyên văn đoạn nguồn cho mỗi trích dẫn; `paragraphs` là Map `pid` → nội dung. */
export function citeTexts(report, paragraphs) {
  const byUnit = report.unit_sources || {};
  const texts = new Map(paragraphs);
  for (const [unit, pids] of Object.entries(byUnit)) {
    const text = pids
      .map((pid) => paragraphs.get(pid))
      .filter(Boolean)
      .join("\n\n");
    if (text) texts.set(unit, text);
  }
  return texts;
}
