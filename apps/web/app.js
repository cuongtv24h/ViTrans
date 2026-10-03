// Vỏ ứng dụng: phiên đăng nhập, thanh điều hướng, định tuyến theo hash.
//
// Vì sao hash routing: SPA không có bước build và máy chủ chỉ phục vụ tệp tĩnh — hash không cần
// cấu hình rewrite, không phá URL API, và sao chép liên kết là dùng được ngay.

import { get, post } from "./lib/api.js";
import { h, mount, toast } from "./lib/ui.js";

import * as login from "./views/login.js";
import * as home from "./views/home.js";
import * as wizard from "./views/wizard.js";
import * as job from "./views/job.js";
import * as report from "./views/report.js";
import * as glossaries from "./views/glossaries.js";
import * as account from "./views/account.js";
import * as admin from "./views/admin.js";
import * as legal from "./views/legal.js";

/** Trạng thái dùng chung: người dùng hiện tại (`null` = chưa đăng nhập). */
export const state = { user: null, signedIn: false };

export const ROUTES = [
  { path: /^\/$/, href: "#/", view: home, nav: "Tổng quan", auth: true },
  { path: /^\/tai-lieu$/, href: "#/tai-lieu", view: wizard, nav: "Dịch tài liệu", auth: true },
  { path: /^\/job\/(?<id>[0-9a-fA-F-]{36})$/, view: job, auth: true },
  { path: /^\/bao-cao\/(?<id>[0-9a-fA-F-]{36})$/, view: report, auth: true },
  { path: /^\/glossary$/, href: "#/glossary", view: glossaries, nav: "Thuật ngữ", auth: true },
  { path: /^\/tai-khoan$/, href: "#/tai-khoan", view: account, nav: "Tài khoản", auth: true },
  {
    path: /^\/admin\/(?<tab>pool|loi-van-phong|thuat-ngu|van-hanh)$/,
    href: "#/admin/pool",
    view: admin,
    nav: "Quản trị",
    auth: true,
    admin: true,
  },
  { path: /^\/phap-ly$/, href: "#/phap-ly", view: legal, nav: "Pháp lý" },
];

function currentRoute() {
  const path = location.hash.replace(/^#/, "") || "/";
  for (const route of ROUTES) {
    if (route.path.test(path)) return { route, path, params: route.path.exec(path)?.groups || {} };
  }
  return null;
}

export async function refreshUser() {
  try {
    const me = await get("/me");
    state.user = me;
    state.signedIn = true;
  } catch {
    state.user = null;
    state.signedIn = false;
  }
  paintNav();
  return state.user;
}

function paintNav() {
  const nav = document.getElementById("nav");
  const who = document.getElementById("who");
  const available = ROUTES.filter((r) => r.nav && (!r.admin || state.user?.role === "admin"));
  mount(
    nav,
    ...(state.signedIn ? available.map((r) => h("a", { href: r.href }, r.nav)) : []),
  );
  if (!state.signedIn) {
    mount(who, h("a", { href: "#/dang-nhap" }, "Đăng nhập"));
    return;
  }
  mount(
    who,
    h("span", { class: "muted" }, state.user?.display_name || state.user?.email || ""),
    h("span", { class: "badge", text: `${state.user?.credits ?? 0} tín dụng` }),
    h(
      "button",
      {
        class: "link",
        on: {
          click: async () => {
            await post("/auth/logout", {});
            state.user = null;
            state.signedIn = false;
            location.hash = "#/dang-nhap";
            location.reload();
          },
        },
      },
      "Thoát",
    ),
  );
}

async function route() {
  const view = document.getElementById("view");
  const matched = currentRoute();
  const path = location.hash.replace(/^#/, "") || "/";

  if (path === "/dang-nhap") {
    login.mount(view, { onSignedIn: afterSignIn });
    return;
  }
  if (!matched) {
    mount(
      view,
      h("h1", {}, "Không có trang này"),
      h("p", {}, "Đường dẫn không tồn tại. "),
      h("a", { href: "#/" }, "Về tổng quan"),
    );
    return;
  }
  if (matched.route.auth && !state.signedIn) {
    location.hash = "#/dang-nhap";
    return;
  }
  if (matched.route.admin && state.user?.role !== "admin") {
    mount(view, h("h1", {}, "Không đủ quyền"), h("p", {}, "Trang này chỉ dành cho quản trị viên."));
    return;
  }
  document.title = `ViSynth — ${matched.route.nav || "tài liệu"}`;
  try {
    await matched.route.view.mount(view, matched.params);
  } catch (error) {
    console.error(error);
    mount(
      view,
      h("h1", {}, "Không tải được trang"),
      h("p", {}, error?.message || "Lỗi không xác định"),
      h("button", { class: "primary", on: { click: () => route() } }, "Thử lại"),
    );
  }
}

function afterSignIn() {
  refreshUser().then(() => {
    if (!location.hash || location.hash === "#/dang-nhap") location.hash = "#/";
    else route();
  });
}

window.addEventListener("hashchange", route);
window.addEventListener("error", (event) => {
  console.error(event.error || event.message);
});
window.addEventListener("unhandledrejection", (event) => {
  const error = event.reason;
  if (error?.code) toast(error.message, { error: true });
});

(async function start() {
  await refreshUser();
  if (!state.signedIn && (location.hash === "" || location.hash === "#/")) location.hash = "#/dang-nhap";
  await route();
  document.documentElement.dataset.ready = "1";
})();
