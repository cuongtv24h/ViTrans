// Tiến độ job + CỔNG DUYỆT THUẬT NGỮ (SPEC §6.4, §19.7 bước 4).
//
// Cổng: khi tài liệu đủ lớn, job dừng ở `awaiting_glossary` và đây là nơi người dùng chốt cách dịch
// thuật ngữ trước khi trả tiền dịch. Trang này cố tình đặt phần duyệt LÊN TRƯỚC phần tiến độ: việc
// cần người quyết định phải nhìn thấy ngay, không bị chôn dưới danh sách giai đoạn.
//
// Tiến độ đi qua SSE `/jobs/{id}/events` (cookie phiên tự gửi kèm, event có `id` nên `EventSource`
// tự phát lại sau khi mất mạng — `Last-Event-ID` do trình duyệt gắn).

import { get, post } from "../lib/api.js";
import { STAGE_LABEL, h, mount, statusBadge, toast, usd, when } from "../lib/ui.js";

export async function render(root, { id }) {
  const view = h("div", {});
  let stream = null;
  // Danh sách glossary cá nhân để chọn nơi lưu mục đã chốt (không bắt buộc — cổng vẫn dùng được).
  const glossaryChoices = await get("/glossaries")
    .then((data) => (data.items || []).filter((item) => item.scope === "personal"))
    .catch(() => []);

  const stop = () => {
    if (stream) {
      stream.close();
      stream = null;
    }
  };

  async function load() {
    const data = await get(`/jobs/${id}`);
    paint(data);
    return data;
  }

  function stageList(data) {
    const stages = data.stages || [];
    const active = stages.filter((s) => s.status === "running").map((s) => s.stage);
    return h(
      "ul",
      { class: "clean" },
      ...stages.map((stage) => {
        const failed = stage.status === "failed";
        return h(
          "li",
          {},
          h(
            "div",
            { class: "row between" },
            h("strong", {}, STAGE_LABEL[stage.stage] || stage.stage),
            failed
              ? h("span", { class: "badge danger" }, "Lỗi")
              : h("span", { class: `badge ${stage.status === "succeeded" ? "ok" : stage.status === "running" ? "warn" : ""}`.trim() }, stage.status),
          ),
          stage.metrics && Object.keys(stage.metrics).length
            ? h("small", { class: "muted" }, Object.entries(stage.metrics).slice(0, 6).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "))
            : null,
          failed && stage.error ? h("small", { class: "muted" }, String(stage.error).slice(0, 200)) : null,
        );
      }),
      ...(active.length ? [] : []),
    );
  }

  async function glossaryGate(data) {
    const box = h("section", { class: "panel" });
    const payload = await get(`/jobs/${id}/glossary`);
    const entries = payload.entries || [];

    if (!entries.length) {
      mount(box, h("h2", { style: "margin-top:0" }, "Cổng thuật ngữ"), h("p", { class: "hint" }, "Job này không có mục thuật ngữ nào cần duyệt."));
      return box;
    }

    const rows = entries.map((entry) => {
      const target = h("input", { type: "text", value: entry.target_term || "" });
      const keep = h("input", { type: "checkbox", checked: Boolean(entry.keep_original) });
      const skip = h("input", { type: "checkbox", checked: entry.status === "rejected" });
      return {
        entry,
        target,
        keep,
        skip,
        row: h(
          "tr",
          {},
          h(
            "td",
            {},
            h("strong", {}, entry.source_term),
            h("br"),
            h("small", { class: "muted" }, `${entry.term_type || "concept"} · độ tin cậy ${Math.round((entry.confidence ?? 0) * 100)}%`),
          ),
          h("td", {}, target),
          h("td", {}, h("label", { class: "row" }, keep, h("span", {}, "giữ nguyên"))),
          h("td", {}, h("label", { class: "row" }, skip, h("span", {}, "bỏ qua"))),
          h("td", {}, entry.note ? h("small", { class: "muted" }, entry.note) : null),
        ),
      };
    });

    const saveTo = h(
      "select",
      {},
      h("option", { value: "" }, "Không lưu vào glossary"),
      ...glossaryChoices.map((item) => h("option", { value: item.id }, `Lưu vào: ${item.name}`)),
    );
    const confirm = h("button", { class: "primary" }, "Chốt thuật ngữ và chạy tiếp");
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      try {
        const chosen = rows
          .filter((row) => !row.skip.checked)
          .map((row) => ({
            source_term: row.entry.source_term,
            target_term: row.target.value.trim() || row.entry.target_term,
            keep_original: row.keep.checked,
            case_sensitive: Boolean(row.entry.case_sensitive),
            forbidden_variants: row.entry.forbidden_variants || [],
          }));
        if (!chosen.length) throw new Error("Cần giữ lại ít nhất một thuật ngữ.");
        await post(`/jobs/${id}/glossary/confirm`, { entries: chosen, save_to_glossary_id: saveTo.value || null });
        toast("Đã chốt thuật ngữ, job chạy tiếp.");
        await load();
      } catch (error) {
        toast(error.message || "Không chốt được thuật ngữ.", { error: true });
      } finally {
        confirm.disabled = false;
      }
    });

    mount(
      box,
      h("h2", { style: "margin-top:0" }, "Cổng thuật ngữ — cần bạn quyết định"),
      h(
        "p",
        { class: "hint" },
        payload.review_deadline
          ? `Tự động xác nhận các mục có độ tin cậy ≥ 70% sau ${when(payload.review_deadline)} nếu bạn không duyệt.`
          : "Các mục có độ tin cậy ≥ 70% sẽ tự được xác nhận nếu bạn không duyệt trước hạn.",
        payload.auto_confirmed ? " (Job này đã từng được tự động xác nhận.)" : "",
      ),
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", {}, "Thuật ngữ nguồn"), h("th", {}, "Cách dịch"), h("th", {}, "Giữ nguyên"), h("th", {}, "Bỏ qua"), h("th", {}, "Số lần"))),
          h("tbody", {}, ...rows.map((row) => row.row)),
        ),
      ),
      h(
        "div",
        { class: "row", style: "margin-top:12px" },
        confirm,
        h("label", { class: "row" }, saveTo),
        h("small", { class: "muted" }, "Mục bị bỏ qua sẽ không được dùng trong bản dịch."),
      ),
    );
    return box;
  }

  function paint(data) {
    const job = data.job;
    const done = ["succeeded", "failed", "canceled", "expired"].includes(job.status);
    const stages = data.stages || [];
    const finished = stages.filter((s) => ["succeeded", "skipped"].includes(s.status)).length;
    const percent = stages.length ? Math.round((finished / stages.length) * 100) : 0;

    mount(
      view,
      h(
        "div",
        { class: "row between" },
        h("h1", { style: "margin:0" }, data.document?.title || "Job"),
        statusBadge(job.status),
      ),
      h(
        "p",
        { class: "muted" },
        `${job.level} · ${job.source_lang} → ${job.target_lang} · tạo lúc ${when(job.created_at)}`,
        job.est_cost_usd ? ` · dự trù ${usd(job.est_cost_usd)}` : "",
        job.actual_shadow_usd ? ` · đã dùng thực ${usd(job.actual_shadow_usd)}` : "",
      ),
      h("div", { class: "progress", "aria-label": "Tiến độ giai đoạn" }, h("span", { style: `width:${percent}%` })),

      job.status === "awaiting_glossary" ? h("div", { class: "warnbox" }, "Job đang dừng ở cổng thuật ngữ. Chốt xong thì job chạy tiếp.") : null,
      job.status === "failed" ? h("div", { class: "warnbox" }, `Job lỗi: ${job.error_code || ""} ${job.error_message || ""}`) : null,
      job.status === "succeeded" && data.report_id
        ? h("p", {}, h("a", { class: "badge ok", href: `#/bao-cao/${data.report_id}`, style: "padding:9px 14px;text-decoration:none" }, "Đọc báo cáo →"))
        : null,

      job.status === "awaiting_glossary" ? h("div", { id: "gate" }) : null,
      h("section", { class: "panel" }, h("h2", { style: "margin-top:0" }, "Các giai đoạn"), stageList(data)),
      !done
        ? h(
            "div",
            { class: "row" },
            h(
              "button",
              {
                class: "danger",
                on: {
                  click: async () => {
                    try {
                      await post(`/jobs/${id}/cancel`, {});
                      toast("Đã yêu cầu huỷ job.");
                      await load();
                    } catch (error) {
                      toast(error.message || "Không huỷ được.", { error: true });
                    }
                  },
                },
              },
              "Huỷ job",
            ),
            h("small", { class: "muted" }, "Job đang chạy sẽ dừng ở ranh giới giai đoạn; job chưa chạy được hoàn đủ tín dụng."),
          )
        : null,
    );

    if (job.status === "awaiting_glossary") {
      glossaryGate(data).then((box) => {
        const slot = view.querySelector("#gate");
        if (slot) slot.replaceWith(box);
      });
    }
    if (!done) {
      stop();
      stream = new EventSource(`/api/v1/jobs/${id}/events`);
      stream.addEventListener("stage", () => load());
      stream.addEventListener("status", () => load());
      stream.addEventListener("completed", () => {
        load();
        stop();
      });
      stream.addEventListener("error", () => {
        /* `EventSource` tự kết nối lại; không làm phiền người dùng */
      });
    } else {
      stop();
    }
  }

  root.replaceChildren(view);
  await load();
  return stop;
}
