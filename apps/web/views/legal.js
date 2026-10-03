// Trang pháp lý: nói thẳng những gì hệ thống làm với dữ liệu, và người dùng làm gì được.
//
// Nội dung phải khớp §14 (PDPL, Luật AI 134/2025/QH15): thông báo AI trên mọi tệp xuất, dữ liệu gửi
// cho nhà cung cấp LLM ở nước ngoài, thời hạn lưu, quyền của người dùng, và kênh xử lý khiếu nại.

import { h, mount } from "../lib/ui.js";

export async function render(root) {
  mount(
    root,
    h("h1", {}, "Pháp lý & quyền riêng tư"),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Điều quan trọng nhất, nói trước"),
      h(
        "ul",
        {},
        h("li", {}, "Nội dung tài liệu của bạn được gửi tới nhà cung cấp mô hình ngôn ngữ ở nước ngoài để xử lý. Không có cách nào dịch mà không làm việc này — vì vậy hệ thống hỏi bạn đồng ý trước khi bật tài khoản."),
        h("li", {}, "Mọi tệp xuất đều kèm dòng \"Nội dung do AI tổng hợp, có thể chứa sai sót\". Bạn phải đối chiếu nguồn trước khi dùng cho quyết định quan trọng."),
        h("li", {}, "Hệ thống KHÔNG dùng nội dung của bạn để huấn luyện mô hình. Nhóm LLM dùng chế độ miễn phí có thể dùng dữ liệu để cải thiện dịch vụ — chế độ riêng tư loại bỏ hoàn toàn nhóm đó."),
        h("li", {}, "Bạn có quyền xem, tải, xoá tài liệu và yêu cầu xoá tài khoản."),
      ),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Dữ liệu được lưu bao lâu"),
      h(
        "ul",
        {},
        h("li", {}, "Nội dung gốc (tệp và đoạn trích): theo hạn lưu trong chính sách của máy chủ; hết hạn thì nội dung bị xoá, chỉ còn bản ghi + trích dẫn phục vụ kiểm toán."),
        h("li", {}, "Nhật ký gọi LLM: không chứa nội dung tài liệu — chỉ có số token, chi phí, mã lỗi."),
        h("li", {}, "Nhật ký kiểm toán: hành động quản trị và quyết định duyệt; giữ lại để truy vết."),
      ),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Nhà cung cấp mô hình"),
      h(
        "p",
        {},
        "Hệ thống dùng nhiều nhà cung cấp để dự phòng: Gemini là mô hình chính cho tiếng Việt, NVIDIA là khoá dự phòng. Mỗi nhóm hạn mức có một chính sách dữ liệu ",
        h("code", {}, "data_policy"),
        " ghi rõ trong trang quản trị; job ở chế độ riêng tư chỉ chạy trên nhóm ",
        h("code", {}, "no_training"),
        ".",
      ),
    ),
    h(
      "section",
      { class: "panel" },
      h("h2", { style: "margin-top:0" }, "Quyền của bạn và cách thực hiện"),
      h(
        "ul",
        {},
        h("li", {}, "Xem dữ liệu: trang Tài khoản hiển thị số dư, lịch sử tín dụng và các đồng ý đã ghi."),
        h("li", {}, "Xoá tài liệu: ở Tổng quan, mỗi tài liệu có nút xoá; xoá mềm rồi xoá nội dung gốc."),
        h("li", {}, "Xoá tài khoản / tải toàn bộ dữ liệu: gửi yêu cầu qua ", h("a", { href: "#/phap-ly" }, "kênh khiếu nại"), " — hệ thống ghi lại và xử lý theo PDPL."),
        h("li", {}, "Tác phẩm có quyền bị xâm phạm: dùng biểu mẫu takedown (`POST /api/v1/takedown`) hoặc liên hệ quản trị viên."),
      ),
      h("p", { class: "hint" }, "Kênh khiếu nại cụ thể do người vận hành máy chủ công bố công khai (điền trong hồ sơ PDPL trước khi mở đăng ký công khai)."),
    ),
  );
}
