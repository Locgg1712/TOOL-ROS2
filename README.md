# ROS2 MCP Server

Server MCP giúp Claude đọc và tương tác trực tiếp với một hệ thống ROS2 đang chạy: liệt kê node/topic/service, xem message thật, gọi service, và (nếu bạn bật) publish message.

## 1. Yêu cầu

- Đã cài ROS2 (Humble, Iron, Jazzy, ...) và đã `source` môi trường:
  ```bash
  source /opt/ros/<distro>/setup.bash
  ```
- Python 3 cùng môi trường đã có `rclpy` (đi kèm ROS2, không cài qua pip).
- Gói `mcp`:
  ```bash
  pip install mcp[cli]
  ```
  > Nếu dùng virtualenv, tạo bằng `python3 -m venv --system-site-packages venv` để venv vẫn thấy được `rclpy` của hệ thống.

## 2. Chạy thử độc lập (kiểm tra không lỗi cú pháp)

```bash
source /opt/ros/<distro>/setup.bash
python3 server.py
```
Server sẽ chờ input qua stdio (đây là cách MCP client như Claude Desktop/Code sẽ giao tiếp với nó). Nhấn Ctrl+C để thoát.

## 3. Kết nối với Claude Desktop hoặc Claude Code

Thêm vào file config MCP (ví dụ `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ros2": {
      "command": "bash",
      "args": [
        "-c",
        "source /opt/ros/<distro>/setup.bash && python3 /duong/dan/toi/ros2_mcp_server/server.py"
      ]
    }
  }
}
```

Với Claude Code, dùng `claude mcp add` trỏ tới cùng lệnh trên. Khởi động lại Claude, các tool ROS2 sẽ xuất hiện trong danh sách tool khả dụng.

## 4. Danh sách tool (MCP Server)

| Tool | Chức năng | Có thể ảnh hưởng hệ thống thật? |
|---|---|---|
| `list_nodes` | Liệt kê node đang chạy | Không |
| `list_topics` | Liệt kê topic + type | Không |
| `get_topic_info` | Số publisher/subscriber của 1 topic | Không |
| `list_services` | Liệt kê service + type | Không |
| `get_node_info` | Chi tiết publisher/subscriber/service của 1 node | Không |
| `echo_topic` | Bắt N message thật trên 1 topic (như `ros2 topic echo`) | Không |
| `tail_rosout` | Đọc log gần nhất từ `/rosout` | Không |
| `call_service` | Gọi 1 service với request tuỳ ý | **Có thể có**, tuỳ service |
| `publish_message` | Publish message lên 1 topic | **Có**, tắt mặc định |

## 5. Sử dụng CLI Tool Độc Lập (`ros2mcp`) trên Ubuntu

Bên cạnh việc dùng MCP Server qua AI, bạn có thể cài đặt và tương tác trực tiếp với ROS2 qua terminal bằng lệnh `ros2mcp`:

### Cài đặt nhanh trên Ubuntu:
```bash
# Cấp quyền và chạy file cài đặt
chmod +x install.sh
./install.sh
```
Sau khi cài đặt, bạn có thể gõ trực tiếp lệnh `ros2mcp` ở bất cứ thư mục nào thay vì chạy `python3 cli.py`.

### Cách sử dụng:
```bash
# Sourced môi trường ROS2 trước (nếu chưa cấu hình trong .bashrc)
source /opt/ros/<distro>/setup.bash

# Liệt kê nodes / topics / services
ros2mcp nodes
ros2mcp topics
ros2mcp services

# Xem thông tin chi tiết
ros2mcp topic-info /cmd_vel
ros2mcp node-info talker

# Xem tài liệu Manifest của Node
ros2mcp list-manifests
ros2mcp get-manifest talker
ros2mcp validate-node talker

# Echo dữ liệu thực tế (bắt 5 message)
ros2mcp echo /cmd_vel geometry_msgs/msg/Twist -c 5

# Đọc log /rosout
ros2mcp rosout -c 10

# Gọi service
ros2mcp call /reset std_srvs/srv/Trigger

# Publish message (yêu cầu bật biến môi trường + flag --confirm)
ROS2_MCP_ALLOW_PUBLISH=1 ros2mcp pub /cmd_vel geometry_msgs/msg/Twist '{"linear":{"x":0.2}}' --confirm
```

💡 **Mẹo (Tips):**
1. **Dùng kết hợp với AI:**
   Khi AI cần kiểm chứng hệ thống, bạn có thể chạy song song `ros2mcp` trên terminal của mình để theo dõi và xác thực trạng thái thực tế.


## 6. Về an toàn (quan trọng)

`publish_message` có thể gửi lệnh thật lên hệ thống — ví dụ publish lên `/cmd_vel` có thể làm robot di chuyển. Vì vậy:

- Tool này **bị tắt mặc định**. Chỉ bật bằng cách chạy server với biến môi trường `ROS2_MCP_ALLOW_PUBLISH=1`.
- Ngay cả khi bật, mỗi lần gọi Claude hoặc CLI vẫn phải truyền `confirm=true` (hoặc `--confirm`).
- Khuyến nghị: chỉ bật khi test trên simulation (Gazebo, Ignition...) trước, và cân nhắc kỹ trước khi bật trên robot thật.

`call_service` không bị gate cứng như vậy vì nhiều service chỉ đọc (get_parameters, trigger diagnostics...), nhưng một số service có thể có tác dụng phụ thật (reset, arm động cơ...) — hãy tự đánh giá theo từng dự án, có thể thêm allowlist tên service nếu cần.

## 7. Hướng mở rộng thêm

- `list_parameters` / `get_parameter` / `set_parameter` qua parameter services của từng node.
- Hỗ trợ Action (goal/feedback/result), không chỉ topic/service.
- Ghi/đọc rosbag qua `ros2 bag`.
- Tra cứu TF (transform tree) qua `tf2_ros`.
- Thêm allowlist/denylist cho `call_service` và `publish_message` theo tên topic/service cụ thể của dự án bạn.
