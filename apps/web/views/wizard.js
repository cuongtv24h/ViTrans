// Wizard 3 bước (SPEC §19.7): (1) tải tài liệu + xác nhận quyền, (2) chọn mức & cấu hình + xem giá,
// (3) xác nhận và tạo job. Mọi bước gọi ĐÚNG API của M1 — giao diện không tự tính giá, không tự đoán
// trạng thái; con số hiển thị là con số API trả về (nhờ vậy giao diện không thể "nói dối" người dùng).
//
// Chế độ riêng tư: `privacy_class=private` chỉ hiện khi `/me` nói máy chủ cho phép; khi bật, giao diện
// cảnh báo rõ ràng rằng nhóm LLM dùng chung (chia sẻ dữ liệu để lấy hạn mức miễn phí) sẽ bị loại.

import { ApiError, get, newIdempotencyKey, post } from "../lib/api.js";
import { LEVELS, h, mount, toast, usd, words } from "../lib/ui.js";

const STEPS = ["Tải tài liệu", "Mức dịch & cấu hình", "Xác nhận & chạy"];

export async function render(root) {
  const [me, glossaries, recipes, styleCores] = await Promise.all([
    get("/me"),
    get("/glossaries").catch(() => ({ items: [] })),
    get("/recipes").catch(() => ({ items: [] })),
    get("/style-cores").catch(() => ({ items: [] })),
  ]);

  const draft = {
    document: null,
    level: "full_translation",
    glossary_ids: [],
    recipe_id: null,
    style_core_id: null,
    target_lang: "vi",
    privacy_class: "standard",
    skip_glossary_review: false,
    custom_instructions: "",
    max_cost_usd: null,
    notify_by_email: false,
  };
  let step = 0;
  let quote = null;

  const view = h("div", {});

  function stepper() {
    return h(
      "ol",
      { class: "steps" },
      ...STEPS.map((label, index) =>
        h(
          "li",
          {
            "aria-current": index === step ? "step" : null,
            class: index < step ? "done" : "",
          },
          `${index + 1}. ${label}`,
        ),
      ),
    );
  }

  // ------------------------------------------------------------------ bước 1
  function paintUpload() {
    const file = h("input", { type: "file", required: true, accept: ".txt,.md,.docx,.pdf" });
    const title = h("input", { type: "text", placeholder: "để trống thì lấy tên tệp", maxlength: "200" });
    const rights = h("input", { type: "checkbox" });
    const progress = h("p", { class: "hint", hidden: true });
    const submit = h("button", { class: "primary", type: "submit" }, "Tải lên và bóc tách");

    const form = h(
      "form",
      { class: "panel" },
      h("p", {}, "Nhận tệp .txt, .md, .docx, .pdf. PDF quét (ảnh) vẫn nhận — hệ thống sẽ OCR từng cụm trang."),
      h("label", {}, "Tệp tài liệu", file),
      h("label", {}, "Tiêu đề hiển thị", title),
      h(
        "label",
        { class: "row" },
        rights,
        h(
          "span",
          {},
          "Tôi xác nhận có quyền sử dụng tài liệu này",
          h("br"),
          h("small", { class: "muted" }, "Bắt buộc theo §14.6: không tải lên tài liệu bạn không có quyền xử lý."),
        ),
      ),
      h("p", { class: "hint" }, `Giới hạn dung lượng: ${Math.round((me.limits?.max_upload_bytes || 0) / 1024 / 1024)} MB.`),
      progress,
      h("div", { class: "row", style: "margin-top:12px" }, submit),
    );

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!rights.checked) {
        toast("Cần xác nhận bạn có quyền sử dụng tài liệu.", { error: true });
        return;
      }
      if (!file.files?.[0]) return;
      submit.disabled = true;
      progress.hidden = false;
      progress.textContent = "Đang tải lên và bóc tách — tệp lớn có thể mất vài chục giây…";
      const body = new FormData();
      body.append("file", file.files[0]);
      body.append("rights_attested", "true");
      if (title.value.trim()) body.append("title", title.value.trim());
      try {
        const created = await post("/documents", undefined, { form: body });
        draft.document = created.document;
        toast(
          created.document.words_estimated
            ? "Đã nhận tài liệu quét — số từ hiển thị là ước lượng cho tới khi OCR xong."
            : "Đã bóc tách tài liệu.",
        );
        step = 1;
        paint();
      } catch (error) {
        progress.hidden = true;
        toast(error.message || "Không tải được tài liệu.", { error: true });
      } finally {
        submit.disabled = false;
      }
    });

    mount(view, stepper(), form);
    file.focus();
  }

  // ------------------------------------------------------------------ bước 2
  function paintOptions() {
    const doc = draft.document;
    const level = h(
      "select",
      {},
      ...Object.entries(LEVELS).map(([value, label]) =>
        h("option", { value, selected: value === draft.level }, label),
      ),
    );
    const glossaryBox = h(
      "div",
      {},
      (glossaries.items || []).length
        ? h(
            "ul",
            { class: "clean" },
            ...(glossaries.items || []).map((item) => {
              const box = h("input", { type: "checkbox", checked: draft.glossary_ids.includes(item.id) });
              box.addEventListener("change", () => {
                draft.glossary_ids = box.checked
                  ? [...draft.glossary_ids, item.id]
                  : draft.glossary_ids.filter((id) => id !== item.id);
              });
              return h("li", {}, h("label", { class: "row" }, box, h("span", {}, `${item.name} (${item.entry_count ?? 0} mục)`)));
            }),
          )
        : h("p", { class: "hint" }, "Chưa có glossary nào. ", h("a", { href: "#/glossary" }, "Tạo một cái")),
    );

    const recipe = h(
      "select",
      {},
      h("option", { value: "" }, "Theo mặc định của mức đã chọn"),
      ...(recipes.items || []).map((item) => h("option", { value: item.id, selected: item.id === draft.recipe_id }, item.name || item.id)),
    );
    const styleCore = h(
      "select",
      {},
      h("option", { value: "" }, "Không dùng lõi văn phong"),
      ...(styleCores.items || []).map((item) =>
        h(
          "option",
          {
            value: item.id,
            disabled: !item.latest_approved_version,
            selected: item.id === draft.style_core_id,
          },
          item.latest_approved_version ? item.name : `${item.name} (chưa có bản duyệt)`,
        ),
      ),
    );
    const targetLang = h(
      "select",
      {},
      ...[
        ["vi", "Tiếng Việt"],
        ["en", "Tiếng Anh"],
      ].map(([value, label]) => h("option", { value, selected: value === draft.target_lang }, label)),
    );
    const privateAllowed = (me.available_privacy_classes || []).includes("private");
    const privacy = h(
      "select",
      { disabled: !privateAllowed },
      h("option", { value: "standard" }, "Tiêu chuẩn"),
      h("option", { value: "private" }, "Riêng tư (private)"),
      ...([]),
    );
    const privacyNote = h("p", { class: "hint" },
      privateAllowed
        ? "Chế độ riêng tư loại bỏ mọi nhóm LLM dùng chung (free tier) khỏi job này."
        : "Máy chủ chưa bật chế độ riêng tư (admin bật bằng app_settings.private_enabled).",
    );
    const skipGate = h("input", { type: "checkbox", checked: draft.skip_glossary_review });
    const instructions = h("textarea", { maxlength: "1000", placeholder: "Ví dụ: giữ nguyên tên riêng, dùng văn phong học thuật…" });
    const costCap = h("input", { type: "number", min: "0.01", step: "0.01", placeholder: "ví dụ 0.50" });
    const notify = h("input", { type: "checkbox" });

    const quoteBox = h("div", { class: "panel" }, h("p", { class: "hint" }, "Chưa có báo giá."));

    const readDraft = () => {
      draft.level = level.value;
      draft.recipe_id = recipe.value || null;
      draft.style_core_id = styleCore.value || null;
      draft.target_lang = targetLang.value;
      draft.privacy_class = privacy.value;
      draft.skip_glossary_review = skipGate.checked;
      draft.custom_instructions = instructions.value;
      draft.max_cost_usd = costCap.value ? Number(costCap.value) : null;
      draft.notify_by_email = notify.checked;
    };

    const estimate = h("button", { type: "button", class: "primary" }, "Xem giá & số dư");
    estimate.addEventListener("click", async () => {
      readDraft();
      estimate.disabled = true;
      try {
        const result = await post("/jobs/estimate", { document_id: draft.document.id, ...jobPayload() });
        quote = result;
        mount(
          quoteBox,
          h("h3", { style: "margin-top:0" }, "Báo giá"),
          h("div", { class: "grid" },
            h("div", {}, h("div", { class: "muted" }, "Tín dụng"), h("strong", {}, String(result.credits))),
            h("div", {}, h("div", { class: "muted" }, "Chi phí thật (ước tính)"), h("strong", {}, usd(result.cost_usd))),
            h("div", {}, h("div", { class: "muted" }, "Thời gian"), h("strong", {}, `${result.minutes_low}–${result.minutes_high} phút`)),
            h("div", {}, h("div", { class: "muted" }, "Token vào/ra"), h("strong", {}, `${result.tokens_in} / ${result.tokens_out}`)),
          ),
          h("p", { class: "hint" }, `Bạn đang có ${me.credits} tín dụng. Bảng giá: ${result.price_model}.`),
        );
      } catch (error) {
        toast(error.message || "Không lấy được báo giá.", { error: true });
      } finally {
        estimate.disabled = false;
      }
    });

    const next = h("button", { class: "primary", type: "submit" }, "Tiếp tục");
    const form = h(
      "form",
      { class: "panel" },
      h("div", { class: "row between" },
        h("div", {},
          h("strong", {}, draft.document.title || "Tài liệu"),
          h("br"),
          h("small", { class: "muted" }, `${words(draft.document.word_count)} · ${draft.document.language_code || "?"} → ${draft.target_lang}`),
        ),
        draft.document.needs_ocr
          ? h("span", { class: "badge warn" }, `Có ${draft.document.ocr_chunks?.length || "nhiều"} cụm trang cần OCR`)
          : h("span", { class: "badge ok" }, "Đã bóc tách chữ"),
      ),
      h("div", { class: "grid" },
        h("div", {}, h("label", {}, "Mức xử lý", level)),
        h("div", {}, h("label", {}, "Ngôn ngữ đích", targetLang)),
        h("div", {}, h("label", {}, "Công thức (recipe)", recipe)),
        h("div", {}, h("label", {}, "Lõi văn phong", styleCore)),
        h("div", {}, h("label", {}, "Chế độ riêng tư", privacy), privacyNote),
        h("div", {}, h("label", {}, "Trần chi phí (USD, tuỳ chọn)", costCap), h("p", { class: "hint" }, "Chạm trần thì job tự dừng, phần chưa làm được hoàn lại theo chính sách.")),
      ),
      h("label", {}, "Glossary dùng cho job này", glossaryBox),
      h("label", { class: "row" }, skipGate, h("span", {}, "Bỏ qua cổng duyệt thuật ngữ (dịch ngay, không dừng chờ tôi duyệt)")),
      h("label", {}, "Yêu cầu riêng", instructions),
      h("label", { class: "row" }, notify, h("span", {}, "Báo cho tôi qua email khi xong")),
      quoteBox,
      h("div", { class: "row", style: "margin-top:12px" },
        h("button", { type: "button", on: { click: () => { readDraft(); step = 0; paint(); } } }, "← Quay lại"),
        estimate,
        next,
      ),
    );

    function jobPayload() {
      return {
        document_id: draft.document.id,
        level: draft.level,
        recipe_id: draft.recipe_id,
        glossary_ids: draft.glossary_ids,
        style_core_id: draft.style_core_id,
        target_lang: draft.target_lang,
        privacy_class: draft.privacy_class,
        skip_glossary_review: draft.skip_glossary_review,
        custom_instructions: draft.custom_instructions,
        max_cost_usd: draft.max_cost_usd,
        notify_by_email: draft.notify_by_email,
      };
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      readDraft();
      step = 2;
      paint();
    });

    mount(view, stepper(), form);
  }

  // ------------------------------------------------------------------ bước 3
  async function paintConfirm() {
    const balance = await get("/credits").catch(() => null);
    const payload = {
      document_id: draft.document.id,
      level: draft.level,
      recipe_id: draft.recipe_id,
      glossary_ids: draft.glossary_ids,
      style_core_id: draft.style_core_id,
      target_lang: draft.target_lang,
      privacy_class: draft.privacy_class,
      skip_glossary_review: draft.skip_glossary_review,
      custom_instructions: draft.custom_instructions,
      max_cost_usd: draft.max_cost_usd,
      notify_by_email: draft.notify_by_email,
    };

    const start = h("button", { class: "primary" }, "Tạo job và bắt đầu");
    start.addEventListener("click", async () => {
      start.disabled = true;
      try {
        const created = await post("/jobs", payload, { headers: { "Idempotency-Key": newIdempotencyKey() } });
        toast("Đã tạo job.");
        location.hash = `#/job/${created.job.id}`;
      } catch (error) {
        start.disabled = false;
        if (error instanceof ApiError && error.code === "insufficient_credits") {
          toast("Không đủ tín dụng cho mức đã chọn.", { error: true });
        } else {
          toast(error.message || "Không tạo được job.", { error: true });
        }
      }
    });

    mount(
      view,
      stepper(),
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Xác nhận"),
        h("div", { class: "grid" },
          h("div", {}, h("div", { class: "muted" }, "Tài liệu"), h("strong", {}, draft.document.title || "—")),
          h("div", {}, h("div", { class: "muted" }, "Mức"), h("strong", {}, LEVELS[draft.level])),
          h("div", {}, h("div", { class: "muted" }, "Ngôn ngữ đích"), h("strong", {}, draft.target_lang === "vi" ? "Tiếng Việt" : "Tiếng Anh")),
          h("div", {}, h("div", { class: "muted" }, "Riêng tư"), h("strong", {}, draft.privacy_class === "private" ? "Chỉ nhóm private" : "Tiêu chuẩn")),
          h("div", {}, h("div", { class: "muted" }, "Cổng thuật ngữ"), h("strong", {}, draft.skip_glossary_review ? "Bỏ qua" : "Dừng chờ tôi duyệt")),
          h("div", {}, h("div", { class: "muted" }, "Tín dụng hiện có"), h("strong", {}, balance?.balance ?? me.credits ?? "—")),
          quote ? h("div", {}, h("div", { class: "muted" }, "Giá đã báo"), h("strong", {}, `${quote.credits} tín dụng (${usd(quote.cost_usd)})`)) : null,
        ),
        draft.custom_instructions ? h("blockquote", {}, draft.custom_instructions) : null,
        h("p", { class: "hint" }, "Tạo job sẽ trừ tín dụng ngay theo báo giá; phần chưa chạy được hoàn lại theo chính sách."),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { type: "button", on: { click: () => { step = 1; paint(); } } }, "← Quay lại"),
          start,
        ),
      ),
    );
  }

  function paint() {
    if (step === 0) paintUpload();
    else if (step === 1) paintOptions();
    else paintConfirm();
    window.scrollTo({ top: 0 });
  }

  root.replaceChildren(h("h1", {}, "Dịch tài liệu mới"), view);
  paint();
}
