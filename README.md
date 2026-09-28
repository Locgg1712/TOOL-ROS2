# ROS2 MCP — Để AI hiểu hệ thống ROS2 của bạn

Công cụ này giúp **Claude và các AI assistant khác nhìn thấy và tương tác trực tiếp với hệ thống ROS2 đang chạy** — thay vì chỉ đoán từ code. AI có thể liệt kê node, đọc message thật, gọi service, phát hiện lỗi, so khớp thiết kế với thực tế tự động, và **quét tĩnh code để bắt cạm bẫy multi-robot kinh điển mà không cần hệ thống nào đang chạy**.

---

## Có 3 cách dùng — chọn cái phù hợp với bạn

| Cách | Dùng khi | Cần gì thêm |
|---|---|---|
| **A. Skill cho Antigravity** | Dùng AI assistant tích hợp trong terminal | Không cần gì thêm |
| **B. MCP Server** | Dùng Claude Desktop hoặc Claude Code | Cài `mcp` package |
| **C. CLI thuần** | Dùng terminal không cần AI | Không cần gì thêm |

Riêng bộ quét `multirobot_lint` (xem mục riêng bên dưới) có thêm cách dùng thứ 4:
chạy trực tiếp `python3 multirobot_lint.py` — không cần bất kỳ yêu cầu chung nào ở
dưới đây (không cần ROS2, không cần `mcp` package).

---

## Yêu cầu chung (cho cách A/B/C — KHÔNG áp dụng cho `multirobot_lint`)

1. **ROS2 đã cài** (Humble, Iron, Jazzy, hoặc mới hơn)
2. **Đã `source` môi trường ROS2** — bước này bắt buộc:
   ```bash
   source /opt/ros/<distro>/setup.bash
   # Ví dụ: source /opt/ros/humble/setup.bash
   ```
3. **Python 3** (đi kèm ROS2, không cần cài thêm)

---

## Cách A — Skill cho Antigravity (Khuyến nghị)

Đây là cách tích hợp sâu nhất. Khi cài xong, bạn chỉ cần nói chuyện bình thường với AI — AI tự biết khi nào cần kiểm tra ROS2.

**Ví dụ bạn nói:** *"Node talker của tôi không gửi được message, debug giúp tôi"*
**AI sẽ tự:** chạy `nodes` → `topics` → `echo` → `rosout` → chẩn đoán → đề xuất fix

**Ví dụ khác:** *"Review code multi-robot của tôi trước khi tôi deploy"*
**AI sẽ tự:** chạy `lint-multirobot` (không cần ROS2 đã source) → chẩn đoán cạm bẫy → đề xuất/áp fix

### Cài đặt

Skill đã có sẵn trong thư mục `.agents/skills/ros2-mcp/` của repo này.
**Không cần làm gì thêm** — AI assistant tự phát hiện khi bạn làm việc trong thư mục này.

Nếu muốn dùng ở bất kỳ đâu (không chỉ trong project này), copy vào global:
```bash
mkdir -p ~/.gemini/config/skills/
cp -r .agents/skills/ros2-mcp ~/.gemini/config/skills/
```

---

## Cách B — MCP Server (cho Claude Desktop / Claude Code)

### Bước 1: Cài package

```bash
# Tạo venv giữ lại quyền truy cập rclpy của hệ thống
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install mcp[cli] pyyaml
```

### Bước 2: Thêm vào config Claude Desktop

Mở file `claude_desktop_config.json` (thường ở `~/.config/claude/`) và thêm:

```json
{
  "mcpServers": {
    "ros2": {
      "command": "bash",
      "args": [
        "-c",
        "source /opt/ros/humble/setup.bash && /đường/dẫn/tới/repo/venv/bin/python3 /đường/dẫn/tới/repo/server.py"
      ]
    }
  }
}
```

> Thay `humble` bằng distro của bạn, và `/đường/dẫn/tới/repo` bằng đường dẫn thực tế.

### Bước 3: Khởi động lại Claude Desktop

Các tool ROS2 sẽ xuất hiện tự động, bao gồm `scan_multirobot_pitfalls` — tool duy nhất
trong server không cần `source` ROS2 để chạy được. Claude có thể gọi chúng khi bạn hỏi
về hệ thống.

**Với Claude Code:**
```bash
claude mcp add ros2 -- bash -c "source /opt/ros/humble/setup.bash && python3 /đường/dẫn/server.py"
```

---

## Cách C — CLI thuần (không cần AI)

Dùng trực tiếp từ terminal, không cần Claude hay bất kỳ AI nào.

### Cài đặt

```bash
bash install.sh
# Tạo lệnh 'ros2mcp' và 'ros2_multirobot_lint' dùng được từ bất kỳ terminal nào
```

> Nếu không có sudo, bạn có thể chạy trực tiếp: `python3 cli.py <lệnh>`

### Các lệnh

```bash
# Xem node nào đang chạy
ros2mcp nodes

# Xem topic nào đang có
ros2mcp topics

# Xem chi tiết 1 topic (ai publish, ai subscribe)
ros2mcp topic-info /cmd_vel

# Xem chi tiết 1 node
ros2mcp node-info talker

# Đọc message thật đang chạy (giống ros2 topic echo)
ros2mcp echo /chatter std_msgs/msg/String

# Đọc log từ /rosout
ros2mcp rosout

# Gọi 1 service
ros2mcp call /add_two_ints example_interfaces/srv/AddTwoInts '{"a":3,"b":5}'

# Publish message (chỉ dùng trên simulation, xem phần An toàn bên dưới)
ros2mcp pub /cmd_vel geometry_msgs/msg/Twist '{"linear":{"x":0.2}}' --confirm

# Quét tĩnh cạm bẫy multi-robot — KHÔNG cần source ROS2
ros2mcp lint-multirobot ./src
ros2mcp lint-multirobot ./src --fix
```

---

## Manifest — Giúp AI hiểu thiết kế node của bạn

Manifest là file YAML mô tả một node làm gì — AI dùng file này để debug thông minh hơn, không cần đọc source code.

### Tạo manifest cho node (tự động)

```bash
# Skill/CLI sẽ tạo draft từ trạng thái thực tế đang chạy
ros2mcp get-manifest talker  # xem ví dụ có sẵn

# Hoặc dùng script skill:
python3 .agents/skills/ros2-mcp/scripts/ros2_cli.py generate-manifest talker --output ros2_manifests/talker.yaml
```

### Cấu trúc file manifest

```yaml
node: talker
package: demo_nodes_cpp
description: >
  Node demo publish chuỗi "Hello World" định kỳ lên /chatter

publishes:
  - topic: /chatter
    type: std_msgs/msg/String
    description: Chuỗi được publish mỗi 1 giây

parameters:
  - name: rate_hz
    type: double
    default: 1.0
    description: Tần số publish (Hz)
```

Đặt file vào `ros2_manifests/<tên_node>.yaml`. AI sẽ tự đọc khi cần.

### So sánh manifest vs thực tế

```bash
ros2mcp validate-node talker
```

Kết quả sẽ chỉ ra ngay chỗ nào node đang làm khác với thiết kế ban đầu.

---

## multirobot_lint — Quét tĩnh cạm bẫy multi-robot (không cần ROS2)

Khác với mọi tool ở trên, `multirobot_lint.py` **không cần ROS2 đã cài, không cần
source môi trường, không cần hệ thống đang chạy**. Đây là phân tích tĩnh thuần
(AST + regex) trên source code, dùng được ngay ở bước thiết kế/review code, hoặc
trong CI.

```bash
# Chạy trực tiếp, không cần cài gì
python3 multirobot_lint.py ./src
python3 multirobot_lint.py ./src --fix              # tự áp các fix an toàn
python3 multirobot_lint.py --list-checks             # xem 9 check hiện có
```

Xem chi tiết đầy đủ (danh sách check, nguyên tắc `--fix`, tích hợp CI) trong
**[MULTIROBOT_LINT.md](./MULTIROBOT_LINT.md)**.

### 3 lớp tích hợp

1. **Script độc lập** — như trên, dùng ngay không cần cài đặt gì.
2. **`colcon test`** — repo này đã là một package `ament_python` (`package.xml` +
   `setup.py`). `test/test_multirobot_lint_self.py` được `colcon test` tự động chạy:
   ```bash
   colcon build --packages-select ros2_mcp_tools
   colcon test --packages-select ros2_mcp_tools
   colcon test-result --verbose
   ```
3. **GitHub Actions** — `.github/workflows/multirobot-lint.yml` chạy trên mọi PR/push
   vào `main`, report-only (không tự `--fix` rồi commit ngược lại nhánh PR).

---

## An toàn — Đọc trước khi dùng `publish`

> ⚠️ **`publish` gửi lệnh thật ra hệ thống** — ví dụ publish lên `/cmd_vel` có thể làm robot di chuyển ngay lập tức.

**MCP Server**: `publish_message` bị tắt mặc định. Chỉ bật bằng:
```bash
ROS2_MCP_ALLOW_PUBLISH=1 python3 server.py
```
Và mỗi lần gọi phải truyền `confirm=true`.

**CLI**: Tương tự, cần thêm flag `--confirm` và set biến môi trường:
```bash
ROS2_MCP_ALLOW_PUBLISH=1 ros2mcp pub /cmd_vel ... --confirm
```

**Khuyến nghị:** Chỉ bật publish khi test trên Gazebo / Ignition / simulation. Không bật trên robot thật trừ khi bạn đã chắc chắn.

`multirobot_lint` không có khái niệm "publish thật" — nó chỉ đọc và (khi dùng
`--fix`) chỉnh sửa file nguồn trên đĩa, không bao giờ chạm vào ROS2 graph.

---

## Danh sách tool / lệnh đầy đủ

| Tool / Lệnh | Làm gì | Cần ROS2 đã source? | Ảnh hưởng hệ thống thật? |
|---|---|---|---|
| `list_nodes` / `nodes` | Liệt kê node đang chạy | Có | Không |
| `list_topics` / `topics` | Liệt kê topic + type | Có | Không |
| `get_topic_info` / `topic-info` | Publisher/subscriber của 1 topic | Có | Không |
| `list_services` / `services` | Liệt kê service + type | Có | Không |
| `get_node_info` / `node-info` | Chi tiết 1 node | Có | Không |
| `echo_topic` / `echo` | Bắt N message thật (như `ros2 topic echo`) | Có | Không |
| `tail_rosout` / `rosout` | Đọc log từ `/rosout` | Có | Không |
| `list_manifests` / `list-manifests` | Xem node nào có manifest | Không | Không |
| `get_manifest` / `get-manifest` | Đọc manifest của 1 node | Không | Không |
| `validate_node` / `validate-node` | So manifest vs runtime, báo lệch | Có | Không |
| `call_service` / `call` | Gọi service | Có | **Có thể có** (tuỳ service) |
| `publish_message` / `pub` | Publish message | Có | **Có** — tắt mặc định |
| `scan_multirobot_pitfalls` / `lint-multirobot` | Quét tĩnh 9 cạm bẫy multi-robot | **Không** | Không (trừ `--fix` sửa file nguồn) |

---

## Câu hỏi thường gặp

**Q: Chạy lệnh bị lỗi `ModuleNotFoundError: No module named 'rclpy'`**
A: Bạn chưa source ROS2. Chạy: `source /opt/ros/<distro>/setup.bash` rồi thử lại.
Ngoại lệ: `lint-multirobot`/`multirobot_lint.py` không cần bước này.

**Q: `ros2mcp` không nhận được message nào (timeout)**
A: Kiểm tra `topic-info` — xem có publisher không. Nếu có publisher nhưng vẫn timeout, thường là QoS mismatch (publisher dùng BEST_EFFORT, subscriber dùng RELIABLE hoặc ngược lại).

**Q: AI nói "không thấy node nào" nhưng tôi thấy node chạy**
A: AI cần vài giây để discovery. Thêm `time.sleep(1)` hoặc thử lại sau 2–3 giây.

**Q: Có thể dùng trên WSL2 không?**
A: Được. Chạy tool bên trong WSL2 (không phải Windows PowerShell). Đảm bảo ROS2 đã cài trong WSL2. `multirobot_lint.py` chạy được trên bất kỳ máy nào có Python 3, kể cả không có WSL2/ROS2.

**Q: `multirobot_lint` báo cùng 1 dòng mãi mãi dù đã `--fix` rồi, làm sao tắt?**
A: Với các check severity `warning`/`info` (1,2,3,4,6), sau khi `--fix` chèn TODO
marker `LINT[multirobot:N]`, lần quét sau sẽ không báo lại dòng đó nữa — nếu vẫn thấy
lặp lại, kiểm tra marker có bị xoá nhầm không. Check severity `error` (hiện chỉ có
check 9) và check cần con người sửa thủ công (7, 8) sẽ luôn báo lại cho đến khi vấn đề
thật sự được giải quyết trong code — đây là chủ đích.

---

## Cấu trúc project

```
TOOL-ROS2/
├── server.py                  # MCP server — cho Claude Desktop / Claude Code
├── cli.py                     # CLI tool — dùng terminal không cần AI
├── multirobot_lint.py         # Linter tĩnh multi-robot — không cần ROS2
├── install.sh                 # Script cài CLI + lint nhanh
├── requirements.txt           # Python dependencies
├── package.xml                # ament_python package (cho colcon build/test)
├── setup.py                   # Entry points: ros2mcp, ros2_mcp_server, ros2_multirobot_lint
├── CLAUDE.md                  # Hướng dẫn workflow cho Claude Code
├── MANIFEST_SCHEMA.md         # Schema chuẩn cho file manifest
├── MULTIROBOT_GUIDE.md        # Hướng dẫn thiết kế kiến trúc multi-robot
├── SIM_VS_REAL.md             # Cạm bẫy khi chuyển từ mô phỏng sang thật
├── MULTIROBOT_LINT.md         # Chi tiết 9 check của multirobot_lint + tích hợp CI
├── ros2_manifests/
│   └── talker.yaml            # Ví dụ manifest
├── test/
│   └── test_multirobot_lint_self.py   # colcon test hook cho multirobot_lint
├── .github/workflows/
│   └── multirobot-lint.yml    # CI gate (Lớp 3)
└── .agents/
    └── skills/
        └── ros2-mcp/              # Skill cho Antigravity AI assistant
            ├── SKILL.md           # Hướng dẫn cho AI
            └── scripts/
                ├── ros2_cli.py        # Script tự chứa (không phụ thuộc file khác)
                └── multirobot_lint.py # Bản sao tự chứa của linter, cho skill dùng độc lập
```
