# Skill: Từ bài toán sang kiến trúc Multi-Robot ROS2

## Mục đích

Hướng dẫn Claude biến một yêu cầu dạng bài toán (ví dụ: "3 robot tuần tra 3 khu vực
độc lập", "2 tay robot gắp thả song song không can thiệp nhau") thành một kiến trúc
ROS2 cụ thể, dùng được ngay với bộ tool `ros2-mcp` hiện có (namespace đã được hỗ trợ
sẵn qua tham số `namespace` trong `get_node_info`/`validate_node`).

**Đọc file này khi:** người dùng mô tả một nhiệm vụ liên quan đến từ 2 robot trở lên
chạy song song, dù họ không dùng đúng từ "multi-robot".

## 1. Nguyên tắc cốt lõi

Mỗi robot = một namespace ROS2 riêng. Đây là cách tách biệt rẻ nhất và ít lỗi nhất:
không cần đổi tên topic thủ công trong code, chỉ cần remap namespace lúc launch.

```
/robot1/cmd_vel      /robot1/scan      /robot1/odom
/robot2/cmd_vel      /robot2/scan      /robot2/odom
/robot3/cmd_vel      /robot3/scan      /robot3/odom
```

Nếu 2 robot vô tình publish/subscribe chung 1 topic không namespace (ví dụ cả 2 cùng
nghe `/cmd_vel` thay vì `/robot1/cmd_vel`), lệnh gửi cho robot 1 sẽ vô tình điều khiển
luôn cả robot 2 — đây là lỗi phổ biến nhất khi mới làm multi-robot, luôn kiểm tra
bằng `list_topics` xem có topic "trần" (không namespace) nào lẽ ra phải thuộc về
1 robot cụ thể hay không.

## 2. Hai kiểu kiến trúc — chọn đúng loại trước khi thiết kế

| Kiểu | Đặc điểm | Khi dùng |
|---|---|---|
| **Cô lập hoàn toàn** | Mỗi robot 1 `ROS_DOMAIN_ID` riêng, không thấy graph của nhau | Robot thực sự độc lập, không cần biết vị trí/trạng thái của robot khác |
| **Cùng domain, khác namespace** | Cùng 1 `ROS_DOMAIN_ID`, tách bằng namespace, có thể thấy nhau nếu cần | Robot cần phối hợp nhẹ (tránh va nhau, chia vùng) qua 1 topic điều phối chung |

Với yêu cầu "chạy độc lập với nhau" (như người dùng mô tả), mặc định nên chọn **cùng
domain, khác namespace** — vì "độc lập" ở đây thường có nghĩa là *không phụ thuộc lẫn
nhau về logic điều khiển*, chứ không hẳn là *không được phép biết nhau tồn tại*. Nếu
người dùng xác nhận robot cần hoàn toàn tách biệt kể cả ở tầng mạng, mới chuyển sang
domain riêng.

## 3. Quy ước đặt tên

- `robot_id`: chuỗi ngắn, không dấu, không khoảng trắng — `robot1`, `robot2` hoặc tên
  có ý nghĩa như `scout_a`, `arm_left`.
- Namespace: `/<robot_id>`.
- Node full name: `/<robot_id>/<node_name>` (ví dụ `/robot1/lidar_filter`).
- TF frame prefix: `<robot_id>/base_link`, `<robot_id>/odom` — **bắt buộc** phải có
  prefix, nếu không 2 robot sẽ cùng publish frame `base_link` và TF tree bị xung đột
  ngầm (không lỗi rõ ràng, chỉ TF sai vị trí).

## 4. Cấu trúc launch file mẫu

Dùng `GroupAction` + `PushRosNamespace` cho mỗi robot, include cùng 1 launch file dùng
chung logic nhưng khác namespace/tham số:

```python
# fleet_launch.py (minh hoạ ý tưởng, không phải chạy trực tiếp)
from launch import LaunchDescription
from launch.actions import GroupAction, IncludeLaunchDescription
from launch_ros.actions import PushRosNamespace
from launch.launch_description_sources import PythonLaunchDescriptionSource

def make_robot_group(robot_id: str, params_file: str):
    return GroupAction([
        PushRosNamespace(robot_id),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource('single_robot_launch.py'),
            launch_arguments={'params_file': params_file}.items(),
        ),
    ])

def generate_launch_description():
    return LaunchDescription([
        make_robot_group('robot1', 'robot1_params.yaml'),
        make_robot_group('robot2', 'robot2_params.yaml'),
        make_robot_group('robot3', 'robot3_params.yaml'),
    ])
```

Điểm quan trọng: `single_robot_launch.py` viết **một lần duy nhất** như thể chỉ có
1 robot — namespace được áp từ bên ngoài qua `PushRosNamespace`, không hardcode
`/robot1/...` bên trong node.

## 5. Dùng bộ tool ros2-mcp với multi-robot

Các tool hiện có đã hỗ trợ multi-robot qua tham số `namespace`, không cần thêm tool
mới cho việc quan sát cơ bản:

- `list_nodes` → thấy tất cả node của mọi robot cùng lúc (khi cùng domain), namespace
  hiển thị trong kết quả giúp phân biệt robot nào là robot nào.
- `get_node_info(node_name, namespace="/robot1")` → xem riêng 1 node của 1 robot cụ
  thể. **Lặp qua từng `robot_id`** khi cần kiểm tra toàn bộ fleet, đừng chỉ kiểm tra
  1 robot rồi suy ra các robot còn lại giống hệt — mỗi robot có thể lệch cấu hình.
- `echo_topic("/robot1/scan", ...)` → luôn dùng full topic path có namespace, không
  dùng tên topic trần.
- `validate_node(node_name, namespace="/robot2")` → so khớp manifest với từng robot
  riêng biệt (xem mục 6 để biết cách đặt tên manifest cho multi-robot).

## 6. Mở rộng Manifest cho multi-robot

Quy ước đặt tên file trong `ros2_manifests/`: `<robot_id>_<node_name>.yaml`, ví dụ:

```
ros2_manifests/
  robot1_lidar_filter.yaml
  robot2_lidar_filter.yaml
  robot3_lidar_filter.yaml
```

Nếu logic node giống hệt nhau giữa các robot (chỉ khác tham số), có thể thêm field
`robot_id` vào manifest để ghi rõ đây là bản khai cho robot nào, tránh nhầm lẫn khi
đọc bằng `get_manifest`:

```yaml
node: lidar_filter
robot_id: robot1
package: my_fleet_pkg
description: >
  Lọc nhiễu dữ liệu lidar cho robot1. Cấu trúc giống hệt robot2/robot3,
  chỉ khác tham số threshold theo môi trường vận hành riêng.
publishes:
  - topic: /robot1/scan_filtered
    type: sensor_msgs/msg/LaserScan
    description: Dữ liệu lidar đã lọc nhiễu
subscribes:
  - topic: /robot1/scan
    type: sensor_msgs/msg/LaserScan
    description: Dữ liệu lidar thô từ driver
```

Khi gọi `validate_node("lidar_filter", namespace="/robot1")`, cần trỏ đúng file
`robot1_lidar_filter.yaml` — nếu bộ tool hiện tại tìm file theo đúng `node_name`
không kèm `robot_id` thì cần điều chỉnh `_load_manifest` để ưu tiên tìm file
`<namespace_không_dấu>_<node_name>.yaml` trước, fallback về `<node_name>.yaml` nếu
không thấy (áp dụng cho node dùng chung logic không phân biệt theo robot).

## 7. Quy trình gợi ý khi nhận một bài toán multi-robot

1. **Xác định số lượng và vai trò từng robot** — hỏi lại nếu người dùng chỉ nói
   "nhiều robot" mà không rõ số lượng/vai trò có giống nhau không.
2. **Chọn kiến trúc** theo mục 2 (cô lập hoàn toàn hay cùng domain khác namespace).
3. **Đặt namespace/robot_id** theo mục 3.
4. **Vẽ ranh giới private vs shared**: những topic nào chỉ thuộc về 1 robot (hầu hết:
   cmd_vel, scan, odom...), topic nào thực sự cần chia sẻ giữa các robot (ví dụ
   `/fleet/task_assignment` nếu có điều phối trung tâm). Topic shared đặt ở namespace
   gốc hoặc `/fleet/...`, không đặt dưới namespace của riêng robot nào.
5. **Viết/cập nhật manifest** cho từng robot theo mục 6.
6. **Viết launch file** theo mẫu ở mục 4.
7. **Kiểm chứng bằng tool, không suy diễn từ code**: chạy `list_nodes` xem đủ số robot
   × số node dự kiến chưa, dùng `get_node_info` với từng namespace xem topic có đúng
   private/shared như thiết kế không — đặc biệt kiểm tra không có topic nào bị "rò"
   ra ngoài namespace của nó.
8. **Test cách ly hành vi**: publish lệnh (`publish_message`, sau khi đã xác nhận theo
   quy tắc an toàn) cho riêng `/robot1/cmd_vel`, dùng `echo_topic` trên
   `/robot2/odom` và `/robot3/odom` để xác nhận 2 robot còn lại **không** phản ứng —
   đây là bằng chứng thực sự cho "chạy độc lập", không phải chỉ vì code trông tách
   biệt.

## 8. Lỗi thường gặp

- Quên namespace ở 1 chỗ trong code (thường là param file hoặc topic hardcode) →
  robot đó vô tình nghe/gửi nhầm topic của robot khác dù launch file namespace đúng.
- TF frame trùng tên giữa các robot (không có prefix `<robot_id>/`) → TF tree xung
  đột, robot này "nhìn thấy mình" ở vị trí của robot khác.
- Chạy nhiều robot sim trên cùng máy nhưng vô tình dùng chung `ROS_DOMAIN_ID` với một
  robot thật ở gần đó (ví dụ trong phòng lab) → nhiễu chéo giữa hệ thống test và hệ
  thống thật, rất khó phát hiện nếu không chủ động kiểm tra `list_nodes` thấy node lạ.
- Giả định các robot "giống hệt nhau" nên chỉ kiểm tra 1 robot rồi báo cáo chung cho
  cả fleet — luôn lặp qua từng `robot_id` khi xác minh (xem mục 7, bước 7).
