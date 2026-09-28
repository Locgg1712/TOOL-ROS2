# multirobot_lint — Quét tĩnh cạm bẫy ROS2 đa robot

`multirobot_lint.py` là bộ quét tĩnh (AST + regex trên Python/YAML/shell), **không cần
môi trường ROS2 đã source, không cần rclpy, không cần hệ thống đang chạy**. Đây là điểm
khác biệt lớn nhất so với mọi tool khác trong bộ `ros2-mcp` — có thể chạy ngay cả khi
chưa cài ROS2, trong CI, hoặc trong `colcon test`.

Công cụ này mã hoá lại các cạm bẫy đã quan sát được khi hệ thống multi-robot (điều
khiển đội hình kiểu CVT/Voronoi, kiến trúc tập trung/phân tán, AMCL đa robot...)
chuyển từ Gazebo/Ignition sang phần cứng thật.

## 3 cách chạy

```bash
# 1. Script độc lập — không cần cài gì ngoài Python 3 chuẩn
python3 multirobot_lint.py ./src --format text
python3 multirobot_lint.py ./src --fix              # tự áp fix an toàn
python3 multirobot_lint.py ./src --checks 1,6,8      # chỉ chạy check cụ thể
python3 multirobot_lint.py --list-checks             # xem danh sách check

# 2. Qua CLI ros2mcp (không cần rclpy dù các subcommand khác của cli.py cần)
ros2mcp lint-multirobot ./src --fix
ros2mcp lint-list-checks

# 3. Qua MCP tool (Claude Desktop / Claude Code)
scan_multirobot_pitfalls(path="./src", fix=false)
```

Thoát với exit code `1` nếu còn finding severity `warning`/`error` chưa được fix —
dùng được thẳng làm CI gate (xem mục "Tích hợp CI" bên dưới).

## Danh sách 9 check

| ID | Mô tả | Severity | Tự sửa được? |
|----|-------|----------|---------------|
| 1 | Timeout/staleness so sánh bằng thời điểm *nhận* thay vì `header.stamp` | warning | ✓ (chèn TODO) |
| 2 | Cờ bất thường (function trả `False`) chỉ bị log, không được xử lý ở nơi gọi | warning | ✓ (chèn TODO) |
| 3 | Hằng số rate-limit/vận tốc không tham chiếu tới thông số phần cứng thật | info | ✓ (chèn TODO) |
| 4 | Ngưỡng an toàn nhỏ hơn 2× bán kính robot đã khai báo | warning | ✓ (chèn TODO) |
| 5 | Hàm ước lượng "vùng tự do"/corridor thiếu disclaimer "đây là ước lượng" | info | ✓ (chèn thẳng vào docstring) |
| 6 | `robot_radius`/margin bị set = 0 tại nơi gọi, ghi đè default an toàn của class | warning | ✓ (chèn TODO) |
| 7 | Code reset dùng API mô phỏng (Gazebo/Ignition) bị gọi từ luồng robot thật | warning | ✗ (cần người xác nhận) |
| 8 | Node publish `cmd_vel` không tự gửi vận tốc 0 khi shutdown/crash | warning | ✗ (cần người thêm handler) |
| 9 | `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION` không nhất quán giữa các file trong repo | error | ✗ (cần người thống nhất giá trị) |

Chỉ severity `error` (hiện tại chỉ có check 9) làm hard-fail `colcon test` mặc định —
xem lý do trong `test/test_multirobot_lint_self.py`.

## Nguyên tắc `--fix`

`--fix` **chỉ** áp dụng các sửa đổi an toàn về mặt cơ học: chèn comment TODO, chèn/mở
rộng docstring disclaimer, không bao giờ tự đổi giá trị số hay logic điều khiển. Với
các check cần biết số liệu vật lý thật (bán kính robot thật, giới hạn động cơ thật...),
tool **không tự bịa số** — nó cắm một TODO đúng vị trí và để người quyết định.

Sau khi một dòng đã có TODO (marker `LINT[multirobot:N]`), lần quét sau sẽ **không**
báo lại dòng đó nữa — giống quy ước `# noqa`. Đây là chủ đích: một khi người đã thấy
và quyết định tạm hoãn, tool không nên khiến CI fail vĩnh viễn vì một hằng số mà chính
repo không thể tự biết đáp số.

## Tích hợp CI

### Lớp 1 — script độc lập
Đã có sẵn, dùng ngay: `python3 multirobot_lint.py .`

### Lớp 2 — `colcon test`
Package này đã là một `ament_python` package (`package.xml` + `setup.py`).
`test/test_multirobot_lint_self.py` được `colcon test`/pytest tự động chạy, dùng
chính `multirobot_lint.run()` quét lại package. Chỉ finding severity `error` mới làm
test fail; `warning`/`info` được in ra log nhưng không chặn build (xem docstring của
file test để biết lý do thiết kế).

```bash
colcon build --packages-select ros2_mcp_tools
colcon test --packages-select ros2_mcp_tools
colcon test-result --verbose
```

### Lớp 3 — GitHub Actions
Xem `.github/workflows/multirobot-lint.yml` — chạy trên mọi PR/push vào `main`,
report-only (không tự `--fix` và commit ngược lại nhánh PR, để tránh thay đổi code
người dùng ngoài ý muốn trong CI).

## Giới hạn đã biết

- Đây là phân tích heuristic (AST + regex), không phải type-checker hay formal
  verification — có thể có false negative (bỏ sót) trên code viết theo phong cách khác
  lạ, và false positive hiếm gặp trên code trùng tên biến ngẫu nhiên.
- Check 1/2/6 dựa trên nhận diện pattern tên biến/hàm (`last_*_time`, hàm trả
  `True`/`False`...) — nếu codebase đặt tên khác quy ước, có thể cần điều chỉnh regex
  trong `multirobot_lint.py`.
- Check 7 chỉ nhận diện qua tên file chứa "real"/"reset" — không đọc hiểu ngữ nghĩa
  launch file đầy đủ.
