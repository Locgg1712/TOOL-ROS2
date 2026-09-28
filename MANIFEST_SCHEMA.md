# Chuẩn Manifest cho ROS2 Node

Mỗi node trong dự án nên có **1 file YAML mô tả nó** để Claude (và người mới) hiểu nhanh mục đích, giao diện (topic/service/param) mà không cần đọc hết source code hay chạy sống hệ thống. File này khai báo **ý định thiết kế** — tool `validate_node` sẽ tự đối chiếu nó với trạng thái chạy thật.

## Vị trí & tên file

Mặc định server tìm trong thư mục `./ros2_manifests/` (đổi bằng biến môi trường `ROS2_MCP_MANIFEST_DIR`). Tên file = tên node:

```
ros2_manifests/
  talker.yaml
  cmd_vel_bridge.yaml
  lidar_filter.yaml
```

Khuyến nghị: đặt thư mục này ở gốc mỗi package ROS2 (hoặc gốc workspace nếu ít node), commit chung với code.

## Schema

```yaml
node: <tên node, không có dấu /, khớp với tên khi chạy>
package: <tên package ROS2 chứa node>
status: <draft | stable | deprecated>          # optional
description: >
  Mô tả ngắn gọn node này làm gì, tại sao tồn tại.

publishes:
  - topic: /ten_topic
    type: pkg_msgs/msg/TypeName
    description: Ý nghĩa của message này

subscribes:
  - topic: /ten_topic_khac
    type: pkg_msgs/msg/TypeName
    description: Node dùng dữ liệu này để làm gì

services_provided:
  - service: /ten_service
    type: pkg_srvs/srv/TypeName
    description: Service này làm gì khi được gọi

services_used:
  - service: /service_cua_node_khac
    type: pkg_srvs/srv/TypeName
    description: Tại sao node này cần gọi service kia

parameters:
  - name: rate_hz
    type: double
    default: 10.0
    description: Tần số publish

dependencies:
  - <package khác mà node này phụ thuộc, nếu có>

notes: >
  Bất kỳ lưu ý vận hành nào — ví dụ node này cần chạy sau node X,
  hoặc chỉ hoạt động đúng trong sim, hoặc có known issue gì.
```

Chỉ `node` là bắt buộc. Các mục còn lại có thể để rỗng (`[]` hoặc bỏ hẳn) nếu không áp dụng — `validate_node` sẽ coi phần rỗng là "không khai báo gì" và so với thực tế.

## Cách Claude dùng 3 tool liên quan

| Tool | Khi nào dùng |
|---|---|
| `list_manifests` | Bước đầu khám phá dự án — xem đã có node nào được tài liệu hoá |
| `get_manifest(node_name)` | Đọc ý định thiết kế của 1 node trước khi debug |
| `validate_node(node_name)` | Đối chiếu ngay: node có publish/subscribe/serve đúng như tài liệu không |

Kết quả `validate_node` trả về 2 loại lệch cho mỗi nhóm (publish/subscribe/service):
- `missing_in_runtime`: có trong manifest nhưng **không thấy khi chạy thật** → có thể node chưa init, sai điều kiện, hoặc tài liệu đã lỗi thời.
- `undeclared_in_manifest`: **thấy khi chạy thật** nhưng chưa được ghi vào manifest → tài liệu thiếu, cần bổ sung.

## Ví dụ

Xem `ros2_manifests/example_talker.yaml` đi kèm.

## Liên quan: multirobot_lint

Manifest mô tả **ý định thiết kế ở tầng giao diện** (topic/service/param). Bộ scan tĩnh `multirobot_lint.py` (xem `MULTIROBOT_LINT.md`) bổ sung ở tầng khác: các **cạm bẫy code** thường gặp khi hệ thống multi-robot chuyển từ sim sang thật (timeout, an toàn, shutdown...). Hai công cụ không thay thế nhau — validate_node kiểm tra "node có làm đúng như khai báo không", multirobot_lint kiểm tra "code có mắc các lỗi kinh điển sim→thật không".
