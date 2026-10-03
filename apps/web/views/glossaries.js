// Quản lý glossary cá nhân: tạo, thêm/sửa/xoá mục, nhập–xuất CSV.
//
// Glossary là thứ người dùng mang từ tài liệu này sang tài liệu khác, nên giao diện tối ưu cho việc
// dán hàng loạt (ô "dán nhanh": mỗi dòng `nguồn = đích` hoặc `nguồn,đích`) hơn là nhập từng ô một.

import { del, download, get, patch, post } from "../lib/api.js";
import { h, mount, toast } from "../lib/ui.js";

export async function render(root) {
  const view = h("div", {});
  let selected = null;
  let glossaries = [];

  async function load() {
    const data = await get("/glossaries");
    glossaries = data.items || [];
    if (!selected || !glossaries.some((item) => item.id === selected)) selected = glossaries[0]?.id || null;
    await paint();
  }

  async function paint() {
    const list = h(
      "ul",
      { class: "clean" },
      ...glossaries.map((item) =>
        h(
          "li",
          { class: item.id === selected ? "row between" : "row between" },
          h(
            "button",
            {
              class: "link",
              on: {
                click: () => {
                  selected = item.id;
                  paint();
                },
              },
            },
            item.name,
          ),
          h("small", { class: "muted" }, `${item.entry_count ?? 0} mục · ${item.scope === "personal" ? "cá nhân" : item.scope}`),
        ),
      ),
    );

    const createName = h("input", { placeholder: "Tên glossary mới" });
    const createDomain = h("input", { placeholder: "Lĩnh vực (ví dụ: y học)" });
    const createButton = h("button", { class: "primary" }, "Tạo glossary");
    createButton.addEventListener("click", async () => {
      if (!createName.value.trim()) return;
      createButton.disabled = true;
      try {
        const created = await post("/glossaries", { name: createName.value.trim(), domain: createDomain.value.trim() || null });
        selected = created.id;
        toast("Đã tạo glossary.");
        await load();
      } catch (error) {
        toast(error.message || "Không tạo được glossary.", { error: true });
      } finally {
        createButton.disabled = false;
      }
    });

    const detail = h("section", { class: "panel" }, h("p", { class: "busy" }, "Đang tải…"));

    mount(
      view,
      h("h1", {}, "Thuật ngữ"),
      h(
        "p",
        { class: "hint" },
        "Cách dịch thuật ngữ được cố định ở đây sẽ được pipeline dùng cho mọi tài liệu bạn chọn glossary này.",
      ),
      h("div", { class: "grid" },
        h(
          "section",
          { class: "panel" },
          h("h2", { style: "margin-top:0" }, "Glossary của tôi"),
          glossaries.length ? list : h("p", { class: "hint" }, "Chưa có glossary nào."),
          h("label", {}, "Tạo mới", createName),
          h("label", {}, "Lĩnh vực", createDomain),
          h("div", { class: "row", style: "margin-top:10px" }, createButton),
        ),
        detail,
      ),
    );

    if (!selected) {
      mount(detail, h("h2", { style: "margin-top:0" }, "Nội dung"), h("p", { class: "hint" }, "Chọn hoặc tạo một glossary để thêm thuật ngữ."));
      return;
    }
    await paintDetail(detail);
  }

  async function paintDetail(box) {
    const [data, listing] = await Promise.all([
      get(`/glossaries/${selected}`),
      get(`/glossaries/${selected}/entries`),
    ]);
    const items = listing.items || [];
    const meta = data || {};

    const rows = items.map((entry) => {
      const target = h("input", { type: "text", value: entry.target_term, maxlength: "160" });
      const note = h("input", { type: "text", value: entry.note || "", maxlength: "300", placeholder: "ghi chú" });
      const save = h("button", { class: "link" }, "Lưu");
      save.addEventListener("click", async () => {
        try {
          // `PATCH` nhận cả mục (không phải bản vá từng trường) — gửi lại đủ trường để không mất dữ liệu.
          await patch(`/glossaries/${selected}/entries/${entry.id}`, {
            source_term: entry.source_term,
            target_term: target.value.trim() || entry.target_term,
            keep_original: Boolean(entry.keep_original),
            case_sensitive: Boolean(entry.case_sensitive),
            forbidden_variants: entry.forbidden_variants || [],
            term_type: entry.term_type || "concept",
            note: note.value.trim() || null,
          });
          toast("Đã lưu mục thuật ngữ.");
        } catch (error) {
          toast(error.message || "Không lưu được.", { error: true });
        }
      });
      const remove = h("button", { class: "link danger" }, "Xoá");
      remove.addEventListener("click", async () => {
        try {
          await del(`/glossaries/${selected}/entries/${entry.id}`);
          toast("Đã xoá mục.");
          await paintDetail(box);
        } catch (error) {
          toast(error.message || "Không xoá được.", { error: true });
        }
      });
      return h(
        "tr",
        {},
        h("td", {}, h("strong", {}, entry.source_term), h("br"), h("small", { class: "muted" }, entry.term_type || "concept")),
        h("td", {}, target),
        h("td", {}, note),
        h("td", {}, h("div", { class: "row" }, save, remove)),
      );
    });

    // Dán nhanh: mỗi dòng "nguồn = đích" hoặc "nguồn,đích" — nhanh hơn nhập từng ô cho danh sách dài.
    const bulk = h("textarea", { placeholder: "Mỗi dòng một thuật ngữ:\nmachine learning = học máy\ninference, suy luận" });
    const bulkButton = h("button", { class: "primary" }, "Thêm các dòng này");
    bulkButton.addEventListener("click", async () => {
      const lines = bulk.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      const parsed = lines
        .map((line) => {
          const parts = line.includes("=") ? line.split("=") : line.split(",");
          return { source_term: (parts[0] || "").trim(), target_term: (parts.slice(1).join(",") || "").trim() };
        })
        .filter((item) => item.source_term && item.target_term);
      if (!parsed.length) {
        toast("Không đọc được dòng nào. Dạng đúng: `nguồn = cách dịch`.", { error: true });
        return;
      }
      bulkButton.disabled = true;
      try {
        // Điểm cuối nhận TỪNG mục; gửi lần lượt và đếm cả mục bị trùng để người dùng biết chính xác.
        const results = await Promise.allSettled(
          parsed.map((item) => post(`/glossaries/${selected}/entries`, { ...item, term_type: "concept" })),
        );
        const added = results.filter((r) => r.status === "fulfilled").length;
        const failed = results.length - added;
        bulk.value = "";
        toast(failed ? `Đã thêm ${added} mục, ${failed} mục bị trùng/lỗi.` : `Đã thêm ${added} mục.`);
        await paintDetail(box);
      } catch (error) {
        toast(error.message || "Không thêm được.", { error: true });
      } finally {
        bulkButton.disabled = false;
      }
    });

    const single = {
      source: h("input", { placeholder: "thuật ngữ nguồn" }),
      target: h("input", { placeholder: "cách dịch" }),
    };
    const singleButton = h("button", {}, "Thêm một mục");
    singleButton.addEventListener("click", async () => {
      if (!single.source.value.trim() || !single.target.value.trim()) return;
      try {
        await post(`/glossaries/${selected}/entries`, {
          source_term: single.source.value.trim(),
          target_term: single.target.value.trim(),
          term_type: "concept",
        });
        single.source.value = "";
        single.target.value = "";
        await paintDetail(box);
      } catch (error) {
        toast(error.message || "Không thêm được mục.", { error: true });
      }
    });

    mount(
      box,
      h("h2", { style: "margin-top:0" }, meta.name || "Glossary"),
      items.length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Thuật ngữ nguồn"), h("th", {}, "Cách dịch"), h("th", {}, "Ghi chú"), h("th", {}))),
              h("tbody", {}, ...rows),
            ),
          )
        : h("p", { class: "hint" }, "Glossary này chưa có mục nào."),
      h("h3", {}, "Thêm nhanh nhiều mục"),
      bulk,
      h("div", { class: "row", style: "margin-top:8px" }, bulkButton),
      h("h3", {}, "Thêm từng mục"),
      h("div", { class: "grid" }, h("div", {}, h("label", {}, "Nguồn", single.source)), h("div", {}, h("label", {}, "Cách dịch", single.target))),
      h("div", { class: "row", style: "margin-top:8px" }, singleButton),
      h(
        "div",
        { class: "row", style: "margin-top:14px" },
        h(
          "button",
          {
            on: {
              click: () =>
                download(`/glossaries/${selected}/export`, "glossary.csv").catch((error) =>
                  toast(error.message || "Không xuất được CSV.", { error: true }),
                ),
            },
          },
          "Xuất CSV",
        ),
        h("small", { class: "muted" }, "Nhập CSV: dùng API `POST /glossaries/{id}/import` (hỗ trợ cả tệp TSV)."),
      ),
    );
  }

  root.replaceChildren(view);
  await load();
}
