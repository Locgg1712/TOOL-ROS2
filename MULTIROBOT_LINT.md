# multirobot_lint — Quét tĩnh cạm bẫy ROS2 đa robot

`multirobot_lint.py` là bộ quét tĩnh (AST + regex trên Python/C++/YAML/shell), **không cần
ROS2 đã source, không cần rclpy, không cần hệ thống đang chạy**. Chạy được trong CI, pre-commit
hoặc `colcon test`. Chỉ cần Python 3 (thư viện chuẩn).

Công cụ mã hoá các cạm bẫy thường gặp khi hệ thống multi-robot chuyển từ mô phỏng sang phần
cứng thật. Nó là **heuristic**, không phải chứng minh hình thức: hãy đọc finding như một danh
sách "nên xem lại chỗ này", không phải phán quyết.

## Cách chạy

```bash
python3 multirobot_lint.py ./src                          # quét, in text
python3 multirobot_lint.py ./src --fix                    # tự áp fix an toàn (xem bên dưới)
python3 multirobot_lint.py ./src --checks 1,6,8           # chỉ vài check
python3 multirobot_lint.py ./src --exclude 'third_party/*' --exclude 'gen_*.py'
python3 multirobot_lint.py ./src --include-tests          # quét cả code test
python3 multirobot_lint.py ./src --fail-on error          # chỉ fail khi có error
python3 multirobot_lint.py ./src --format github          # annotation cho GitHub Actions
python3 multirobot_lint.py --list-checks

ros2mcp lint-multirobot ./src --fix                       # qua CLI (cần patch cli.py, xem CHANGES.md)
scan_multirobot_pitfalls(path="./src", fix=false)         # qua MCP tool
```

Exit code: `0` sạch · `1` còn finding có severity ≥ `--fail-on` (mặc định `warning`) chưa fix ·
`2` một check bị lỗi nội bộ (xem `report["errors"]`).

Mặc định, khi quét **thư mục**, code test (`test/`, `tests/`, `test_*.py`, `*_test.cpp`…) bị bỏ
qua vì test hay publish `cmd_vel` không dừng, đặt domain id lạ… Quét **một file cụ thể** thì luôn
quét. Thư mục `build/ install/ log/ venv/ .git/ node_modules/`… chỉ bị bỏ khi nằm *bên dưới* thư
mục gốc bạn quét (project nằm dưới `~/build/…` vẫn quét bình thường). File > 2 MB bị bỏ qua.

## Danh sách 9 check

| ID | Mô tả | Severity | Tự sửa được? |
|----|-------|----------|---------------|
| 1 | Timeout/staleness tính từ thời điểm *nhận* thay vì `header.stamp` (file có dùng `header.stamp` → chỉ `info`) | warning / info | ✓ (chèn TODO) |
| 2 | Hàm trả `True`/`False` kiểu health-check (tên có `check/valid/verify/detect/safe/…`) bị **bỏ kết quả** ở nơi gọi | warning | ✓ (chèn TODO) |
| 3 | Hằng số giới hạn vận tốc/gia tốc (`MAX_*_VEL`, `LIMIT_ACCEL`, `V_MAX`…) không tham chiếu tới phần cứng | info | ✓ (chèn TODO) |
| 4 | Ngưỡng an toàn nhỏ hơn 2× bán kính robot lớn nhất trong file (bỏ qua tên sensor/lidar/factor…) | warning | ✓ (chèn TODO) |
| 5 | Hàm ước lượng vùng tự do/clearance/corridor thiếu disclaimer "đây là ước lượng" | info | ✓ (chèn vào docstring) |
| 6 | `*radius*`/`*margin*` bị set = 0 tại nơi gọi, ghi đè default của class (kể cả kw-only) | warning | ✓ (chèn TODO) |
| 7 | Script "real" (token `real` riêng, không phải `realsense`) tham chiếu code reset mô phỏng Gazebo | warning | ✗ |
| 8 | Node publish `cmd_vel`/`Twist` (Python hoặc C++) không publish vận tốc 0 ở đường shutdown | warning (C++: info) | ✗ |
| 9 | `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION` không nhất quán | **error** nếu cùng 1 file · warning nếu giữa các file | ✗ |

**Check 8** nhận diện đường shutdown bằng AST: `finally`, `except KeyboardInterrupt`,
`destroy_node/on_shutdown/__del__/shutdown/cleanup`, callback đăng ký qua `on_shutdown`/
`atexit.register`/`signal.signal`, và cho phép 1 bước gọi hàm helper (`finally: node.stop()`).
C++ chỉ có heuristic mức `info` và bỏ qua header (`.hpp/.h`, thường là lớp wrapper).

**Check 9** đọc `export X=1`, `X: 1`, `os.environ['X'] = '1'`, `SetEnvironmentVariable('X','1')`,
`setenv/putenv`. Bỏ qua dòng comment và ví dụ trong docstring, không tính `os.environ.get('X', '0')`.
Khác giá trị giữa các file là **hợp lệ** nếu bạn dùng mỗi robot một domain hoặc sim/real khác RMW,
vì vậy đó chỉ là `warning` — `colcon test` chỉ fail khi 1 file tự mâu thuẫn.

## Tắt finding (áp dụng cho MỌI check, kể cả loại không tự sửa được)

```python
self.pub = self.create_publisher(Twist, "cmd_vel", 10)  # cạnh dòng đó (trên 2 dòng / dưới 1 dòng):
# LINT-IGNORE[multirobot:8] safety_stopper node gửi lệnh dừng

# LINT-DISABLE[multirobot:9] mỗi robot một domain   <- bất kỳ đâu trong file: tắt check 9 cho file đó
```

Hoặc `--exclude GLOB`. Marker `LINT[multirobot:N] TODO` do `--fix` chèn cũng làm lần quét sau không
báo lại dòng đó (giống `# noqa`).

## Nguyên tắc `--fix`

Chỉ chèn comment TODO / disclaimer docstring; **không bao giờ** đổi giá trị số hay logic. Thêm:
- mỗi file sửa xong được `compile()` lại, hỏng thì **không ghi**;
- giữ nguyên BOM và kiểu xuống dòng (CRLF/LF); file dùng CR đơn lẻ bị bỏ qua;
- không chèn vào bên trong chuỗi nhiều dòng hay sau dòng kết thúc bằng `\`;
- một dòng dính nhiều finding chỉ nhận một comment; chạy lần 2 không đổi gì (idempotent).

## Tích hợp CI

1. **Script độc lập:** `python3 multirobot_lint.py .`
2. **`colcon test`:** `test/test_multirobot_lint_self.py` quét chính package; chỉ `error` và lỗi nội
   bộ làm fail. `warning/info` được in ra log.
3. **GitHub Actions:** `.github/workflows/multirobot-lint.yml` chạy test + lint, xuất annotation,
   upload báo cáo JSON; không tự `--fix` rồi commit ngược.

## Giới hạn đã biết

- Heuristic: check 1/3/4 dựa trên quy ước đặt tên; có thể có false negative với code đặt tên khác
  và false positive hiếm. Dùng `LINT-IGNORE` thay vì sửa regex khi gặp false positive.
- Check 8 không theo dõi luồng gọi xuyên file: thư viện publish `cmd_vel` còn node gọi nó xử lý dừng ở
  file khác sẽ bị báo — hãy `LINT-IGNORE` kèm lý do.
- C++ mới có check 8 (info) và 9; các check còn lại chỉ phân tích Python.
- ROS1 (`rospy`) không được hỗ trợ.
