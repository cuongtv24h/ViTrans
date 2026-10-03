// Tổng quan: tài liệu đã tải, job đang chạy, báo cáo đã xong. Đây là nơi người dùng quay lại nhiều
// nhất, nên nút "Dịch tài liệu mới" nằm ngay dòng đầu và mọi việc đang dở được đẩy lên trên.
//
// `GET /jobs` không kèm tiêu đề tài liệu hay id báo cáo (hợp đồng của API giữ hàng `jobs` thuần), nên
// trang này tự nối ba danh sách — 3 lời gọi song song, không thêm điểm cuối riêng cho giao diện.

import { get } from "../lib/api.js";
import { excerpt } from "../lib/md.js";
import { LEVELS, h, mount, statusBadge, when, words } from "../lib/ui.js";

export async function render(root) {
  const [jobs, documents, reports] = await Promise.all([
    get("/jobs?limit=8"),
    get("/documents?limit=30"),
    get("/reports?limit=30"),
  ]);

  const docTitle = new Map((documents.items || []).map((doc) => [doc.id, doc.title || "Không tiêu đề"]));
  const reportOfJob = new Map((reports.items || []).map((item) => [item.job_id, item]));

  const jobRow = (job) => {
    const report = reportOfJob.get(job.id);
    const active = ["queued", "running", "awaiting_glossary"].includes(job.status);
    return h(
      "li",
      {},
      h("div", { class: "row between" }, h("strong", {}, docTitle.get(job.document_id) || "Tài liệu"), statusBadge(job.status)),
      h(
        "small",
        { class: "muted" },
        `${LEVELS[job.level] || job.level} · ${words(job.source_words)} · tạo lúc ${when(job.created_at)}`,
      ),
      h(
        "div",
        { class: "row", style: "margin-top:6px" },
        job.status === "awaiting_glossary"
          ? h("a", { href: `#/job/${job.id}` }, h("span", { class: "badge warn" }, "Duyệt thuật ngữ để chạy tiếp"))
          : null,
        report ? h("a", { href: `#/bao-cao/${report.id}` }, "Đọc báo cáo") : null,
        active || !report ? h("a", { class: "muted", href: `#/job/${job.id}` }, active ? "Xem tiến độ" : "Chi tiết job") : null,
      ),
    );
  };

  mount(
    root,
    h(
      "div",
      { class: "row between" },
      h("h1", { style: "margin:0" }, "Tổng quan"),
      h("a", { class: "badge ok", href: "#/tai-lieu", style: "padding:9px 14px;text-decoration:none" }, "+ Dịch tài liệu mới"),
    ),

    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Việc đang làm"),
      jobs.items?.length
        ? h("ul", { class: "clean" }, ...jobs.items.map(jobRow))
        : h("p", { class: "hint" }, "Chưa có job nào. Bắt đầu bằng cách tải một tài liệu lên."),
    ),

    h(
      "div",
      { class: "grid" },
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Tài liệu đã tải"),
        documents.items?.length
          ? h(
              "ul",
              { class: "clean" },
              ...documents.items.map((doc) =>
                h(
                  "li",
                  {},
                  h("strong", {}, doc.title || "Không tiêu đề"),
                  h("br"),
                  h(
                    "small",
                    { class: "muted" },
                    `${words(doc.word_count)} · ${doc.status} · ${when(doc.created_at)}`,
                    doc.needs_ocr ? " · cần OCR" : "",
                    doc.source_kind === "ocr" ? " · đã OCR" : "",
                  ),
                  doc.summary ? h("br") : null,
                  doc.summary ? h("small", { class: "muted" }, excerpt(doc.summary, 120)) : null,
                ),
              ),
            )
          : h("p", { class: "hint" }, "Chưa có tài liệu."),
      ),
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Báo cáo gần đây"),
        reports.items?.length
          ? h(
              "ul",
              { class: "clean" },
              ...reports.items.map((item) =>
                h(
                  "li",
                  {},
                  h("a", { href: `#/bao-cao/${item.id}` }, item.title || "Báo cáo"),
                  h("br"),
                  h(
                    "small",
                    { class: "muted" },
                    `${LEVELS[item.level] || item.level} · hạng ${item.quality_grade || "?"} · ${when(item.created_at)}`,
                  ),
                ),
              ),
            )
          : h("p", { class: "hint" }, "Chưa có báo cáo nào."),
      ),
    ),
  );
}
