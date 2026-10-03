// Tài khoản: số dư tín dụng, lịch sử giao dịch, đồng ý đã ghi nhận, nhập mã mời, tải dữ liệu, xoá tài khoản.
//
// Đây là trang "quyền của người dùng" theo PDPL §14.3: người dùng phải thấy được mình đã đồng ý gì,
// số dư thay đổi vì việc gì, và có đường rời đi rõ ràng (tải dữ liệu / xoá tài khoản).

import { get, normCode, post } from "../lib/api.js";
import { h, mount, toast, usd, when } from "../lib/ui.js";

const REASON = {
  grant_signup: "Tặng khi đăng ký",
  grant_invite: "Tặng theo mã mời",
  charge_job: "Trừ cho job",
  refund_job: "Hoàn do job lỗi/huỷ",
  grant_admin: "Admin cấp",
};

export async function render(root) {
  const [me, ledger] = await Promise.all([get("/me"), get("/credits?limit=25")]);

  const code = h("input", { placeholder: "MÃ-MỜI" });
  const redeem = h("button", { class: "primary" }, "Nhập mã");
  redeem.addEventListener("click", async () => {
    redeem.disabled = true;
    try {
      const result = await post("/invites/redeem", { code: normCode(code.value) });
      toast(`Đã nhận ${result.credits_granted} tín dụng. Số dư: ${result.balance}.`);
      await mount(root);
    } catch (error) {
      toast(error.message || "Mã mời không dùng được.", { error: true });
    } finally {
      redeem.disabled = false;
    }
  });

  const consentBox = h("div", {});
  const consents = [
    ["consent_cross_border", "Chuyển dữ liệu xuyên biên giới (bắt buộc để dịch)"],
    ["consent_shared_processing", "Dùng chung lõi văn phong đã ẩn danh"],
    ["age_confirmed", "Xác nhận đủ tuổi"],
  ];
  const boxes = consents.map(([key, labelText]) => {
    const box = h("input", { type: "checkbox", checked: Boolean(me[key]), disabled: me[key] });
    return { key, box, labelText };
  });
  const saveConsents = h("button", {}, "Ghi nhận đồng ý mới");
  saveConsents.addEventListener("click", async () => {
    saveConsents.disabled = true;
    try {
      await post("/me/consents", {
        tos_version: "2026-10-01",
        consent_cross_border: boxes[0].box.checked,
        consent_shared_processing: boxes[1].box.checked,
        age_confirmed: boxes[2].box.checked,
      });
      toast("Đã ghi nhận.");
      await mount(root);
    } catch (error) {
      toast(error.message || "Không ghi nhận được.", { error: true });
    } finally {
      saveConsents.disabled = false;
    }
  });
  mount(
    consentBox,
    ...boxes.map(({ box, labelText }) => h("label", { class: "row" }, box, h("span", {}, labelText))),
    h("div", { class: "row", style: "margin-top:8px" }, saveConsents),
  );

  const rows = (ledger.items || []).map((item) =>
    h(
      "tr",
      {},
      h("td", {}, when(item.created_at)),
      h("td", {}, REASON[item.reason] || item.reason),
      h("td", {}, item.job_id ? h("a", { href: `#/job/${item.job_id}` }, "xem job") : "—"),
      h("td", { style: `color:${item.delta < 0 ? "var(--danger)" : "var(--ok)"}` }, item.delta > 0 ? `+${item.delta}` : String(item.delta)),
    ),
  );

  mount(
    root,
    h("h1", {}, "Tài khoản"),
    h(
      "div",
      { class: "grid" },
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Số dư"),
        h("p", { style: "font-size:24px;font-weight:700" }, `${me.credits} tín dụng`),
        h("small", { class: "muted" }, `${me.email} · vai trò ${me.role} · ${me.jobs?.active ?? 0} job đang chạy / ${me.jobs?.total ?? 0} tổng`),
        h("label", {}, "Nhập mã mời", code),
        h("div", { class: "row", style: "margin-top:8px" }, redeem),
      ),
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Đồng ý đã ghi nhận"),
        h(
          "p",
          { class: "hint" },
          "Ô đã tích là đồng ý đã ghi vào CSDL (kèm thời điểm). Bỏ tích không rút lại được đồng ý cũ — hãy liên hệ để rút theo PDPL.",
        ),
        consentBox,
      ),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Lịch sử tín dụng"),
      rows.length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Lúc"), h("th", {}, "Lý do"), h("th", {}, "Job"), h("th", {}, "Thay đổi"))),
              h("tbody", {}, ...rows),
            ),
          )
        : h("p", { class: "hint" }, "Chưa có giao dịch nào."),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Dữ liệu & quyền của tôi"),
      h("p", {}, "Xoá tài liệu: vào ", h("a", { href: "#/" }, "Tổng quan"), " rồi xoá từng tài liệu; nội dung gốc bị xoá, bản ghi kiểm toán được giữ lại."),
      h(
        "p",
        { class: "hint" },
        `Chi phí thật của các job đã chạy được hiển thị theo từng job (ví dụ ${usd(0.12)}). `,
        "Yêu cầu tải toàn bộ dữ liệu hoặc xoá tài khoản: dùng mục ",
        h("a", { href: "#/phap-ly" }, "Pháp lý"),
        ".",
      ),
    ),
  );
}
