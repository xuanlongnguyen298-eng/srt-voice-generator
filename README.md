# Tạo voice từ file SRT hoặc TXT

Ứng dụng Windows đơn giản để chọn file `.srt` hoặc `.txt` có timestamp và tạo WAV
lồng tiếng bằng Blaze.
Mặc định dùng model `blaze-tts-3`, speaker `HN-Nam-2-TM`, 10 worker. Các lần gửi
request TTS được giãn cách tối thiểu 1 giây dùng chung giữa các worker để giảm
đợt gọi dồn; khi nhận HTTP 429, ứng dụng chờ theo thông tin Blaze trả về rồi thử
lại tối đa 10 lần. Tốc độ được tự điều chỉnh để khớp timestamp, không cắt lời
hay cho các câu chồng nhau.

## Dùng nhanh

1. Đặt nguyên thư mục này ở vị trí bạn muốn sử dụng lâu dài.
2. Cài Python 3.10 trở lên nếu máy chưa có.
3. Nhấp đúp `Cài đặt và mở.bat`. Lần đầu cần Internet để tạo môi trường Python
   riêng trong thư mục và cài hai thư viện cần thiết.
4. Sao chép `.env.example` thành `.env`, nhập Blaze API key sau dấu `=` ở dòng
   `BLAZE_API_KEY=` rồi lưu file. Không thêm dấu ngoặc kép.
5. Chọn voice trong danh sách. Có thể gõ tên hoặc ID vào ô lọc; bấm **Nghe thử**
   để nghe clip mẫu của voice đang chọn.
6. Bấm **Thêm file** để chọn một hoặc nhiều file `.srt`/`.txt`. Dùng nút **Lên**
   và **Xuống** để sắp xếp thứ tự. Mỗi file mới được gán voice đang chọn; để đổi
   voice riêng cho file, chọn file đó trong hàng đợi, chọn voice bên dưới rồi bấm
   **Gán voice**. Có thể chọn nhiều file để gán cùng một voice.
7. Bấm **Chạy hàng đợi**. Ứng dụng lần lượt tạo audio cho từng file bằng voice
   được hiển thị cạnh file đó. WAV được tạo cạnh file phụ đề tương ứng, với tên
   `<tên-file>_voice.wav`.

Có thể thu nhỏ ứng dụng trong khi hàng đợi chạy. Kết quả từng file và lỗi (nếu
có) được ghi vào log; khi một file lỗi, ứng dụng vẫn tiếp tục file tiếp theo.
Nếu WAV đầu ra đã tồn tại, ứng dụng hỏi một lần trước khi chạy: ghi đè các WAV
đó, hoặc bỏ qua file đã có WAV.

Các lần sau chỉ cần nhấp đúp `Cài đặt và mở.bat`, thêm các file cần chạy và sắp
xếp hàng đợi.
Audio trung gian/cache nằm trong thư mục `<tên-file>_voice_parts` cạnh file SRT,
giúp chạy lại mà không phải gửi lại những câu đã tạo xong.

Nếu chuyển thư mục sang vị trí khác, launcher sẽ tự tạo lại môi trường Python
trong vị trí mới. Cần có Internet cho lần cài lại này.

## Thành phần

- `app.py` — giao diện chọn file và chạy công việc nền.
- `srt_tts_timeline.py` — xử lý SRT, gọi Blaze, căn audio với timeline.
- `requirements.txt` — thư viện Python tối thiểu cần cho chức năng.
- `ffmpeg\ffmpeg.exe` — tùy chọn; có thể thay bằng FFmpeg đã cài trong `PATH`.
- `Cài đặt và mở.bat` — tạo môi trường `.venv`, cài dependency và mở giao diện.
- Danh sách voice và clip nghe thử được tải từ Blaze khi mở ứng dụng.

Ứng dụng hiển thị thời gian đã chạy khi tạo thành công hoặc khi tác vụ thất bại.

Không chia sẻ file `.env`: file này chứa API key và bị loại khỏi Git. Nếu clone
repo hoặc chuyển ứng dụng sang máy khác, sao chép `.env.example` thành `.env`,
nhập key riêng của bạn. Nút **Mở file .env** trong giao diện sẽ mở đúng file cấu
hình để bạn chỉnh sửa.

FFmpeg không được đưa vào repo để tránh đẩy binary lớn. Cài FFmpeg trên Windows
và thêm `ffmpeg.exe` vào `PATH`, hoặc đặt `ffmpeg.exe` cục bộ trong thư mục
`ffmpeg`. Launcher hỗ trợ cả hai cách.

## Chạy bằng lệnh

Sau khi cài, mở PowerShell tại thư mục ứng dụng:

```powershell
.\.venv\Scripts\python.exe .\srt_tts_timeline.py "C:\Video\subtitles.srt" --output "C:\Video\subtitles_voice.wav" --model blaze-tts-3 --speaker-id HN-Nam-2-TM --workers 10
```

`BLAZE_API_KEY` cần được cấu hình trong `.env` của thư mục ứng dụng hoặc trong
environment trước khi chạy lệnh. FFmpeg cần được cài và có trong `PATH`; nếu
đặt riêng `ffmpeg.exe` trong thư mục `ffmpeg`, truyền thêm
`--ffmpeg ".\ffmpeg\ffmpeg.exe"`.

File `.txt` cần có cấu trúc timestamp giống các cue SRT: mỗi khối có dòng
`HH:MM:SS,mmm --> HH:MM:SS,mmm`, dòng kế tiếp là nội dung lời thoại, các khối
được ngăn cách bằng một dòng trống. Số thứ tự cue là tùy chọn. Ví dụ:

```text
00:00:00,000 --> 00:00:03,000
Xin chào, đây là câu thoại đầu tiên.

00:00:03,000 --> 00:00:06,000
Đây là câu thoại tiếp theo.
```

## Mở giao diện từ Terminal VS Code ở thư mục dự án

Sau khi clone repo, chạy `Cài đặt và mở.bat`. Để mở lại giao diện trong Terminal:

```powershell
.\.venv\Scripts\pythonw.exe .\app.py
```

Cửa sổ riêng sẽ hiện nút **Chọn SRT và tạo voice**; terminal không hiển thị
được nút GUI trực tiếp.
