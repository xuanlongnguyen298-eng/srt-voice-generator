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

from srt_tts_timeline import cue_duration_ms, parse_srt


APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = APP_DIR / "srt_tts_timeline.py"
ENV_PATH = APP_DIR / ".env"
FFMPEG_PATH = APP_DIR / "ffmpeg" / "ffmpeg.exe"
PYTHON_PATH = APP_DIR / ".venv" / "Scripts" / "python.exe"
VOICE_CATALOG_URL = "https://api.blaze.vn/v1/tts/list-speaker-ids"
VOICE_PREVIEW_URL = "https://api.blaze.vn/v1/tts/voices/{voice_id}/preview"
TTS_MODEL = "v2.0_pro"
DEFAULT_VOICE_ID = "HN-Nam-2-TM"


class VoiceGeneratorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Tạo voice từ file SRT - Blaze")
        self.root.geometry("960x720")
        self.root.minsize(760, 540)
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.voices: dict[str, dict[str, str]] = {}
        self.preview_path: Path | None = None
        self.queue_paths: list[Path] = []
        self.queue_voice_ids: dict[str, str] = {}
        self.selected_queue_indices: tuple[int, ...] = ()
        self.active_voice_id: str | None = None
        self._refreshing_queue = False
        self.is_generating = False

        container = ttk.Frame(root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(2, weight=1)
        container.rowconfigure(3, weight=1)
        container.rowconfigure(5, weight=1)

        ttk.Label(
            container,
            text="Tạo file voice từ phụ đề SRT",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(
            container,
            text="Model v2.0 Pro · Voice của model này, gồm BTV và Blaze Note · Tự căn tốc độ theo timestamp",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 14))

        ttk.Label(container, text="Hàng đợi file phụ đề (chạy theo thứ tự bên dưới):").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(0, 6)
        )
        queue_frame = ttk.Frame(container)
        queue_frame.grid(row=3, column=0, columnspan=4, sticky="nsew")
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)
        self.queue_listbox = tk.Listbox(
            queue_frame,
            height=8,
            exportselection=False,
            selectmode=tk.EXTENDED,
            activestyle="dotbox",
        )
        self.queue_listbox.grid(row=0, column=0, sticky="nsew")
        queue_scrollbar = ttk.Scrollbar(
            queue_frame, orient=tk.VERTICAL, command=self.queue_listbox.yview
        )
        queue_scrollbar.grid(row=0, column=1, sticky="ns")
        queue_horizontal_scrollbar = ttk.Scrollbar(
            queue_frame, orient=tk.HORIZONTAL, command=self.queue_listbox.xview
        )
        queue_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.queue_listbox.configure(
            xscrollcommand=queue_horizontal_scrollbar.set,
            yscrollcommand=queue_scrollbar.set,
        )
        self.queue_listbox.bind("<<ListboxSelect>>", self.on_queue_selection_changed)

        queue_actions = ttk.Frame(queue_frame)
        queue_actions.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(8, 0))
        self.add_files_button = ttk.Button(
            queue_actions, text="Thêm file", command=self.add_files
        )
        self.add_files_button.pack(fill=tk.X, pady=(0, 6))
        self.assign_voice_button = ttk.Button(
            queue_actions,
            text="Gán voice",
            command=self.assign_voice_to_selected_files,
            state=tk.DISABLED,
        )
        self.assign_voice_button.pack(fill=tk.X, pady=3)
        self.move_up_button = ttk.Button(
            queue_actions, text="Lên", command=lambda: self.move_selected_file(-1)
        )
        self.move_up_button.pack(fill=tk.X, pady=3)
        self.move_down_button = ttk.Button(
            queue_actions, text="Xuống", command=lambda: self.move_selected_file(1)
        )
        self.move_down_button.pack(fill=tk.X, pady=3)
        self.remove_file_button = ttk.Button(
            queue_actions, text="Xóa file", command=self.remove_selected_file
        )
        self.remove_file_button.pack(fill=tk.X, pady=(6, 3))
        self.clear_queue_button = ttk.Button(
            queue_actions, text="Xóa tất cả", command=self.clear_queue
        )
        self.clear_queue_button.pack(fill=tk.X, pady=3)
        self.queue_buttons = (
            self.add_files_button,
            self.assign_voice_button,
            self.move_up_button,
            self.move_down_button,
            self.remove_file_button,
            self.clear_queue_button,
        )

        ttk.Label(container, text="Voice cho file đang chọn:").grid(
            row=4, column=0, sticky="w", padx=(0, 8)
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
        self.voice_search_entry.grid(row=4, column=1, sticky="ew", padx=(0, 8))
        self.voice_combo = ttk.Combobox(
            container,
            textvariable=self.voice_choice,
            state="disabled",
            width=42,
        )
        self.voice_combo.grid(row=4, column=2, sticky="ew")
        self.voice_combo.bind("<<ComboboxSelected>>", self.on_voice_selected)
        self.preview_button = ttk.Button(
            container,
            text="Nghe thử",
            command=self.preview_selected_voice,
            state=tk.DISABLED,
        )
        self.preview_button.grid(row=4, column=3, sticky="e", padx=(8, 0))

        self.log = scrolledtext.ScrolledText(
            container, height=14, state=tk.DISABLED, wrap=tk.WORD
        )
        self.log.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(16, 8))

        progress_row = ttk.Frame(container)
        progress_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        progress_row.columnconfigure(0, weight=1)
        ttk.Label(progress_row, text="Tiến độ cả phiên").grid(row=0, column=0, sticky="w")
        self.progress_text = tk.StringVar(value="Chưa chạy")
        ttk.Label(progress_row, textvariable=self.progress_text).grid(
            row=0, column=1, sticky="e"
        )
        self.progress = ttk.Progressbar(
            progress_row,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100,
        )
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        controls = ttk.Frame(container)
        controls.grid(row=7, column=0, columnspan=4, sticky="ew")
        controls.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Thêm file SRT/TXT rồi sắp xếp thứ tự chạy.")
        ttk.Label(controls, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.key_button = ttk.Button(
            controls, text="Mở file .env", command=self.open_env_file
        )
        self.key_button.grid(row=0, column=1, padx=(8, 8))
        self.choose_button = ttk.Button(
            controls,
            text="Chạy hàng đợi",
            command=self.start_queue,
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

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn một hoặc nhiều file phụ đề có timestamp",
            filetypes=[
                ("SRT hoặc TXT có timestamp", "*.srt *.txt"),
                ("SRT subtitles", "*.srt"),
                ("Text files", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        existing_paths = {self.queue_key(path) for path in self.queue_paths}
        selected_voice = self.voice_from_combo()
        if not selected_voice:
            messagebox.showerror(
                "Chưa có voice của Blaze TTS 3",
                "Hãy đợi danh sách voice của v2.0_pro tải xong rồi thêm file.",
                parent=self.root,
            )
            return
        default_voice_id = selected_voice["id"]
        self.active_voice_id = default_voice_id
        added = 0
        for selected_path in paths:
            path = Path(selected_path)
            normalized_path = self.queue_key(path)
            if normalized_path in existing_paths:
                continue
            self.queue_paths.append(path)
            self.queue_voice_ids[normalized_path] = default_voice_id
            existing_paths.add(normalized_path)
            added += 1
        self.refresh_queue_list()
        if added:
            self.status.set(f"Đã thêm {added} file vào hàng đợi.")

    def refresh_queue_list(self) -> None:
        selected_indices = self.current_queue_selection()
        self._refreshing_queue = True
        try:
            self.queue_listbox.delete(0, tk.END)
            for index, path in enumerate(self.queue_paths, start=1):
                voice_id = self.queue_voice_ids.get(self.queue_key(path), "")
                voice_label = self.voice_name_for_id(voice_id)
                self.queue_listbox.insert(
                    tk.END, f"{index}. {path.name} — voice: {voice_label}"
                )
            restored: list[int] = []
            for index in selected_indices:
                if index < len(self.queue_paths):
                    self.queue_listbox.selection_set(index)
                    restored.append(index)
        finally:
            self._refreshing_queue = False
        self.selected_queue_indices = tuple(restored)

    def on_queue_selection_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._refreshing_queue:
            return
        self.selected_queue_indices = tuple(self.queue_listbox.curselection())

    def current_queue_selection(self) -> tuple[int, ...]:
        if not self._refreshing_queue:
            live_selection = tuple(self.queue_listbox.curselection())
            if live_selection:
                self.selected_queue_indices = live_selection
                return live_selection
        return tuple(
            index for index in self.selected_queue_indices if index < len(self.queue_paths)
        )

    @staticmethod
    def queue_key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))

    def voice_name_for_id(self, voice_id: str) -> str:
        voice = next(
            (voice for voice in self.voices.values() if voice["id"] == voice_id),
            None,
        )
        if voice:
            return f"{voice['name']} [{voice_id}]"
        return voice_id or "chưa gán"

    def replace_unknown_queue_voices(self, known_ids: set[str]) -> int:
        replacement = self.active_voice_id
        if not replacement or replacement not in known_ids:
            return 0
        replaced = 0
        for path in self.queue_paths:
            key = self.queue_key(path)
            current = self.queue_voice_ids.get(key)
            if current not in known_ids:
                self.queue_voice_ids[key] = replacement
                replaced += 1
        return replaced

    def assign_voice_to_selected_files(self) -> None:
        selected_indices = self.current_queue_selection()
        voice = self.voice_from_combo()
        if not selected_indices:
            messagebox.showerror(
                "Chưa chọn file",
                "Hãy chọn một hoặc nhiều file trong hàng đợi để gán voice.",
                parent=self.root,
            )
            return
        if not voice:
            messagebox.showerror(
                "Chưa chọn voice",
                "Hãy chọn voice trong danh sách trước khi gán cho file.",
                parent=self.root,
            )
            return
        self.apply_voice_to_indices(selected_indices, voice)

    def apply_voice_to_indices(
        self, selected_indices: tuple[int, ...], voice: dict[str, str]
    ) -> None:
        for index in selected_indices:
            self.queue_voice_ids[self.queue_key(self.queue_paths[index])] = voice["id"]
        self.active_voice_id = voice["id"]
        self.refresh_queue_list()
        self.status.set(
            f"Đã gán voice {voice['name']} cho {len(selected_indices)} file."
        )
        self.append_log(
            f"Đã gán voice {voice['name']} [{voice['id']}] "
            f"cho {len(selected_indices)} file trong hàng đợi."
        )

    def move_selected_file(self, direction: int) -> None:
        selected = self.queue_listbox.curselection()
        if not selected:
            return
        current_index = selected[0]
        target_index = current_index + direction
        if not 0 <= target_index < len(self.queue_paths):
            return
        self.queue_paths[current_index], self.queue_paths[target_index] = (
            self.queue_paths[target_index],
            self.queue_paths[current_index],
        )
        self.refresh_queue_list()
        self.queue_listbox.selection_clear(0, tk.END)
        self.queue_listbox.selection_set(target_index)
        self.queue_listbox.activate(target_index)
        self.selected_queue_indices = (target_index,)

    def remove_selected_file(self) -> None:
        selected = self.queue_listbox.curselection()
        if not selected:
            return
        for index in reversed(selected):
            path = self.queue_paths.pop(index)
            self.queue_voice_ids.pop(self.queue_key(path), None)
        self.refresh_queue_list()

    def clear_queue(self) -> None:
        self.queue_paths.clear()
        self.queue_voice_ids.clear()
        self.refresh_queue_list()

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
            entries = self.catalog_entries(response.json())
            if entries is None:
                raise RuntimeError("Blaze trả về danh sách voice không đúng định dạng.")

            voices: list[dict[str, str]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                voice = self.normalize_catalog_voice(entry)
                if voice:
                    voices.append(voice)
            if not voices:
                raise RuntimeError("Blaze không trả về voice nào của v2.0_pro.")
            voices.sort(
                key=lambda voice: (
                    not self.is_vietnamese_voice(voice),
                    voice["name"].casefold(),
                    voice["id"].casefold(),
                )
            )
            self.messages.put(("voices_loaded", voices))
        except (requests.RequestException, requests.JSONDecodeError, RuntimeError) as exc:
            self.messages.put(("voices_error", str(exc)))

    @staticmethod
    def catalog_entries(payload: object) -> list[object] | None:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("voices", "list_speakers"):
                entries = payload.get(key)
                if isinstance(entries, list):
                    return entries
        return None

    @staticmethod
    def normalize_catalog_voice(entry: dict[str, object]) -> dict[str, str] | None:
        raw_id = entry.get("voice_id") or entry.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            return None
        language = entry.get("language")
        if not isinstance(language, str) or not language.strip():
            languages = entry.get("languages")
            if isinstance(languages, list):
                language = ", ".join(
                    str(item).strip() for item in languages if str(item).strip()
                )
            else:
                language = ""
        return {
            "id": raw_id.strip(),
            "name": str(entry.get("name") or raw_id).strip(),
            "gender": str(entry.get("gender") or "").strip(),
            "language": language.strip(),
            "province": str(entry.get("province") or "").strip(),
            "type": str(entry.get("type") or "").strip(),
        }

    @staticmethod
    def is_vietnamese_voice(voice: dict[str, str]) -> bool:
        tokens = {part.strip().casefold() for part in voice["language"].split(",")}
        return "vi" in tokens

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
        return self.voice_from_combo()

    def voice_from_combo(self) -> dict[str, str] | None:
        label = self.voice_combo.get().strip()
        voice = self.voices.get(label)
        if voice:
            return voice
        if label.endswith("]") and "[" in label:
            voice_id = label[label.rfind("[") + 1 : -1].strip()
            voice = next(
                (item for item in self.voices.values() if item["id"] == voice_id),
                None,
            )
            if voice:
                return voice
        if self.active_voice_id:
            return next(
                (item for item in self.voices.values() if item["id"] == self.active_voice_id),
                None,
            )
        return None

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

    def on_voice_selected(self, event: tk.Event[tk.Misc] | None = None) -> None:
        # The StringVar can still hold the previous voice while this event runs.
        # Keep the widget value from the event, then apply it once Tk is idle.
        captured = event.widget.get() if event is not None else self.voice_combo.get()

        def apply_captured() -> None:
            current = self.voice_combo.get().strip()
            label = current if current in self.voices else captured
            self.apply_voice_chosen_in_dropdown(label)

        self.root.after_idle(apply_captured)

    def apply_voice_chosen_in_dropdown(self, label: str | None = None) -> None:
        voice = self.voices.get(label or "") or self.voice_from_combo()
        if not voice or self.is_generating:
            return
        self.active_voice_id = voice["id"]
        selected_indices = self.current_queue_selection()
        if not selected_indices:
            self.status.set(
                f"Đã chọn voice {voice['name']}. "
                "Hãy chọn file trong hàng đợi để gán voice này."
            )
            return
        self.apply_voice_to_indices(selected_indices, voice)

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
            suffix = ".wav" if "wav" in content_type or response.content.startswith(b"RIFF") else ".mp3"

            with tempfile.NamedTemporaryFile(
                prefix="blaze_voice_preview_", suffix=suffix, delete=False
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

    def start_queue(self) -> None:
        if self.is_generating:
            return
        if not self.queue_paths:
            messagebox.showerror(
                "Hàng đợi đang trống",
                "Hãy thêm ít nhất một file SRT hoặc TXT có timestamp.",
                parent=self.root,
            )
            return

        invalid_paths = [
            path
            for path in self.queue_paths
            if not path.is_file() or path.suffix.lower() not in {".srt", ".txt"}
        ]
        if invalid_paths:
            messagebox.showerror(
                "File trong hàng đợi không hợp lệ",
                "Hãy kiểm tra file không tồn tại hoặc không phải SRT/TXT:\n\n"
                + "\n".join(str(path) for path in invalid_paths[:8]),
                parent=self.root,
            )
            return
        if not self.voices:
            messagebox.showerror(
                "Chưa có danh sách voice",
                "Ứng dụng chưa tải được danh sách voice. Hãy kiểm tra kết nối/API key "
                "rồi mở lại ứng dụng trước khi chạy hàng đợi.",
                parent=self.root,
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

        output_paths = [
            path.with_name(f"{path.stem}_voice.wav") for path in self.queue_paths
        ]
        normalized_outputs = [
            os.path.normcase(str(path.resolve())) for path in output_paths
        ]
        if len(set(normalized_outputs)) != len(normalized_outputs):
            messagebox.showerror(
                "Trùng file đầu ra",
                "Có nhiều file phụ đề tạo ra cùng một tên WAV. Hãy bỏ bớt file "
                "hoặc đổi tên chúng trước khi chạy hàng đợi.",
                parent=self.root,
            )
            return

        existing_outputs = [
            output_path for output_path in output_paths if output_path.exists()
        ]
        overwrite_existing = True
        if existing_outputs:
            displayed_outputs = "\n".join(str(path) for path in existing_outputs[:8])
            if len(existing_outputs) > 8:
                displayed_outputs += f"\n... và {len(existing_outputs) - 8} file khác"
            overwrite_choice = messagebox.askyesnocancel(
                "WAV đầu ra đã tồn tại",
                f"Có {len(existing_outputs)} file WAV đã tồn tại:\n\n"
                f"{displayed_outputs}\n\n"
                "Chọn Có để ghi đè tất cả, Không để bỏ qua các file này, "
                "hoặc Hủy để dừng.",
                parent=self.root,
            )
            if overwrite_choice is None:
                return
            overwrite_existing = overwrite_choice

        known_voice_ids = {voice["id"] for voice in self.voices.values()}
        missing_voice_files = [
            path.name
            for path in self.queue_paths
            if self.queue_voice_ids.get(self.queue_key(path)) not in known_voice_ids
        ]
        if missing_voice_files:
            preview = "\n".join(missing_voice_files[:8])
            messagebox.showerror(
                "Voice không thuộc Blaze TTS 3",
                "Mỗi file cần một voice trong danh sách v2.0_pro.\n\n"
                f"{preview}",
                parent=self.root,
            )
            return
        queue_items = tuple(
            (
                path,
                self.queue_voice_ids[self.queue_key(path)],
            )
            for path in self.queue_paths
        )
        self.is_generating = True
        self.choose_button.configure(state=tk.DISABLED)
        for button in self.queue_buttons:
            button.configure(state=tk.DISABLED)
        self.voice_combo.configure(state=tk.DISABLED)
        self.voice_search_entry.configure(state=tk.DISABLED)
        self.preview_button.configure(state=tk.DISABLED)
        self.status.set(f"Đang chạy hàng đợi: {len(queue_items)} file...")
        self.append_log(f"Bắt đầu hàng đợi gồm {len(queue_items)} file.")
        self.update_session_progress(0, 1)
        self.progress_text.set("Đang tính thời lượng phụ đề...")
        threading.Thread(
            target=self.run_queue,
            args=(queue_items, key, ffmpeg, overwrite_existing),
            daemon=True,
        ).start()

    def run_queue(
        self,
        queue_items: tuple[tuple[Path, str], ...],
        key: str,
        ffmpeg: str,
        overwrite_existing: bool,
    ) -> None:
        started = time.monotonic()
        success_count = 0
        skipped_count = 0
        failure_count = 0
        total = len(queue_items)
        file_weights = [self.subtitle_weight_ms(path) for path, _voice in queue_items]
        session_total = sum(file_weights) or 1
        session_done = 0
        self.messages.put(("session_progress", (session_done, session_total)))
        for index, (srt_path, voice_id) in enumerate(queue_items, start=1):
            file_total = file_weights[index - 1]
            file_done = 0
            output_path = srt_path.with_name(f"{srt_path.stem}_voice.wav")
            if output_path.exists() and not overwrite_existing:
                skipped_count += 1
                session_done += file_total
                self.messages.put(("session_progress", (session_done, session_total)))
                self.messages.put(
                    ("queue_item_skipped", (index, total, str(output_path)))
                )
                continue

            self.messages.put(
                ("queue_item_start", (index, total, srt_path.name, voice_id))
            )
            command = [
                str(PYTHON_PATH),
                str(SCRIPT_PATH),
                str(srt_path),
                "--output",
                str(output_path),
                "--model",
                TTS_MODEL,
                "--speaker-id",
                voice_id,
                "--workers",
                "10",
                "--ffmpeg",
                ffmpeg,
            ]
            item_started = time.monotonic()
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
                finished_cues: set[int] = set()
                for line in process.stdout:
                    raw_line = line.rstrip()
                    cue_progress = self.parse_progress_line(raw_line)
                    if cue_progress is not None:
                        cue_index, cue_ms = cue_progress
                        if cue_index not in finished_cues:
                            finished_cues.add(cue_index)
                            room = file_total - file_done
                            if room > 0:
                                added = min(max(cue_ms, 0), room)
                                file_done += added
                                session_done += added
                                self.messages.put(
                                    ("session_progress", (session_done, session_total))
                                )
                        continue
                    self.messages.put(("log", f"[{index}/{total}] {raw_line}"))
                return_code = process.wait()
                elapsed = time.monotonic() - item_started
                if return_code == 0:
                    success_count += 1
                    remainder = file_total - file_done
                    if remainder > 0:
                        file_done += remainder
                        session_done += remainder
                        self.messages.put(
                            ("session_progress", (session_done, session_total))
                        )
                    self.messages.put(
                        (
                            "queue_item_success",
                            (index, total, str(output_path), elapsed),
                        )
                    )
                else:
                    error = f"Tiến trình kết thúc với mã {return_code}."
                    self.queue_failure(index, total, srt_path, error, elapsed)
                    failure_count += 1
            except OSError as exc:
                elapsed = time.monotonic() - item_started
                self.queue_failure(
                    index,
                    total,
                    srt_path,
                    f"Không chạy được tiến trình tạo voice: {exc}",
                    elapsed,
                )
                failure_count += 1
        self.messages.put(
            (
                "queue_finished",
                (
                    success_count,
                    skipped_count,
                    failure_count,
                    time.monotonic() - started,
                ),
            )
        )

    def queue_failure(
        self,
        index: int,
        total: int,
        srt_path: Path,
        error: str,
        elapsed: float,
    ) -> None:
        self.messages.put(
            (
                "queue_item_failure",
                (index, total, srt_path.name, error, elapsed),
            )
        )

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
                        state=tk.DISABLED if self.is_generating else "readonly",
                    )
                    known_labels = {
                        voice["id"]: label for label, voice in self.voices.items()
                    }
                    preferred_id = self.active_voice_id
                    if preferred_id not in known_labels:
                        preferred_id = (
                            DEFAULT_VOICE_ID
                            if DEFAULT_VOICE_ID in known_labels
                            else next(iter(self.voices.values()))["id"]
                        )
                    default_label = known_labels[preferred_id]
                    if self.voice_choice.get() not in self.voices:
                        self.voice_choice.set(default_label)
                    self.active_voice_id = self.voices[self.voice_choice.get()]["id"]
                    replaced = self.replace_unknown_queue_voices(set(known_labels))
                    self.refresh_queue_list()
                    if replaced:
                        self.append_log(
                            "Đã đổi voice không thuộc v2.0_pro sang "
                            f"{self.voice_name_for_id(self.active_voice_id)}."
                        )
                    self.assign_voice_button.configure(
                        state=tk.DISABLED if self.is_generating else tk.NORMAL
                    )
                    self.preview_button.configure(
                        state=tk.DISABLED if self.is_generating else tk.NORMAL
                    )
                    if not self.is_generating:
                        self.status.set(
                            f"Đã tải {len(self.voices)} voice của v2.0_pro."
                        )
                    self.append_log(
                        f"Đã tải {len(self.voices)} voice của v2.0_pro."
                    )
                elif kind == "voices_error":
                    self.voice_choice.set("Không tải được danh sách voice")
                    if self.is_generating:
                        self.append_log(
                            f"Không tải được danh sách voice: {message}"
                        )
                    else:
                        self.status.set(
                            "Không tải được voice; kiểm tra API key/kết nối."
                        )
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
                elif kind == "session_progress":
                    done_ms, total_ms = message
                    assert isinstance(done_ms, int) and isinstance(total_ms, int)
                    self.update_session_progress(done_ms, total_ms)
                elif kind == "queue_item_start":
                    index, total, file_name, voice_id = message
                    assert isinstance(index, int) and isinstance(total, int)
                    assert isinstance(file_name, str) and isinstance(voice_id, str)
                    self.status.set(f"Đang tạo file {index}/{total}: {file_name}")
                    self.append_log(
                        f"[{index}/{total}] Bắt đầu: {file_name} "
                        f"(voice: {self.voice_name_for_id(voice_id)})"
                    )
                elif kind == "queue_item_skipped":
                    index, total, output_path = message
                    assert isinstance(index, int) and isinstance(total, int)
                    assert isinstance(output_path, str)
                    self.append_log(
                        f"[{index}/{total}] Bỏ qua vì WAV đã tồn tại: {output_path}"
                    )
                elif kind == "queue_item_success":
                    index, total, output_path, elapsed = message
                    assert isinstance(index, int) and isinstance(total, int)
                    assert isinstance(output_path, str) and isinstance(elapsed, float)
                    self.append_log(
                        f"[{index}/{total}] Hoàn tất sau "
                        f"{self.format_elapsed(elapsed)}: {output_path}"
                    )
                elif kind == "queue_item_failure":
                    index, total, file_name, error, elapsed = message
                    assert isinstance(index, int) and isinstance(total, int)
                    assert isinstance(file_name, str) and isinstance(error, str)
                    assert isinstance(elapsed, float)
                    self.append_log(
                        f"[{index}/{total}] Lỗi sau {self.format_elapsed(elapsed)} "
                        f"({file_name}): {error}"
                    )
                elif kind == "queue_finished":
                    success_count, skipped_count, failure_count, elapsed = message
                    assert isinstance(success_count, int) and isinstance(skipped_count, int)
                    assert isinstance(failure_count, int) and isinstance(elapsed, float)
                    self.is_generating = False
                    self.choose_button.configure(state=tk.NORMAL)
                    for button in self.queue_buttons:
                        button.configure(state=tk.NORMAL)
                    self.assign_voice_button.configure(
                        state=tk.NORMAL if self.voices else tk.DISABLED
                    )
                    self.voice_combo.configure(
                        state="readonly" if self.voices else tk.DISABLED
                    )
                    self.voice_search_entry.configure(state=tk.NORMAL)
                    self.preview_button.configure(
                        state=tk.NORMAL if self.voices else tk.DISABLED
                    )
                    summary = (
                        f"Hàng đợi xong: {success_count} thành công, "
                        f"{failure_count} lỗi, {skipped_count} bỏ qua. "
                        f"Thời gian: {self.format_elapsed(elapsed)}."
                    )
                    self.status.set(
                        "Hàng đợi hoàn tất có lỗi."
                        if failure_count
                        else "Đã hoàn tất hàng đợi."
                    )
                    self.append_log(summary)
        except queue.Empty:
            pass
        self.root.after(100, self.read_messages)

    @staticmethod
    def subtitle_weight_ms(path: Path) -> int:
        try:
            cues = parse_srt(path)
        except (OSError, ValueError):
            return 1
        return sum(cue_duration_ms(cue) for cue in cues) or 1

    @staticmethod
    def parse_progress_line(line: str) -> tuple[int, int] | None:
        parts = line.split()
        if len(parts) != 3 or parts[0] != "PROGRESS":
            return None
        try:
            cue_index = int(parts[1])
            duration_ms = int(parts[2])
        except ValueError:
            return None
        if cue_index < 1 or duration_ms < 0:
            return None
        return cue_index, duration_ms

    def update_session_progress(self, done_ms: int, total_ms: int) -> None:
        total_ms = max(total_ms, 1)
        done_ms = min(max(done_ms, 0), total_ms)
        percent = done_ms / total_ms * 100
        self.progress["value"] = percent
        self.progress_text.set(
            f"{percent:.0f}% · {self.format_clock(done_ms)} / "
            f"{self.format_clock(total_ms)} thời lượng phụ đề"
        )

    @staticmethod
    def format_clock(duration_ms: int) -> str:
        total_seconds = max(duration_ms, 0) // 1000
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

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
        if self.is_generating and not messagebox.askyesno(
            "Hàng đợi đang chạy",
            "Nếu đóng ứng dụng lúc này, các file còn lại trong hàng đợi sẽ không "
            "được chạy. Bạn vẫn muốn đóng?",
            parent=self.root,
        ):
            return
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
