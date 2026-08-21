# Skill: Mô phỏng vs Robot thật

## Mục đích

File này giúp Claude không mang nguyên kết luận/tham số đã kiểm chứng trong mô phỏng
(Gazebo, Ignition, Webots...) áp thẳng lên robot thật mà không xét lại. Trong sim,
rất nhiều thứ "vừa đủ tốt để hoạt động" — nhưng lý do nó hoạt động thường là vì sim đã
đơn giản hoá đúng cái phần khiến robot thật gặp vấn đề.

**Đọc file này khi:** người dùng chuẩn bị chạy `publish_message`/`call_service` lên
một hệ thống không rõ là sim hay thật, khi chuyển từ test sim sang test robot thật,
hoặc khi kết quả `echo_topic`/`validate_node` "trông ổn" trong sim nhưng câu hỏi thực
tế là về hành vi trên phần cứng.

## 1. Thời gian & đồng bộ

- Sim thường publish `/clock` và các node dùng `use_sim_time=true` — nếu quên set
  tham số này trên 1 node, node đó chạy theo wall-clock trong khi phần còn lại chạy
  theo sim-clock → lệch thời gian, TF/log timestamp sai mà không báo lỗi rõ ràng.
- Robot thật không có `/clock` giả lập — độ trễ mạng, jitter CPU, độ trễ driver là
  thật, không phải mô hình lý tưởng của sim. Một loop điều khiển chạy "mượt" 50Hz
  trong sim (1 máy, loopback) có thể tụt xuống 20Hz thật khi phải qua WiFi.
- Trước khi kết luận tần số publish đạt yêu cầu, dùng `echo_topic` để đo tần số **trên
  chính hệ thống đang test**, không suy từ số liệu đã đo trong sim.

## 2. Cảm biến

- Trong sim: dữ liệu sạch, không nhiễu, không occlusion, không lệch calib, không mất
  gói. `sensor_msgs/LaserScan` từ Gazebo gần như lý tưởng.
- Trên robot thật: nhiễu, dropout, độ trễ phần cứng, lỗi calibration, phản xạ/ánh sáng
  ảnh hưởng camera-lidar. Code lọc dữ liệu (filter, outlier rejection) mà chưa test
  trong sim không có việc gì để làm — chỉ lộ ra khi chạy thật.
- Dùng `echo_topic` để xem vài message thật trước khi tin dữ liệu, đặc biệt so sánh
  range/giá trị với thông số trong datasheet cảm biến.

## 3. Actuator & động lực học

- Sim thường dùng mô hình động lực học đơn giản hoá: bỏ qua ma sát, backlash, quán
  tính chính xác, độ trễ cơ khí, giới hạn dòng điện/mô-men thật của động cơ.
- Lệnh publish một giá trị "an toàn trong sim" (ví dụ `linear.x: 0.5`) có thể vượt quá
  giới hạn thực tế của robot, gây giật, quá dòng, hoặc phản ứng không tuyến tính.
- **Quy tắc bắt buộc khi chuyển sang thật:** luôn publish giá trị nhỏ nhất có ý nghĩa
  trước, quan sát phản hồi qua `echo_topic` trên topic cảm biến liên quan (odometry,
  IMU...), rồi mới tăng dần. Không nhảy thẳng lên giá trị đã dùng trong sim.

## 4. An toàn vật lý

- Sim không có rủi ro va chạm/ngã/làm hỏng thiết bị. Robot thật thì có, kể cả robot
  nhỏ (kẹp tay, va đập bánh xe, rơi từ độ cao).
- Trước khi gọi `publish_message` hoặc `call_service` có tác dụng phụ trên hệ thống
  chưa xác nhận là sim: hỏi lại người dùng đây có phải môi trường mô phỏng không
  (theo đúng nguyên tắc trong CLAUDE.md — không tự suy diễn).
- Nếu là robot thật: xác nhận có người giám sát và có thể ngắt khẩn cấp (e-stop /
  service dừng) trước khi test hành vi mới, không chỉ trước khi test hành vi nguy hiểm
  rõ ràng — hành vi tưởng chừng vô hại (xoay chậm) vẫn có thể gây hại nếu môi trường
  khác giả định.

## 5. Mạng & discovery (DDS)

- Sim thường chạy trên 1 máy — DDS discovery qua loopback gần như luôn ổn định,
  multicast không bị chặn.
- Robot thật thường nhiều máy (onboard computer + máy điều khiển) qua WiFi/Ethernet.
  Router/firewall có thể chặn multicast DDS → node "thấy nhau" trong sim nhưng
  `list_nodes`/`list_topics` trên hệ thống thật lại thiếu node, dễ bị hiểu nhầm là
  lỗi logic code trong khi thực chất là lỗi mạng/discovery.
- Nếu `list_nodes` trả về ít node hơn kỳ vọng trên hệ thống thật, đừng vội kết luận
  node chưa chạy — cân nhắc khả năng discovery bị chặn (kiểm tra `ROS_DOMAIN_ID`
  trùng khớp giữa các máy, kiểm tra multicast có bị firewall chặn không) trước khi
  chẩn đoán sâu vào code.

## 6. QoS thực tế khác QoS mặc định trong sim

- Nhiều driver cảm biến thật (lidar, camera) dùng QoS **Best Effort** để ưu tiên
  thông lượng thay vì đảm bảo gói tin, trong khi node demo/sim đôi khi mặc định
  Reliable.
- Công cụ `echo_topic` trong bộ ros2-mcp hiện subscribe với QoS mặc định
  `QoSProfile(depth=10)` (Reliable). Nếu publisher trên robot thật là Best Effort,
  `echo_topic` có thể time out với `captured_count: 0` **không phải vì topic không
  có dữ liệu**, mà vì QoS không tương thích. Khi gặp kết quả 0 message trên hệ thống
  thật (khác với sim), đây là nghi vấn đầu tiên cần loại trừ trước khi kết luận
  publisher có vấn đề.

## 7. Checklist trước khi chuyển từ sim sang robot thật

1. Đã xác nhận rõ với người dùng đây là robot thật, không phải sim.
2. Đã kiểm tra `list_nodes`/`list_topics` trên chính hệ thống thật (không dùng lại
   kết quả từ lần test sim).
3. Đã đo tần số/giá trị thật qua `echo_topic`, so sánh với kỳ vọng từ sim — nếu lệch
   nhiều, tìm hiểu nguyên nhân trước khi tiếp tục.
4. Nếu cần `publish_message`: bắt đầu với giá trị nhỏ, có người giám sát, có phương án
   dừng khẩn cấp.
5. Không dùng lại nguyên các con số tham số (tốc độ, gain, threshold) đã tune trong
   sim mà không kiểm chứng lại — nêu rõ với người dùng đây là điểm cần tune lại,
   không phải copy nguyên.

## 8. Nguyên tắc chung khi báo cáo

Khi mô tả kết quả kiểm thử, luôn ghi rõ **đang test trên sim hay robot thật** — không
dùng câu chung chung như "hoạt động đúng" mà không nêu môi trường, vì hai kết luận đó
có mức độ tin cậy rất khác nhau.
