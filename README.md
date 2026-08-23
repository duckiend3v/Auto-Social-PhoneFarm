# Auto Social PhoneFarm

Ứng dụng Desktop (PySide6 / Python) chuyên nghiệp giúp quản lý hệ thống Phone Farm Android qua ADB, hỗ trợ điều khiển đa thiết bị đồng thời và tự động hóa các tác vụ nuôi nick mạng xã hội (Facebook, Google Warm-up, v.v.).

---

## 🌟 Tính năng chính

### 1. Quản lý & Điều khiển Phone Farm qua ADB
- **Quét thiết bị tự động**: Nhận diện tức thì tất cả điện thoại/giả lập Android đang kết nối qua cáp hoặc Wi-Fi (`adb devices`).
- **Màn hình lưới đa thiết bị (ADB Screen Wall)**: Xem và điều khiển trực tiếp nhiều máy cùng lúc qua `scrcpy` tích hợp, tự động sắp xếp layout cửa sổ gọn gàng.
- **Điều khiển hàng loạt**: Gửi thao tác chạm (tap), vuốt (swipe), gõ văn bản (`ADBKeyboard`), phím cứng (Home, Back, Recent Apps) đồng thời tới nhiều máy.
- **Quản lý Mạng & Proxy**: Cấu hình Proxy độc lập theo từng thiết bị (HTTP / Socks5 / College Proxy), kiểm tra IP thực tế và quản lý kết nối Wi-Fi.

### 2. Tự động hóa nuôi nick & Tương tác Mạng xã hội (Facebook Automation)
- **Đăng nhập đa phương thức**: Hỗ trợ đăng nhập tự động bằng UID / Password / 2FA / Cookie / Token / Mail khôi phục.
- **Nuôi tương tác tự nhiên**:
  - Tự động lướt Newsfeed và xem Story / Facebook Reels.
  - Thả like, reaction ngẫu nhiên theo xác suất cấu hình.
  - Gợi ý kết bạn & tự động chấp nhận lời mời kết bạn.
  - Tự động Follow Fanpage và Tham gia Group Facebook theo danh sách UID/Link.
- **Quản lý Hồ sơ & Profile**:
  - Cập nhật ảnh đại diện (Avatar) hàng loạt từ thư viện ảnh hoặc tải tự động.
  - Tự động đổi tiểu sử (Bio) theo kịch bản.
  - Kiểm tra trạng thái tài khoản Live / Die hàng loạt theo UID.

### 3. Tối ưu Trust Score & Warm-up thiết bị
- **Google Warm-up**: Tự động mở ứng dụng Google, tìm kiếm và lướt đọc báo để tăng độ tin cậy và hạn chế checkpoint tài khoản.
- **Quản lý dữ liệu an toàn**: Lưu hồ sơ thiết bị (`device_profiles.json`) và lịch sử hành động (`follow_history.json`) để tránh trùng lặp thao tác.

---

## 🚀 Hướng dẫn Cài đặt & Khởi chạy (Windows)

### Yêu cầu hệ thống:
- Windows 10/11 (64-bit).
- Python 3.10 trở lên.
- Cáp kết nối USB hoặc mạng LAN hỗ trợ ADB.

### Cách 1: Khởi chạy nhanh bằng script tự động (Khuyên dùng)
Dự án đã tích hợp script tự động tạo môi trường ảo, cài đặt thư viện và tải `scrcpy` + `platform-tools`:
```powershell
# Chạy file bootstrap để tự động cài đặt môi trường
.\bootstrap.bat

# Khởi chạy ứng dụng
.\run.bat
```

### Cách 2: Cài đặt thủ công bằng dòng lệnh
```powershell
# 1. Tạo và kích hoạt môi trường ảo Python
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Cài đặt các thư viện cần thiết
pip install -r requirements.txt

# 3. Khởi chạy ứng dụng
python main.py
```

---

## 🛠️ Đóng gói ứng dụng (Build EXE Portable)

Nếu muốn đóng gói ứng dụng thành file `.exe` độc lập để chia sẻ hoặc chạy trên máy khác:
```powershell
.\build_exe.bat
```
File thực thi và dữ liệu đóng gói sẽ nằm trong thư mục `dist/fb_tool_portable/`.

---

## 📁 Cấu trúc thư mục

```text
├── src/                    # Mã nguồn chính của ứng dụng
│   ├── app.py              # Giao diện chính PySide6 (UI/UX)
│   ├── adb_client.py       # Core xử lý ADB và kịch bản tự động hóa
│   ├── models.py           # Data models (DeviceInfo, v.v.)
│   ├── profiles.py         # Quản lý hồ sơ thiết bị & proxy
│   └── follow_store.py     # Quản lý lịch sử follow/tương tác
├── scripts/                # Script hỗ trợ (bootstrap, run, make_dist_zip)
├── assets/                 # Icons và hình ảnh giao diện
├── data/                   # Dữ liệu cục bộ (cấu hình, avatar, lịch sử)
├── tools/                  # Công cụ bên thứ 3 (scrcpy, adb platform-tools)
├── requirements.txt        # Danh sách thư viện Python
├── bootstrap.bat           # Script cài đặt tự động
├── run.bat                 # Script chạy ứng dụng
└── main.py                 # File entry point khởi chạy
```

---

## 🔒 Bảo mật & Lưu ý
- Không chia sẻ file `data/device_profiles.json` chứa thông tin tài khoản hoặc Proxy cá nhân.
- File `.gitignore` đã được cấu hình sẵn để bảo vệ dữ liệu nhạy cảm và các file nhị phân lớn khi đẩy lên Git.
