// Đăng nhập / đăng ký. M1 dùng email + mật khẩu (SPEC §20.2 chọn Google OAuth + OTP — sẽ thay ở M2,
// ghi trong "sai lệch có chủ ý" của docs/BUILD_PLAN.md).
//
// Ba điều bắt buộc khi đăng ký (PDPL §14.3, Luật AI §14.4): đồng ý điều khoản, đồng ý chuyển dữ liệu
// xuyên biên giới (vì LLM là dịch vụ nước ngoài), và xác nhận đủ tuổi. Không có ba ô này thì không tạo
// được tài khoản — biểu mẫu phản ánh ĐÚNG ràng buộc của API, không chỉ trang trí.

import { ApiError, post } from "../lib/api.js";
import { h, mount, toast } from "../lib/ui.js";

const TOS_VERSION = "2026-10-01";

export function render(root, { onSignedIn } = {}) {
  let mode = "login";

  const email = h("input", { type: "email", name: "email", autocomplete: "email", required: true });
  const password = h("input", {
    type: "password",
    name: "password",
    autocomplete: "current-password",
    minlength: "8",
    required: true,
  });
  const displayName = h("input", { type: "text", name: "display_name", maxlength: "120" });
  const invite = h("input", { type: "text", name: "invite_code", placeholder: "MÃ-MỜI (nếu có)" });
  const tos = h("input", { type: "checkbox" });
  const crossBorder = h("input", { type: "checkbox" });
  const shared = h("input", { type: "checkbox" });
  const age = h("input", { type: "checkbox" });

  const errorBox = h("div", { class: "warnbox", hidden: true });
  const submit = h("button", { class: "primary", type: "submit" }, "Đăng nhập");

  const form = h("form", { class: "panel" }, h("div", { class: "grid" }));
  const fields = form.querySelector(".grid");

  function paint() {
    const registering = mode === "register";
    submit.textContent = registering ? "Tạo tài khoản" : "Đăng nhập";
    errorBox.hidden = true;
    mount(
      fields,
      h("div", {}, h("h2", { style: "margin-top:0" }, registering ? "Tạo tài khoản" : "Đăng nhập"), h("p", { class: "hint" }, registering
        ? "Cần mã mời trừ khi máy chủ đang mở đăng ký tự do."
        : "Dùng email và mật khẩu bạn đã đăng ký.")),
      h("label", {}, "Email", email),
      h("label", {}, "Mật khẩu", password),
      ...(registering
        ? [
            h("label", {}, "Tên hiển thị (tuỳ chọn)", displayName),
            h("label", {}, "Mã mời (tuỳ chọn)", invite),
            h("label", { class: "row" }, tos, h("span", {}, `Tôi đồng ý điều khoản sử dụng (bản ${TOS_VERSION})`)),
            h(
              "label",
              { class: "row" },
              crossBorder,
              h("span", {}, "Tôi đồng ý dữ liệu được xử lý bởi nhà cung cấp LLM ở nước ngoài (bắt buộc để dịch)"),
            ),
            h("label", { class: "row" }, shared, h("span", {}, "Tôi đồng ý dùng chung lõi văn phong đã ẩn danh (tuỳ chọn)")),
            h("label", { class: "row" }, age, h("span", {}, "Tôi xác nhận đủ 16 tuổi trở lên")),
          ]
        : []),
      h("div", { class: "row", style: "margin-top:14px" }, submit),
      h(
        "p",
        { class: "hint", style: "margin-top:12px" },
        registering ? "Đã có tài khoản? " : "Chưa có tài khoản? ",
        h(
          "button",
          {
            type: "button",
            class: "link",
            on: {
              click: () => {
                mode = registering ? "login" : "register";
                paint();
              },
            },
          },
          registering ? "Đăng nhập" : "Đăng ký",
        ),
      ),
    );
    password.autocomplete = registering ? "new-password" : "current-password";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    submit.disabled = true;
    try {
      if (mode === "login") {
        await post("/auth/login", { email: email.value.trim(), password: password.value });
      } else {
        if (!tos.checked || !crossBorder.checked || !age.checked) {
          throw new ApiError(422, {
            code: "consents_required",
            detail: "Cần đồng ý điều khoản, đồng ý chuyển dữ liệu và xác nhận đủ tuổi.",
          });
        }
        await post("/auth/register", {
          email: email.value.trim(),
          password: password.value,
          display_name: displayName.value.trim() || null,
          invite_code: invite.value.trim() || null,
          tos_version: TOS_VERSION,
          consent_cross_border: crossBorder.checked,
          consent_shared_processing: shared.checked,
          age_confirmed: age.checked,
        });
      }
      toast("Đăng nhập thành công.");
      (onSignedIn || (() => location.reload()))();
    } catch (error) {
      errorBox.textContent = error.message || "Không đăng nhập được.";
      errorBox.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });

  paint();
  mount(
    root,
    h("h1", {}, "ViSynth"),
    h(
      "p",
      {},
      "Dịch và tổng hợp tài liệu Việt–Anh, có trích dẫn kiểm chứng được, kèm cổng duyệt thuật ngữ trước khi dịch.",
    ),
    errorBox,
    form,
  );
  email.focus();
}
