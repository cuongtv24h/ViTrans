// Trang đọc báo cáo (SPEC §6.8–§6.10, §19.7 bước 5): mục → khối → TRÍCH DẪN bấm được, cờ đoạn sai,
// chấm sao, và xuất MD/DOCX/HTML/PDF (bản song ngữ cho mức dịch đầy đủ).
//
// Trích dẫn là phần quan trọng nhất của sản phẩm ("kiểm chứng nguồn"): mỗi khối hiển thị danh sách
// `pid` của đoạn nguồn. Bấm vào trích dẫn mở khung đối chiếu ngay trong trang — người dùng không phải
// tin lời AI, họ đọc được nguyên văn câu nguồn mà khối đó dựa vào.

import { ApiError, download, get, post } from "../lib/api.js";
import { citeNumbers, citeTexts, sourcePids } from "../lib/cites.js";
import { markdownToHtml, splitTranslation } from "../lib/md.js";
import { FLAG_REASONS, LEVELS, h, mount, toast, usd, when, words } from "../lib/ui.js";

export async function render(root, { id }) {
  const report = await get(`/reports/${id}`);
  const view = h("div", {});
  const cache = new Map();

  /** Nguyên văn đoạn nguồn, tải một lần cho cả trang rồi dùng lại cho mọi trích dẫn. */
  async function sourceParagraphs() {
    if (cache.has("source")) return cache.get("source");
    const pids = sourcePids(report);
    const paragraphs = new Map();
    if (pids.length) {
      const query = pids.slice(0, 200).join(",");
      const data = await get(`/documents/${report.document_id}/paragraphs?pids=${encodeURIComponent(query)}`).catch(() => ({ items: [] }));
      for (const item of data.items || []) paragraphs.set(item.pid, item.content);
    }
    const map = citeTexts(report, paragraphs);
    cache.set("source", map);
    return map;
  }

  const numbers = citeNumbers(report);

  function citationChip(cite, sourceBlock) {
    const number = numbers.get(cite) || 1;
    return h(
      "button",
      {
        class: "cite",
        type: "button",
        title: `Xem đoạn nguồn của trích dẫn [${number}]`,
        on: {
          click: async () => {
            sourceBlock.replaceChildren(h("p", { class: "busy" }, "Đang mở đoạn nguồn…"));
            const map = await sourceParagraphs().catch(() => new Map());
            const text = map.get(cite);
            mount(
              sourceBlock,
              h(
                "div",
                { class: "panel" },
                h(
                  "div",
                  { class: "row between" },
                  h("strong", {}, `Nguyên văn nguồn [${number}]`),
                  h("button", { class: "link", on: { click: () => sourceBlock.replaceChildren() } }, "Đóng"),
                ),
                h("p", {}, text || "Không tìm thấy đoạn này (tài liệu có thể đã bị xoá theo hạn lưu)."),
              ),
            );
          },
        },
      },
      `[${number}]`,
    );
  }

  function plainBlockView(block) {
    const source = h("div", {});
    return h(
      "article",
      { class: "block" },
      h("div", { html: markdownToHtml(block.markdown || "") }),
      (block.cites || []).length ? h("div", { class: "row" }, ...block.cites.map((cite) => citationChip(cite, source))) : null,
      source,
    );
  }

  function translationView(markdown) {
    return h(
      "div",
      { class: "reader" },
      ...splitTranslation(markdown).map((block) =>
        block.heading ? h("h2", {}, block.heading) : plainBlockView(block),
      ),
    );
  }

  function blockView(block) {
    const flagged = block.verdict === "flagged" || (block.issues || []).length > 0;
    const source = h("div", {});
    const flagBox = h("div", { hidden: true });
    const comment = h("textarea", { maxlength: "2000", placeholder: "Mô tả ngắn (tuỳ chọn)" });
    const reason = h(
      "select",
      {},
      ...Object.entries(FLAG_REASONS).map(([value, label]) => h("option", { value }, label)),
    );
    const send = h("button", { class: "primary" }, "Gửi báo lỗi");
    send.addEventListener("click", async () => {
      send.disabled = true;
      try {
        await post(`/reports/${id}/blocks/${block.block_id}/flag`, { reason: reason.value, comment: comment.value || null });
        toast("Đã ghi nhận báo lỗi. Cảm ơn bạn!");
        flagBox.hidden = true;
      } catch (error) {
        toast(error.message || "Không gửi được báo lỗi.", { error: true });
      } finally {
        send.disabled = false;
      }
    });

    return h(
      "article",
      { class: "block", "data-flagged": String(flagged) },
      h("div", { html: markdownToHtml(block.markdown_vi || "") }),
      h(
        "div",
        { class: "row" },
        ...(block.cites || []).map((cite) => citationChip(cite, source)),
        flagged ? h("span", { class: "badge warn", text: (block.issues || [])[0]?.code || "đã bị đánh cờ" }) : null,
        h(
          "button",
          {
            class: "link",
            type: "button",
            on: { click: () => { flagBox.hidden = !flagBox.hidden; } },
          },
          "Báo lỗi đoạn này",
        ),
      ),
      h("div", { class: "row" }, reason, comment, send),
      h("div", {}),
      source,
    );
  }

  const exportBar = () => {
    const buttons = [
      ["md", "Markdown"],
      ["docx", "Word (.docx)"],
      ["pdf", "PDF"],
      ["html", "HTML"],
    ].map(([format, label]) =>
      h(
        "button",
        {
          type: "button",
          on: {
            click: async (event) => {
              const button = event.currentTarget;
              button.disabled = true;
              try {
                await download(`/reports/${id}/export?format=${format}`, `${slug(report.title)}.${format}`);
              } catch (error) {
                toast(error.message || "Không xuất được tệp.", { error: true });
              } finally {
                button.disabled = false;
              }
            },
          },
        },
        label,
      ),
    );
    const bilingual =
      report.level === "full_translation"
        ? h(
            "button",
            {
              type: "button",
              title: "Xuất bản ghép nguyên văn nguồn và bản dịch theo từng đoạn",
              on: {
                click: async (event) => {
                  event.currentTarget.disabled = true;
                  try {
                    await download(`/reports/${id}/export?format=md&bilingual=true`, `${slug(report.title)}-song-ngu.md`);
                  } catch (error) {
                    toast(error.message || "Không xuất được bản song ngữ.", { error: true });
                  } finally {
                    event.currentTarget.disabled = false;
                  }
                },
              },
            },
            "Song ngữ (Markdown)",
          )
        : null;
    return h("div", { class: "panel" }, h("div", { class: "row" }, h("strong", {}, "Xuất tệp:"), ...buttons, bilingual),
      h("small", { class: "muted" }, "Mọi tệp xuất đều kèm dòng \"Nội dung do AI tổng hợp\" ở đầu và cuối (Luật AI 134/2025/QH15)."));
  };

  const send = h("button", { class: "primary" }, "Gửi đánh giá");
  const stars = h("select", {}, ...[5, 4, 3, 2, 1].map((n) => h("option", { value: String(n) }, `${n} sao`)));
  const feedbackComment = h("textarea", { maxlength: "2000", placeholder: "Điều gì hữu ích, điều gì cần sửa?" });
  send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      await post(`/reports/${id}/feedback`, { stars: Number(stars.value), comment: feedbackComment.value || null });
      toast("Đã gửi đánh giá.");
    } catch (error) {
      toast(error.message || "Không gửi được đánh giá.", { error: true });
    } finally {
      send.disabled = false;
    }
  });

  mount(
    view,
    h("h1", {}, report.title || "Báo cáo"),
    h(
      "p",
      { class: "muted" },
      `${LEVELS[report.level] || report.level} · hạng chất lượng ${report.quality_grade || "?"} · ${words(report.stats?.report_words)} · ${when(report.created_at)}`,
      report.stats?.faithfulness_rate !== null && report.stats?.faithfulness_rate !== undefined
        ? ` · độ trung thực ${Math.round(report.stats.faithfulness_rate * 100)}%`
        : "",
    ),
    h("div", { class: "warnbox" }, report.ai_notice),
    exportBar(),
    h("div", { class: "reader" },
      ...(report.sections || []).map((section) =>
        h(
          "section",
          {},
          h("h2", {}, section.title_vi || "Phần"),
          ...(section.blocks || []).map(blockView),
        ),
      ),
      !(report.sections || []).length ? translationView(report.markdown || "") : null,
    ),
    h(
      "section",
      { class: "panel" },
      h("h3", { style: "margin-top:0" }, "Đánh giá báo cáo này"),
      h("div", { class: "row" }, stars, send),
      h("label", {}, "Nhận xét", feedbackComment),
    ),
    h(
      "p",
      {},
      h("a", { href: "#/" }, "← Về tổng quan"),
    ),
  );

  root.replaceChildren(view);
}

function slug(text) {
  return String(text || "bao-cao")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/đ/gi, "d")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase()
    .slice(0, 60) || "bao-cao";
}
