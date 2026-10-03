// Khu quản trị (§17.14, §19.6): bốn tab — pool, lõi văn phong, hàng đợi thuật ngữ, vận hành.
//
// Đây là giao diện DUY NHẤT để nhập khoá LLM (§17.14: một cách nhập, không dán khoá vào tệp cấu hình
// trên máy). Nguyên tắc bảo mật của trang này:
//   * khoá gửi một lần qua HTTPS, đi thẳng vào kho khoá đã mã hoá — giao diện KHÔNG đọc lại khoá;
//   * `GET /admin/pool/config` chỉ trả tham chiếu `enc:<id>` + 4 ký tự cuối, và giao diện chỉ hiển thị
//     đúng phần đó;
//   * mọi thao tác ghi đều hỏi xác nhận, và `PUT /admin/pool/config` chạy `dry_run` trước khi áp dụng.

import { get, patch, post, put } from "../lib/api.js";
import { h, nf, mount, statusBadge, toast, usd, when } from "../lib/ui.js";

const TABS = [
  ["pool", "Pool LLM"],
  ["loi-van-phong", "Lõi văn phong"],
  ["thuat-ngu", "Hàng đợi thuật ngữ"],
  ["van-hanh", "Vận hành"],
];

export async function render(root, { tab = "pool" } = {}) {
  const view = h("div", {});
  const body = h("div", {});
  mount(
    view,
    h("h1", {}, "Quản trị"),
    h(
      "nav",
      { class: "steps", "aria-label": "Khu quản trị" },
      ...TABS.map(([key, label]) =>
        h(
          "a",
          {
            href: `#/admin/${key}`,
            "aria-current": key === tab ? "page" : null,
            style: "text-decoration:none",
          },
          h("li", { style: "list-style:none" }, label),
        ),
      ),
    ),
    body,
  );
  root.replaceChildren(view);

  const painters = { pool: paintPool, "loi-van-phong": paintStyle, "thuat-ngu": paintQueue, "van-hanh": paintOps };
  await (painters[tab] || paintPool)(body);
}

// ------------------------------------------------------------------ pool

async function paintPool(box) {
  const [status, capacity, incidents, config] = await Promise.all([
    get("/admin/pool/status"),
    get("/admin/pool/capacity?privacy_class=standard"),
    get("/admin/pool/incidents?limit=10"),
    get("/admin/pool/config"),
  ]);

  const groups = config.groups || [];
  const deployments = status || [];

  const rows = deployments.map((item) => {
    const probe = h("button", { class: "link" }, "Thử");
    probe.addEventListener("click", async () => {
      probe.disabled = true;
      try {
        const result = await post(`/admin/pool/deployments/${encodeURIComponent(item.id)}/probe`, {});
        toast(result.passed ? "Kiểm định ĐẠT." : `Kiểm định chưa đạt: ${result.note || "xem chi tiết"}`);
        await paintPool(box);
      } catch (error) {
        toast(error.message || "Không thử được.", { error: true });
      } finally {
        probe.disabled = false;
      }
    });
    const toggle = h("button", { class: "link" }, item.enabled ? "Tắt" : "Bật");
    toggle.addEventListener("click", async () => {
      try {
        await patch(`/admin/pool/deployments/${encodeURIComponent(item.id)}`, { enabled: !item.enabled });
        toast("Đã cập nhật deployment.");
        await paintPool(box);
      } catch (error) {
        toast(error.message || "Không đổi được trạng thái.", { error: true });
      }
    });
    return h(
      "tr",
      {},
      h("td", {}, h("strong", {}, item.model), h("br"), h("small", { class: "muted" }, item.id)),
      h("td", {}, h("span", { class: `badge ${item.data_policy === "no_training" ? "ok" : "warn"}` }, item.data_policy || "?")),
      h("td", {}, `${item.rpd_used ?? 0}/${item.rpd_limit ?? "—"}`),
      h("td", {}, item.headroom === null || item.headroom === undefined ? "—" : `${Math.round(Number(item.headroom) * 100)}%`),
      h("td", {}, `${item.active_credentials ?? 0} khoá`),
      h("td", {}, h("span", { class: `badge ${item.circuit === "open" ? "danger" : "ok"}` }, item.circuit || "closed")),
      h("td", {}, h("div", { class: "row" }, toggle, probe)),
    );
  });

  // Nhập khoá: chọn NHÓM (nhóm đã gắn nhà cung cấp + hạn mức), đặt nhãn, dán khoá.
  // Khoá chỉ nằm trong biến `secret` của lần bấm này; sau khi lưu, giao diện chỉ còn thấy `••••last4`.
  const groupSelect = h("select", {}, ...groups.map((group) => h("option", { value: group.id }, `${group.id} · ${group.provider} · ${group.tier}`)));
  const label = h("input", { placeholder: "nhãn để phân biệt (ví dụ: gemini-2)" });
  const secret = h("input", { type: "password", autocomplete: "off", placeholder: "dán khoá API (không hiện lại sau khi lưu)" });
  const addKey = h("button", { class: "primary" }, "Lưu khoá (mã hoá)");
  addKey.addEventListener("click", async () => {
    if (!groupSelect.value || !label.value.trim() || !secret.value.trim()) {
      toast("Cần chọn nhóm, đặt nhãn và dán khoá.", { error: true });
      return;
    }
    if (!confirm("Gửi khoá này lên máy chủ? Khoá được mã hoá bằng khoá chủ và KHÔNG hiển thị lại.")) return;
    addKey.disabled = true;
    try {
      await post(`/admin/pool/groups/${encodeURIComponent(groupSelect.value)}/credentials`, {
        label: label.value.trim(),
        secret: secret.value.trim(),
      });
      secret.value = "";
      label.value = "";
      toast("Đã lưu khoá (đã mã hoá trong kho khoá).");
      await paintPool(box);
    } catch (error) {
      toast(error.message || "Không lưu được khoá.", { error: true });
    } finally {
      addKey.disabled = false;
    }
  });

  const credentialItems = groups.flatMap((group) =>
    (group.credentials || []).map((cred) =>
      h(
        "li",
        {},
        h(
          "div",
          { class: "row between" },
          h("strong", {}, `${cred.label || group.id} · ${group.provider}`),
          h("span", { class: `badge ${cred.status === "active" ? "ok" : "warn"}` }, cred.status || "active"),
        ),
        h(
          "small",
          { class: "muted" },
          `••••${cred.last4 || "????"} · tham chiếu ${cred.secret_ref || cred.id} · dùng lần cuối ${when(cred.last_used_at)}`,
        ),
        cred.quarantined_reason ? h("div", { class: "hint" }, `Lý do cách ly: ${cred.quarantined_reason}`) : null,
        h(
          "div",
          { class: "row", style: "margin-top:6px" },
          h(
            "button",
            {
              class: "link",
              on: {
                click: async () => {
                  try {
                    await patch(`/admin/pool/credentials/${cred.id}`, { status: cred.status === "active" ? "disabled" : "active" });
                    toast("Đã đổi trạng thái khoá.");
                    await paintPool(box);
                  } catch (error) {
                    toast(error.message || "Không đổi được.", { error: true });
                  }
                },
              },
            },
            cred.status === "active" ? "Tạm dừng" : "Bật lại",
          ),
        ),
      ),
    ),
  );

  const declarations = h("textarea", { placeholder: "Dán YAML/JSON khai báo pool (xem mẫu bên dưới)" });
  const dryRun = h("button", {}, "Xem trước (dry-run)");
  const apply = h("button", { class: "primary" }, "Áp dụng");
  const runDeclaration = async (isDryRun) => {
    if (!declarations.value.trim()) return;
    const button = isDryRun ? dryRun : apply;
    button.disabled = true;
    try {
      const response = await fetch(`/api/v1/admin/pool/declare?dry_run=${isDryRun ? "true" : "false"}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/yaml" },
        body: declarations.value,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "khai báo không hợp lệ");
      toast(isDryRun ? `Xem trước: ${(payload.changes || payload.applied || []).length} thay đổi.` : "Đã áp dụng khai báo.");
      if (!isDryRun) await paintPool(box);
    } catch (error) {
      toast(error.message || "Khai báo lỗi.", { error: true });
    } finally {
      button.disabled = false;
    }
  };
  dryRun.addEventListener("click", () => runDeclaration(true));
  apply.addEventListener("click", () => {
    if (confirm("Áp dụng khai báo này vào pool đang chạy?")) runDeclaration(false);
  });

  mount(
    box,
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Deployment đang có"),
      deployments.length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h(
                "thead",
                {},
                h(
                  "tr",
                  {},
                  h("th", {}, "Model"),
                  h("th", {}, "Chính sách dữ liệu"),
                  h("th", {}, "Hạn mức/ngày"),
                  h("th", {}, "Dư địa"),
                  h("th", {}, "Khoá"),
                  h("th", {}, "Cầu dao"),
                  h("th", {}),
                ),
              ),
              h("tbody", {}, ...rows),
            ),
          )
        : h("p", { class: "hint" }, "Pool chưa có deployment nào. Dùng khối khai báo bên dưới để thêm."),
      h(
        "p",
        { class: "hint" },
        `Dung lượng (tiêu chuẩn): ~${nf.format(capacity.docs_per_day ?? 0)} tài liệu/ngày · `,
        `chi tiết: ${JSON.stringify(capacity.limits || {}).slice(0, 160)}`,
      ),
    ),

    h(
      "div",
      { class: "grid" },
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, "Thêm khoá API"),
        h("p", { class: "hint" }, "Khoá đi thẳng vào kho khoá đã mã hoá bằng khoá chủ; giao diện không bao giờ đọc lại khoá."),
        h("label", {}, "Nhóm", groupSelect),
        h("label", {}, "Nhãn", label),
        h("label", {}, "Khoá API", secret),
        h("div", { class: "row", style: "margin-top:10px" }, addKey),
      ),
      h(
        "section",
        { class: "panel" },
        h("h2", { style: "margin-top:0" }, `Khoá hiện có (${credentialItems.length})`),
        credentialItems.length
          ? h("ul", { class: "clean" }, ...credentialItems)
          : h("p", { class: "hint" }, "Chưa có khoá nào trong CSDL."),
      ),
    ),

    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Khai báo nhanh (YAML/JSON)"),
      h(
        "p",
        { class: "hint" },
        "Mẫu có chú thích: ",
        h("a", { href: "/api/v1/admin/pool/declaration-template", target: "_blank", rel: "noreferrer" }, "mở mẫu"),
        ". Khai báo luôn chạy xem trước trước khi áp dụng.",
      ),
      declarations,
      h("div", { class: "row", style: "margin-top:10px" }, dryRun, apply),
    ),

    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Sự cố gần đây"),
      (incidents || []).length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Lúc"), h("th", {}, "Deployment"), h("th", {}, "Loại"), h("th", {}, "Chi tiết"))),
              h(
                "tbody",
                {},
                ...incidents.map((item) =>
                  h(
                    "tr",
                    {},
                    h("td", {}, when(item.created_at)),
                    h("td", {}, item.deployment_id || "—"),
                    h("td", {}, item.kind || item.type || "—"),
                    h("td", {}, String(item.detail || item.message || "").slice(0, 160)),
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "hint" }, "Không có sự cố nào được ghi."),
    ),
  );
}

// ------------------------------------------------------------------ lõi văn phong

async function paintStyle(box) {
  const cores = await get("/admin/style-cores");
  const items = cores.items || cores || [];

  const cards = await Promise.all(
    items.map(async (core) => {
      const versions = await get(`/admin/style-cores/${core.id}/versions`).catch(() => []);
      const list = versions.items || versions || [];
      const latest = list[0];
      const decide = h("input", { placeholder: "Trả lời câu hỏi của P12 (nếu có)" });
      const actions = [];
      if (latest?.status === "draft") {
        actions.push(
          h(
            "button",
            {
              class: "link",
              on: {
                click: async () => {
                  try {
                    await post(`/admin/style-cores/${core.id}/versions/${latest.id}/submit`, {});
                    toast("Đã gửi bản nháp để P12 chất vấn.");
                    await paintStyle(box);
                  } catch (error) {
                    toast(error.message || "Không gửi được.", { error: true });
                  }
                },
              },
            },
            "Gửi để chất vấn",
          ),
        );
      }
      if (latest?.status === "interrogating") {
        actions.push(
          h(
            "button",
            {
              class: "link",
              on: {
                click: async () => {
                  try {
                    await post(`/admin/style-cores/${core.id}/versions/${latest.id}/answer-decision`, {
                      answer_vi: decide.value.trim() || "đồng ý",
                      accept: true,
                    });
                    toast("Đã trả lời quyết định.");
                    await paintStyle(box);
                  } catch (error) {
                    toast(error.message || "Không trả lời được.", { error: true });
                  }
                },
              },
            },
            "Trả lời & chấp nhận",
          ),
        );
      }
      if (latest && ["draft", "interrogating", "decided"].includes(latest.status)) {
        actions.push(
          h(
            "button",
            {
              class: "link",
              on: {
                click: async () => {
                  if (!confirm("Duyệt phiên bản này? Bản đã duyệt là BẤT BIẾN (không sửa được nữa).")) return;
                  try {
                    await post(`/admin/style-cores/${core.id}/versions/${latest.id}/approve`, {});
                    toast("Đã duyệt phiên bản.");
                    await paintStyle(box);
                  } catch (error) {
                    toast(error.message || "Không duyệt được.", { error: true });
                  }
                },
              },
            },
            "Duyệt",
          ),
          h(
            "button",
            {
              class: "link danger",
              on: {
                click: async () => {
                  try {
                    await post(`/admin/style-cores/${core.id}/versions/${latest.id}/reject`, { reason_vi: "chưa đạt" });
                    toast("Đã từ chối phiên bản.");
                    await paintStyle(box);
                  } catch (error) {
                    toast(error.message || "Không từ chối được.", { error: true });
                  }
                },
              },
            },
            "Từ chối",
          ),
        );
      }
      return h(
        "section",
        { class: "panel" },
        h("div", { class: "row between" }, h("h3", { style: "margin:0" }, core.name), h("span", { class: "badge" }, core.scope || "system")),
        h("small", { class: "muted" }, `${core.domain || "chung"} · ${core.locale || "vi"}`),
        h(
          "p",
          { class: "hint" },
          latest
            ? `Phiên bản mới nhất: v${latest.version} — ${latest.status}. ${latest.summary_vi || latest.diff_summary_vi || ""}`
            : "Chưa có phiên bản nào.",
        ),
        h("label", {}, "Trả lời P12", decide),
        h("div", { class: "row", style: "margin-top:8px" }, ...actions),
      );
    }),
  );

  const testInput = h("textarea", { placeholder: "Dán một đoạn văn để thử lõi văn phong (test-drive)" });
  const testCore = h(
    "select",
    {},
    ...items.map((core) => h("option", { value: core.id }, core.name)),
  );
  const testRun = h("button", { class: "primary" }, "Chạy thử");
  const testOut = h("div", {});
  testRun.addEventListener("click", async () => {
    const versions = await get(`/admin/style-cores/${testCore.value}/versions`).catch(() => []);
    const latest = (versions.items || versions || [])[0];
    if (!latest) {
      toast("Lõi này chưa có phiên bản để thử.", { error: true });
      return;
    }
    testRun.disabled = true;
    try {
      const result = await post(`/admin/style-cores/${testCore.value}/versions/${latest.id}/test-drive`, {
        text: testInput.value.trim(),
      });
      mount(testOut, h("pre", {}, JSON.stringify(result, null, 2)));
    } catch (error) {
      toast(error.message || "Không chạy thử được.", { error: true });
    } finally {
      testRun.disabled = false;
    }
  });

  mount(
    box,
    h("p", { class: "hint" }, "Lõi văn phong theo lĩnh vực: tạo phiên bản nháp → P12 chất vấn → người duyệt → chỉ bản ĐÃ DUYỆT mới dùng được trong job."),
    ...cards,
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Chạy thử lõi văn phong"),
      h("label", {}, "Lõi", testCore),
      h("label", {}, "Đoạn văn", testInput),
      h("div", { class: "row", style: "margin-top:10px" }, testRun),
      testOut,
    ),
  );
}

// ------------------------------------------------------------------ hàng đợi thuật ngữ

async function paintQueue(box) {
  const data = await get("/admin/glossary-review?limit=30");
  const items = data.items || [];

  const rows = items.map((entry) => {
    const target = h("input", { type: "text", value: entry.target_term || "" });
    const note = h("input", { type: "text", maxlength: "300", placeholder: "ghi chú duyệt" });
    const decide = (action) =>
      h(
        "button",
        {
          class: action === "reject" ? "link danger" : "link",
          on: {
            click: async () => {
              try {
                await post(`/admin/glossary-review/${entry.id}/decision`, {
                  action,
                  target_term: target.value.trim() || null,
                  note: note.value.trim() || null,
                });
                toast(action === "reject" ? "Đã loại mục." : "Đã duyệt mục.");
                await paintQueue(box);
              } catch (error) {
                toast(error.message || "Không ghi được quyết định.", { error: true });
              }
            },
          },
        },
        action === "reject" ? "Loại" : "Duyệt",
      );
    return h(
      "tr",
      {},
      h(
        "td",
        {},
        h("strong", {}, entry.source_term),
        entry.needs_human ? h("span", { class: "badge warn", style: "margin-left:6px" }, "cần người") : null,
        h("br"),
        h("small", { class: "muted" }, `${entry.glossary_name || ""} · tin cậy ${Math.round((entry.confidence ?? 0) * 100)}%`),
        entry.question_vi ? h("div", { class: "hint" }, `P12 hỏi: ${entry.question_vi}`) : null,
      ),
      h("td", {}, target),
      h("td", {}, note),
      h("td", {}, h("div", { class: "row" }, decide("confirm"), decide("reject"))),
    );
  });

  mount(
    box,
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, `Hàng đợi duyệt thuật ngữ (${items.length})`),
      items.length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Thuật ngữ"), h("th", {}, "Cách dịch"), h("th", {}, "Ghi chú"), h("th", {}))),
              h("tbody", {}, ...rows),
            ),
          )
        : h("p", { class: "hint" }, "Hàng đợi trống — không có mục nào chờ quyết định."),
      data.next_cursor ? h("small", { class: "muted" }, `Còn nữa — lần sau dùng con trỏ: ${data.next_cursor}`) : null,
    ),
  );
}

// ------------------------------------------------------------------ vận hành

async function paintOps(box) {
  const [users, usage, caps, audit] = await Promise.all([
    get("/admin/users?limit=20"),
    get("/admin/usage?days=14").catch(() => null),
    get("/admin/spend-cap"),
    get("/admin/audit?limit=20").catch(() => ({ items: [] })),
  ]);

  const userRows = (users.items || []).map((user) => {
    const select = h(
      "select",
      {},
      ...["user", "curator", "admin"].map((role) => h("option", { value: role, selected: user.role === role }, role)),
    );
    const status = h(
      "select",
      {},
      ...["active", "suspended"].map((value) => h("option", { value, selected: user.status === value }, value)),
    );
    const save = h("button", { class: "link" }, "Lưu");
    save.addEventListener("click", async () => {
      try {
        await patch(`/admin/users/${user.id}`, { role: select.value, status: status.value });
        toast("Đã cập nhật người dùng.");
      } catch (error) {
        toast(error.message || "Không cập nhật được.", { error: true });
      }
    });
    return h(
      "tr",
      {},
      h("td", {}, user.email, h("br"), h("small", { class: "muted" }, `tạo ${when(user.created_at)}`)),
      h("td", {}, select),
      h("td", {}, status),
      h("td", {}, String(user.credits ?? 0)),
      h("td", {}, save),
    );
  });

  const capInput = h("input", { type: "number", step: "0.5", min: "0", value: String(caps.max_cost_usd ?? caps.daily_cost_usd ?? "") });
  const capSave = h("button", { class: "primary" }, "Lưu trần");
  capSave.addEventListener("click", async () => {
    capSave.disabled = true;
    try {
      await put("/admin/spend-cap", { max_cost_usd: Number(capInput.value) });
      toast("Đã lưu trần chi tiêu.");
    } catch (error) {
      toast(error.message || "Không lưu được trần.", { error: true });
    } finally {
      capSave.disabled = false;
    }
  });

  const inviteCredits = h("input", { type: "number", min: "1", value: "50" });
  const inviteCount = h("input", { type: "number", min: "1", max: "200", value: "3" });
  const inviteMax = h("input", { type: "number", min: "1", value: "1" });
  const inviteBox = h("div", { class: "row" });
  const inviteCreate = h("button", { class: "primary" }, "Tạo mã mời");
  inviteCreate.addEventListener("click", async () => {
    inviteCreate.disabled = true;
    try {
      const created = await post("/admin/invites", {
        count: Number(inviteCount.value),
        credits_grant: Number(inviteCredits.value),
        max_uses: Number(inviteMax.value),
      });
      const codes = (created.codes || created.items || []).map((item) => item.code || item).join(", ");
      mount(inviteBox, h("code", {}, codes || "(không có mã)"));
      toast("Đã tạo mã mời.");
    } catch (error) {
      toast(error.message || "Không tạo được mã mời.", { error: true });
    } finally {
      inviteCreate.disabled = false;
    }
  });

  mount(
    box,
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Trần chi tiêu"),
      h("div", { class: "row" }, h("label", {}, "USD/ngày", capInput), capSave),
      h("small", { class: "muted" }, `Đang dùng: ${usd(caps.spent_today_usd ?? 0)} hôm nay.`),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Mã mời"),
      h("div", { class: "grid" }, h("div", {}, h("label", {}, "Tín dụng/mã", inviteCredits)), h("div", {}, h("label", {}, "Số mã", inviteCount)), h("div", {}, h("label", {}, "Lượt dùng/mã", inviteMax))),
      h("div", { class: "row", style: "margin-top:10px" }, inviteCreate),
      inviteBox,
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Người dùng"),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Email"), h("th", {}, "Vai trò"), h("th", {}, "Trạng thái"), h("th", {}, "Tín dụng"), h("th", {}))), h("tbody", {}, ...userRows))),
    ),
    usage
      ? h(
          "section",
          { class: "panel" },
          h("h2", { style: "margin-top:0" }, "Sử dụng 14 ngày"),
          h("div", { class: "grid" },
            h("div", {}, h("div", { class: "muted" }, "Job"), h("strong", {}, String(usage.jobs ?? usage.job_count ?? "—"))),
            h("div", {}, h("div", { class: "muted" }, "Tín dụng"), h("strong", {}, String(usage.credits ?? "—"))),
            h("div", {}, h("div", { class: "muted" }, "Chi phí thật"), h("strong", {}, usd(usage.cost_usd ?? 0))),
          ),
        )
      : null,
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Nhật ký kiểm toán gần đây"),
      (audit.items || []).length
        ? h(
            "ul",
            { class: "clean" },
            ...(audit.items || []).map((row) =>
              h("li", {}, h("small", { class: "muted" }, `${when(row.created_at)} · ${row.action} · ${row.target_type || ""}`), h("br"), String(row.actor_email || row.actor_id || "")),
            ),
          )
        : h("p", { class: "hint" }, "Chưa có bản ghi kiểm toán."),
    ),
  );
}
