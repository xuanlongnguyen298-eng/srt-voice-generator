from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import winsound
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import requests
from dotenv import dotenv_values


APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = APP_DIR / "srt_tts_timeline.py"
ENV_PATH = APP_DIR / ".env"
FFMPEG_PATH = APP_DIR / "ffmpeg" / "ffmpeg.exe"
PYTHON_PATH = APP_DIR / ".venv" / "Scripts" / "python.exe"
VOICE_CATALOG_URL = "https://api.blaze.vn/v1/tts/list-speaker-ids"
VOICE_PREVIEW_URL = "https://api.blaze.vn/v1/tts/voices/{voice_id}/preview"


class VoiceGeneratorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Tạo voice từ file SRT - Blaze")
        self.root.geometry("900x540")
        self.root.minsize(720, 440)
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.voices: dict[str, dict[str, str]] = {}
        self.preview_path: Path | None = None

        container = ttk.Frame(root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(2, weight=1)
        container.rowconfigure(4, weight=1)

        ttk.Label(
            container,
            text="Tạo file voice từ phụ đề SRT",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(
            container,
            text="Model Blaze TTS 3 · Chọn voice và nghe thử · Tự căn tốc độ theo timestamp",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 14))

        ttk.Label(container, text="File phụ đề:").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        self.srt_path = tk.StringVar()
        self.path_entry = ttk.Entry(container, textvariable=self.srt_path)
        self.path_entry.grid(row=2, column=1, columnspan=3, sticky="ew")

        ttk.Label(container, text="Voice:").grid(
            row=3, column=0, sticky="w", padx=(0, 8)
        )
        self.voice_choice = tk.StringVar(value="Đang tải danh sách voice...")
        self.voice_search = tk.StringVar()
        self.voice_search.trace_add("write", self.filter_voices)
        self.voice_search_entry = ttk.Entry(
            container,
            textvariable=self.voice_search,
        )
        self.voice_search_entry.insert(0, "Gõ tên hoặc ID để lọc")
        self.voice_search_entry.bind(
            "<FocusIn>",
            lambda _event: (
                self.voice_search_entry.delete(0, tk.END)
                if self.voice_search_entry.get() == "Gõ tên hoặc ID để lọc"
                else None
            ),
        )
        self.voice_search_entry.grid(row=3, column=1, sticky="ew", padx=(0, 8))
        self.voice_combo = ttk.Combobox(
            container,
            textvariable=self.voice_choice,
            state="disabled",
            width=42,
        )
        self.voice_combo.grid(row=3, column=2, sticky="ew")
        self.voice_combo.bind("<<ComboboxSelected>>", self.on_voice_selected)
        self.preview_button = ttk.Button(
            container,
            text="Nghe thử",
            command=self.preview_selected_voice,
            state=tk.DISABLED,
        )
        self.preview_button.grid(row=3, column=3, sticky="e", padx=(8, 0))

        self.log = scrolledtext.ScrolledText(
            container, height=14, state=tk.DISABLED, wrap=tk.WORD
        )
        self.log.grid(
            row=4, column=0, columnspan=4, sticky="nsew", pady=(16, 8)
        )

        controls = ttk.Frame(container)
        controls.grid(row=5, column=0, columnspan=4, sticky="ew")
        controls.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Chọn file SRT để bắt đầu.")
        ttk.Label(controls, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.key_button = ttk.Button(
            controls, text="Mở file .env", command=self.open_env_file
        )
        self.key_button.grid(row=0, column=1, padx=(8, 8))
        self.choose_button = ttk.Button(
            controls,
            text="Chọn SRT và tạo voice",
            command=self.choose_and_generate,
            style="Accent.TButton",
        )
        self.choose_button.grid(row=0, column=2)

        self.root.after(100, self.read_messages)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.load_voice_catalog()

    def append_log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def choose_and_generate(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file phụ đề có timestamp",
            filetypes=[
                ("SRT hoặc TXT có timestamp", "*.srt *.txt"),
                ("SRT subtitles", "*.srt"),
                ("Text files", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.srt_path.set(path)
        self.start_generation(Path(path))

    def api_key(self) -> str | None:
        key = dotenv_values(ENV_PATH).get("BLAZE_API_KEY")
        if isinstance(key, str) and key.strip():
            return key.strip()
        return None

    def load_voice_catalog(self) -> None:
        key = self.api_key()
        if not key:
            self.voice_choice.set("Hãy cấu hình API key trong file .env")
            self.status.set("Thiếu Blaze API key trong file .env.")
            return
        self.status.set("Đang tải danh sách voice từ Blaze...")
        threading.Thread(
            target=self.fetch_voice_catalog,
            args=(key,),
            daemon=True,
        ).start()

    def fetch_voice_catalog(self, key: str) -> None:
        try:
            response = requests.get(
                VOICE_CATALOG_URL,
                headers={"Authorization": f"Bearer {key}"},
                timeout=(15, 60),
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Blaze không tải được danh sách voice "
                    f"(HTTP {response.status_code}): {response.text[:400]}"
                )
            payload = response.json()
            entries = payload.get("list_speakers") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise RuntimeError("Blaze trả về danh sách voice không đúng định dạng.")

            voices: list[dict[str, str]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                voice_id = entry.get("id")
                if not isinstance(voice_id, str) or not voice_id.strip():
                    continue
                voices.append(
                    {
                        "id": voice_id.strip(),
                        "name": str(entry.get("name") or voice_id).strip(),
                        "gender": str(entry.get("gender") or "").strip(),
                        "language": str(entry.get("language") or "").strip(),
                        "province": str(entry.get("province") or "").strip(),
                        "type": str(entry.get("type") or "").strip(),
                    }
                )
            if not voices:
                raise RuntimeError("Blaze không trả về voice nào để chọn.")
            voices.sort(
                key=lambda voice: (
                    voice["language"] != "vi",
                    voice["name"].casefold(),
                    voice["id"].casefold(),
                )
            )
            self.messages.put(("voices_loaded", voices))
        except (requests.RequestException, requests.JSONDecodeError, RuntimeError) as exc:
            self.messages.put(("voices_error", str(exc)))

    @staticmethod
    def voice_label(voice: dict[str, str]) -> str:
        details = [
            value
            for value in (voice["gender"], voice["province"], voice["language"])
            if value
        ]
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{voice['name']}{suffix} [{voice['id']}]"

    def selected_voice(self) -> dict[str, str] | None:
        return self.voices.get(self.voice_choice.get())

    def filter_voices(self, *_args: str) -> None:
        if not hasattr(self, "voices") or not self.voices:
            return
        query = self.voice_search.get().strip().casefold()
        if query == "gõ tên hoặc id để lọc":
            query = ""
        matches = [
            label
            for label, voice in self.voices.items()
            if not query
            or query in label.casefold()
            or query in voice["id"].casefold()
        ]
        self.voice_combo.configure(values=matches)
        if matches and self.voice_choice.get() not in matches:
            self.voice_choice.set(matches[0])
        elif not matches:
            self.voice_choice.set("Không tìm thấy voice")

    def on_voice_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        voice = self.selected_voice()
        if voice:
            self.status.set(f"Đã chọn voice: {voice['name']} ({voice['id']}).")

    def preview_selected_voice(self) -> None:
        voice = self.selected_voice()
        key = self.api_key()
        if not voice:
            messagebox.showerror(
                "Chưa chọn voice",
                "Hãy chọn một voice trong danh sách.",
                parent=self.root,
            )
            return
        if not key:
            messagebox.showerror(
                "Chưa cấu hình Blaze API key",
                "Hãy điền BLAZE_API_KEY trong file .env rồi mở lại ứng dụng.",
                parent=self.root,
            )
            return
        ffmpeg = str(FFMPEG_PATH) if FFMPEG_PATH.is_file() else shutil.which("ffmpeg")
        if not ffmpeg:
            messagebox.showerror(
                "Thiếu FFmpeg",
                "Không tìm thấy FFmpeg đi kèm hoặc đã cài trên máy.",
                parent=self.root,
            )
            return
        self.preview_button.configure(state=tk.DISABLED)
        self.status.set(f"Đang tải bản nghe thử: {voice['name']}...")
        threading.Thread(
            target=self.fetch_preview,
            args=(voice, key, ffmpeg),
            daemon=True,
        ).start()

    def fetch_preview(
        self, voice: dict[str, str], key: str, ffmpeg: str
    ) -> None:
        mp3_path: Path | None = None
        wav_path: Path | None = None
        try:
            response = requests.get(
                VOICE_PREVIEW_URL.format(voice_id=voice["id"]),
                headers={"Authorization": f"Bearer {key}"},
                params={"sample": "default"},
                timeout=(15, 90),
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Không tải được bản nghe thử "
                    f"(HTTP {response.status_code}): {response.text[:400]}"
                )
            content_type = response.headers.get("content-type", "").lower()
            if "json" in content_type or len(response.content) <= 44:
                raise RuntimeError("Blaze không trả về dữ liệu audio nghe thử.")

            with tempfile.NamedTemporaryFile(
                prefix="blaze_voice_preview_", suffix=".mp3", delete=False
            ) as preview_file:
                mp3_path = Path(preview_file.name)
                preview_file.write(response.content)
            with tempfile.NamedTemporaryFile(
                prefix="blaze_voice_preview_", suffix=".wav", delete=False
            ) as preview_file:
                wav_path = Path(preview_file.name)
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(mp3_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    str(wav_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Không chuyển đổi được audio nghe thử: "
                    f"{result.stderr[-600:].strip() or result.returncode}"
                )
            self.messages.put(("preview_ready", (voice, wav_path)))
            wav_path = None
        except (OSError, requests.RequestException, RuntimeError) as exc:
            self.messages.put(("preview_error", str(exc)))
        finally:
            if mp3_path:
                mp3_path.unlink(missing_ok=True)
            if wav_path:
                wav_path.unlink(missing_ok=True)

    def open_env_file(self) -> None:
        if not ENV_PATH.exists():
            messagebox.showerror(
                "Không tìm thấy file .env",
                f"Tạo file cấu hình tại:\n{ENV_PATH}",
                parent=self.root,
            )
            return
        try:
            os.startfile(ENV_PATH)
        except OSError as exc:
            messagebox.showerror(
                "Không mở được file .env",
                str(exc),
                parent=self.root,
            )

    def start_generation(self, srt_path: Path) -> None:
        if not srt_path.is_file():
            messagebox.showerror("Không tìm thấy file", "File SRT đã chọn không tồn tại.")
            return
        if srt_path.suffix.lower() not in {".srt", ".txt"}:
            messagebox.showerror(
                "Sai định dạng",
                "Vui lòng chọn file .srt hoặc file .txt có timestamp.",
            )
            return

        key = self.api_key()
        if not key:
            self.status.set("Hãy thêm API key vào file .env.")
            messagebox.showerror(
                "Chưa cấu hình Blaze API key",
                "Mở file .env trong thư mục SRT Voice Generator và thêm dòng:\n\n"
                "BLAZE_API_KEY=your_api_key_here\n\n"
                "Lưu file rồi bấm tạo voice lại.",
                parent=self.root,
            )
            return

        output_path = srt_path.with_name(f"{srt_path.stem}_voice.wav")
        if output_path.exists() and not messagebox.askyesno(
            "Ghi đè file?",
            f"File này đã tồn tại:\n{output_path}\n\nBạn có muốn tạo lại và ghi đè không?",
        ):
            return

        ffmpeg = str(FFMPEG_PATH) if FFMPEG_PATH.is_file() else shutil.which("ffmpeg")
        if not ffmpeg:
            messagebox.showerror(
                "Thiếu FFmpeg",
                "Không tìm thấy FFmpeg đi kèm hoặc đã cài trên máy.",
            )
            return
        if not PYTHON_PATH.is_file():
            messagebox.showerror(
                "Chưa cài môi trường",
                "Không tìm thấy Python môi trường riêng. Hãy đóng ứng dụng và chạy "
                "“Cài đặt và mở.bat” trước.",
            )
            return

        command = [
            str(PYTHON_PATH),
            str(SCRIPT_PATH),
            str(srt_path),
            "--output",
            str(output_path),
            "--model",
            "blaze-tts-3",
            "--speaker-id",
            self.selected_voice()["id"] if self.selected_voice() else "HN-Nam-2-TM",
            "--workers",
            "10",
            "--ffmpeg",
            ffmpeg,
        ]
        self.choose_button.configure(state=tk.DISABLED)
        self.status.set("Đang tạo voice...")
        self.append_log(f"Bắt đầu: {srt_path.name}")
        threading.Thread(
            target=self.run_generator,
            args=(command, key, output_path),
            daemon=True,
        ).start()

    def run_generator(
        self, command: list[str], key: str, output_path: Path
    ) -> None:
        started = time.monotonic()
        environment = os.environ.copy()
        environment["BLAZE_API_KEY"] = key
        try:
            process = subprocess.Popen(
                command,
                cwd=APP_DIR,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self.messages.put(("log", line.rstrip()))
            return_code = process.wait()
            elapsed = time.monotonic() - started
            if return_code == 0:
                self.messages.put(("success", (str(output_path), elapsed)))
            else:
                self.messages.put(
                    (
                        "failure",
                        (f"Tiến trình kết thúc với mã {return_code}.", elapsed),
                    )
                )
        except OSError as exc:
            self.messages.put(("failure", (f"Không chạy được tiến trình tạo voice: {exc}", 0.0)))

    def read_messages(self) -> None:
        try:
            while True:
                kind, message = self.messages.get_nowait()
                if kind == "log":
                    self.append_log(str(message))
                elif kind == "voices_loaded":
                    voices = message
                    assert isinstance(voices, list)
                    self.voices = {
                        self.voice_label(voice): voice for voice in voices
                    }
                    self.voice_combo.configure(
                        values=list(self.voices),
                        state="readonly",
                    )
                    default_label = next(
                        (
                            label
                            for label, voice in self.voices.items()
                            if voice["id"] == "HN-Nam-2-TM"
                        ),
                        next(iter(self.voices)),
                    )
                    self.voice_choice.set(default_label)
                    self.preview_button.configure(state=tk.NORMAL)
                    self.status.set(f"Đã tải {len(self.voices)} voice từ Blaze.")
                    self.append_log(f"Đã tải danh sách {len(self.voices)} voice.")
                elif kind == "voices_error":
                    self.voice_choice.set("Không tải được danh sách voice")
                    self.status.set("Không tải được voice; kiểm tra API key/kết nối.")
                    messagebox.showerror(
                        "Không tải được danh sách voice",
                        str(message),
                        parent=self.root,
                    )
                elif kind == "preview_ready":
                    voice, preview_path = message
                    assert isinstance(voice, dict) and isinstance(preview_path, Path)
                    self.stop_preview()
                    self.preview_path = preview_path
                    try:
                        winsound.PlaySound(
                            str(preview_path),
                            winsound.SND_FILENAME | winsound.SND_ASYNC,
                        )
                    except RuntimeError as exc:
                        self.stop_preview()
                        self.status.set("Không phát được audio nghe thử.")
                        messagebox.showerror(
                            "Lỗi phát audio",
                            str(exc),
                            parent=self.root,
                        )
                    else:
                        self.status.set(f"Đang nghe thử voice {voice['name']}.")
                        self.append_log(f"Đang nghe thử: {voice['name']} [{voice['id']}]")
                    self.preview_button.configure(state=tk.NORMAL)
                elif kind == "preview_error":
                    self.preview_button.configure(state=tk.NORMAL)
                    self.status.set("Không tải được audio nghe thử.")
                    messagebox.showerror(
                        "Không nghe thử được voice",
                        str(message),
                        parent=self.root,
                    )
                elif kind == "success":
                    self.choose_button.configure(state=tk.NORMAL)
                    self.status.set("Đã tạo voice thành công.")
                    output_path, elapsed = message
                    assert isinstance(output_path, str) and isinstance(elapsed, float)
                    self.append_log(
                        f"Hoàn tất sau {self.format_elapsed(elapsed)}: {output_path}"
                    )
                    messagebox.showinfo(
                        "Hoàn tất",
                        f"Đã tạo file audio:\n{output_path}\n\n"
                        f"Thời gian: {self.format_elapsed(elapsed)}",
                        parent=self.root,
                    )
                else:
                    self.choose_button.configure(state=tk.NORMAL)
                    self.status.set("Tạo voice không thành công.")
                    error, elapsed = message
                    assert isinstance(error, str) and isinstance(elapsed, float)
                    self.append_log(
                        f"Lỗi sau {self.format_elapsed(elapsed)}: {error}"
                    )
                    messagebox.showerror("Không tạo được voice", error, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.read_messages)

    @staticmethod
    def format_elapsed(seconds: float) -> str:
        total_seconds = round(seconds)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes} phút {seconds} giây" if minutes else f"{seconds} giây"

    def stop_preview(self) -> None:
        winsound.PlaySound(None, winsound.SND_PURGE)
        if self.preview_path:
            self.preview_path.unlink(missing_ok=True)
            self.preview_path = None

    def close(self) -> None:
        self.stop_preview()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    VoiceGeneratorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
