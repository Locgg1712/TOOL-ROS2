# CHANGES — v0.2.0 "chạy ổn với mọi project ROS2"

Zip này là **overlay**: giải nén đè lên repo TOOL-ROS2 (README, CLAUDE.md, MANIFEST_SCHEMA.md,
MULTIROBOT_GUIDE.md, SIM_VS_REAL.md, install.sh, cli.py, ros2_cli.py của bạn được giữ nguyên).

## Áp dụng
```bash
unzip -o TOOL-ROS2-fixes.zip -d .. # hoặc giải nén đè vào thư mục repo
python3 tools/apply_cli_patches.py --dry-run   # xem trước
python3 tools/apply_cli_patches.py             # vá cli.py + ros2_cli.py (có .bak, atomic, idempotent)
python3 -m pytest test -q                      # 42 test
python3 multirobot_lint.py .                   # tự quét: 0 finding
```
`apply_cli_patches.py` chỉ ghi file khi MỌI anchor khớp đúng 1 lần; nếu file của bạn lệch nó báo và
không đụng vào. Nó đã được test trên đoạn trích giống code gốc (CRLF, chạy lại, lệch anchor), chưa
chạy trên file thật của bạn — đó là lý do có `--dry-run` và `.bak`.

## Danh sách file
| File | Trạng thái |
|---|---|
| `multirobot_lint.py` + bản copy `.agents/skills/ros2-mcp/scripts/multirobot_lint.py` | viết lại |
| `server.py` | sửa |
| `ros2_infra.py` + copy trong `scripts/` | MỚI — lọc topic/service hạ tầng, `auto_qos()` dùng chung |
| `tools/apply_cli_patches.py` | MỚI — vá cli.py/ros2_cli.py |
| `setup.py`, `requirements.txt`, `package.xml` | sửa (v0.2.0, `mcp>=1.0.0,<3`, cài kèm manifests) |
| `MULTIROBOT_LINT.md` | viết lại theo hành vi mới |
| `.github/workflows/multirobot-lint.yml` | sửa: chạy pytest + annotation + báo cáo JSON |
| `test/test_multirobot_lint_generic.py` (31 test), `test/test_server_fake_ros.py` (7), `test/test_ros2_infra.py` (2); cộng 2 test self-scan = 42 | MỚI |
| `test/test_multirobot_lint_self.py` | sửa |
| `ros2_manifests/.keep`, `CHANGES.md` | MỚI |

## Đã kiểm chứng (chạy thật, không chỉ đọc code)
- **42 test pass**; pyflakes sạch; tự quét repo = 0 finding, 0 lỗi nội bộ.
- **Project ROS2 thật** (ros2/demos, turtlebot3, navigation2, teleop_twist_keyboard; 1.500+ file):
  0 lỗi nội bộ, quét nav2 (1.252 file) ~1 giây. Finding thật: check 1 ở `topic_monitor.py` (chính code
  có TODO "dùng timestamp của msg"), check 8 ở 5 node example của turtlebot3 (không dừng robot khi
  shutdown). Nhiễu ban đầu (file trong `test/`, header `.hpp`) đã được loại: nav2 từ 8 → 2 finding.
- **`--fix` trên bản copy** của các repo thật + stdlib Python (573 file, `--include-tests`):
  0 file bị hỏng (compile lại toàn bộ), 0 dòng bị xoá (chỉ thêm dòng), BOM/CRLF không đổi, chạy lần 2
  sửa 0 chỗ (idempotent).
- **`server.py` không có rclpy:** khởi động được, 13 tool đăng ký đúng schema (mcp 2.x), các tool
  tĩnh chạy, tool live trả lỗi JSON rõ ràng. Logic live-graph (auto-QoS, `validate_node`, tên node có
  namespace, cắt mảng, Twist cap) được test với rclpy **giả**.

## Chưa kiểm chứng (cần bạn thử)
- Tool live-graph trên **ROS2 thật** (chưa có rclpy trong môi trường test): thử `validate_node talker`,
  `echo_topic` trên topic sensor Best-Effort, `get_topic_info` với nhiều robot.
- Python 3.10 (Humble) — mới chạy trên 3.12; code tránh cú pháp riêng của 3.12.
- `apply_cli_patches.py` trên file `cli.py`/`ros2_cli.py` thật của bạn.

## Lỗi đã sửa (vòng 1)
Discovery bỏ qua cả project nằm dưới `build/`; check 8 coi `linear.x = 0.5` là lệnh dừng; `--fix` chèn
trùng comment / đổi CRLF→LF / bỏ BOM / không kiểm tra compile; file BOM bị bỏ qua ở check 2/5/6;
check 9 luôn `error` dù per-robot domain là hợp lệ; `server.py` import rclpy ở đầu file nên
`scan_multirobot_pitfalls` không chạy khi chưa source ROS2; `validate_node` không bao giờ "OK" vì
`/rosout`, `/parameter_events`, parameter services; mcp 2.x làm hỏng `from mcp.server.fastmcp import
FastMCP`; check 3 không khớp comment cuối dòng; check 4 ghép mọi `*dist*` với mọi `*radius*`; check 7
khớp `realsense`.

## Cải tiến thêm (vòng 2, sau khi chạy trên code thật)
- Mọi check bỏ qua **code test** khi quét thư mục (`--include-tests` để bật lại) — nguồn nhiễu lớn nhất.
- Check 1/3/4/9 bỏ qua **ví dụ trong docstring/chuỗi nhiều dòng**; check 9 bỏ qua dòng comment, đọc thêm
  `os.environ[...] = ...`, `SetEnvironmentVariable(...)`, `setenv/putenv`, không tính `os.environ.get`.
- Check 8: nhận `create_publisher(msg_type=Twist, ...)`, không nhầm khi 2 class cùng tên hàm `stop`,
  bỏ header C++.
- CLI lint: `--fail-on {info,warning,error,never}`, `--format github`, `--include-tests`, `--exclude`.
- Hiệu năng: cache đọc/parse/AST theo file (nav2 ~1s; stdlib 22MB ~7s); bỏ file > 2MB.
- `--fix` bỏ qua file dùng CR đơn lẻ (số dòng sẽ lệch với `ast`).
- `server.py`: `ROS2_MCP_MAX_LINEAR` / `ROS2_MCP_MAX_ANGULAR` (tuỳ chọn) từ chối `publish_message` Twist
  vượt ngưỡng; chưa đặt biến = không giới hạn (hành vi cũ).
- `ros2_infra.auto_qos()` dùng chung cho `cli.py`/`ros2_cli.py` (qua script vá).

## Giới hạn còn lại
Heuristic (AST + regex): check 1/3/4 dựa trên quy ước đặt tên; check 8 không theo dõi luồng gọi xuyên
file (dùng `# LINT-IGNORE[multirobot:8] lý do`); C++ chỉ có check 8 (info) và 9; ROS1 không hỗ trợ.
Cần cập nhật tay vài câu trong README/CLAUDE.md nói "check 9 luôn là error" và "check 7/8 luôn báo lại":
giờ chúng có thể tắt bằng `LINT-IGNORE`/`LINT-DISABLE`.
