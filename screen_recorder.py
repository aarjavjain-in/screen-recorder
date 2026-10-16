import ctypes
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from ctypes import wintypes


FRAME_RATE = 30


def list_windows():
    if not hasattr(ctypes, "windll"):
        return []

    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows.argtypes = [callback_type, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    windows = []

    def collect_window(hwnd, _parameter):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value and title.value != "Screen Recorder":
            windows.append((title.value, hwnd))
        return True

    callback = callback_type(collect_window)
    if not enum_windows(callback, 0):
        raise RuntimeError("Windows could not list the open windows.")
    return sorted(windows, key=lambda item: item[0].casefold())


class WindowCapture:
    def __init__(self, hwnd):
        self.hwnd = int(hwnd)
        self.frames = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.control = None
        self.capture = None
        self.last_frame = None

    def __enter__(self):
        try:
            from windows_capture import WindowsCapture
        except ImportError as error:
            raise RuntimeError(
                "Window recording needs windows-capture. Install the project requirements."
            ) from error

        self.capture = WindowsCapture(
            cursor_capture=True,
            draw_border=False,
            window_hwnd=self.hwnd,
        )

        def on_frame_arrived(frame, _capture_control):
            snapshot = (
                frame.frame_buffer.tobytes(),
                frame.width,
                frame.height,
            )
            try:
                self.frames.put_nowait(snapshot)
            except queue.Full:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put_nowait(snapshot)

        def on_closed():
            self.closed.set()

        self.capture.event(on_frame_arrived)
        self.capture.event(on_closed)
        self.control = self.capture.start_free_threaded()
        try:
            self.last_frame = self.frames.get(timeout=10)
        except queue.Empty as error:
            self.close_capture()
            raise RuntimeError(
                "Windows did not provide a frame for that window. Check that it is still open."
            ) from error

        _pixels, self.width, self.height = self.last_frame
        if self.width <= 0 or self.height <= 0:
            self.close_capture()
            raise RuntimeError("The selected window has no capturable content.")
        return self

    def grab(self):
        if self.closed.is_set() and self.frames.empty():
            raise RuntimeError("The selected window was closed during recording.")
        try:
            self.last_frame = self.frames.get(timeout=1 / FRAME_RATE)
        except queue.Empty:
            pass

        pixels, width, height = self.last_frame
        if (width, height) != (self.width, self.height):
            import cv2
            import numpy

            image = numpy.frombuffer(pixels, dtype=numpy.uint8).reshape(height, width, 4)
            pixels = cv2.resize(image, (self.width, self.height)).tobytes()
        return pixels

    def close_capture(self):
        if self.control is not None:
            self.control.stop()
            self.control.wait()
            self.control = None

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close_capture()


class ScreenCapture:
    def __enter__(self):
        import mss

        self.capture = mss.MSS()
        self.capture.__enter__()
        self.monitor = self.capture.monitors[0]
        self.width = self.monitor["width"]
        self.height = self.monitor["height"]
        return self

    def grab(self):
        frame = self.capture.grab(self.monitor)
        return frame.raw, self.monitor["left"], self.monitor["top"]

    def __exit__(self, exception_type, exception, traceback):
        return self.capture.__exit__(exception_type, exception, traceback)


def add_cursor_pointer(pixels, width, height, left, top):
    if not hasattr(ctypes, "windll"):
        return pixels

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    position = Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(position)):
        return pixels

    x = position.x - left
    y = position.y - top
    if not (0 <= x < width and 0 <= y < height):
        return pixels
    outer = ((0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (13, 12))
    inner = ((1, 2), (1, 14), (4, 10), (8, 18), (9, 17), (6, 10), (11, 10))
    frame = bytearray(pixels)

    def fill_polygon(points, color):
        for row in range(min(point[1] for point in points), max(point[1] for point in points)):
            scan_y = row + 0.5
            intersections = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if (y1 > scan_y) != (y2 > scan_y):
                    intersections.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
            intersections.sort()
            for index in range(0, len(intersections) - 1, 2):
                start = max(0, x + int(intersections[index] + 0.5))
                end = min(width, x + int(intersections[index + 1] + 0.5))
                target_y = y + row
                if target_y < 0 or target_y >= height:
                    continue
                for target_x in range(start, end):
                    offset = (target_y * width + target_x) * 4
                    frame[offset : offset + 4] = color

    fill_polygon(outer, b"\x00\x00\x00\xff")
    fill_polygon(inner, b"\xff\xff\xff\xff")
    return frame


class ScreenRecorder:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Recorder")
        self.root.geometry("600x750")
        self.root.minsize(500, 650)
        self.root.configure(bg="#f4f6f5")

        self.messages = queue.Queue()
        self.stop_event = None
        self.recording = False
        self.processing = False
        self.close_when_done = False

        tk.Label(
            root,
            text="SCREEN RECORDER",
            bg="#f4f6f5",
            fg="#183b38",
            font=("Segoe UI", 10, "bold"),
        ).pack(pady=(16, 8))

        window_selector = tk.Frame(root, bg="#f4f6f5")
        window_selector.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(
            window_selector,
            text="Capture",
            bg="#f4f6f5",
            fg="#183b38",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        self.window_options = []
        self.window_selector = ttk.Combobox(
            window_selector,
            state="readonly",
            width=42,
            values=("Entire screen",),
        )
        self.window_selector.current(0)
        self.window_selector.pack(side="left", fill="x", expand=True)
        self.refresh_windows_button = tk.Button(
            window_selector,
            text="Refresh",
            command=self.refresh_windows,
        )
        self.refresh_windows_button.pack(side="left", padx=(6, 0))

        self.record_button = tk.Button(
            root,
            text="Start Recording",
            command=self.toggle_recording,
            bg="#176b59",
            fg="white",
            activebackground="#125747",
            activeforeground="white",
            disabledforeground="#ffffff",
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            padx=20,
            pady=9,
            cursor="hand2",
        )
        self.record_button.pack()

        recordings_frame = tk.LabelFrame(
            root,
            text="Previous recordings",
            bg="#f4f6f5",
            fg="#183b38",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=8,
        )
        recordings_frame.pack(fill="both", expand=True, padx=18, pady=(16, 8))
        list_frame = tk.Frame(recordings_frame, bg="#f4f6f5")
        list_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.recordings_list = tk.Listbox(
            list_frame,
            height=7,
            font=("Segoe UI", 9),
            activestyle="dotbox",
            yscrollcommand=scrollbar.set,
            exportselection=False,
        )
        self.recordings_list.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.recordings_list.yview)
        self.recordings_list.bind("<<ListboxSelect>>", self.select_recording)
        self.recordings_list.bind("<Double-Button-1>", lambda _event: self.open_selected())

        recording_actions = tk.Frame(recordings_frame, bg="#f4f6f5")
        recording_actions.pack(fill="x", pady=(8, 0))
        self.open_button = tk.Button(
            recording_actions,
            text="Play selected",
            command=self.open_selected,
            state="disabled",
        )
        self.open_button.pack(side="left")
        tk.Button(
            recording_actions,
            text="Choose file...",
            command=self.choose_recording,
        ).pack(side="right")

        adjustments = tk.LabelFrame(
            root,
            text="Adjust selected recording",
            bg="#f4f6f5",
            fg="#183b38",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=5,
        )
        adjustments.pack(fill="x", padx=18, pady=8)
        self.brightness = tk.DoubleVar(value=0.0)
        self.contrast = tk.DoubleVar(value=1.0)
        tk.Label(adjustments, text="Brightness", bg="#f4f6f5", anchor="w").pack(fill="x")
        tk.Scale(
            adjustments,
            variable=self.brightness,
            from_=-0.5,
            to=0.5,
            resolution=0.05,
            orient="horizontal",
            showvalue=True,
            bg="#f4f6f5",
            highlightthickness=0,
        ).pack(fill="x")
        tk.Label(adjustments, text="Contrast", bg="#f4f6f5", anchor="w").pack(fill="x")
        tk.Scale(
            adjustments,
            variable=self.contrast,
            from_=0.5,
            to=2.0,
            resolution=0.05,
            orient="horizontal",
            showvalue=True,
            bg="#f4f6f5",
            highlightthickness=0,
        ).pack(fill="x")
        self.save_button = tk.Button(
            adjustments,
            text="Save adjusted copy",
            command=self.save_adjusted_copy,
            state="disabled",
            bg="#176b59",
            fg="white",
            activebackground="#125747",
            activeforeground="white",
            disabledforeground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=14,
            pady=8,
        )
        self.save_button.pack(anchor="e", pady=(4, 0))

        self.status = tk.StringVar(value="Ready")
        tk.Label(
            root,
            textvariable=self.status,
            bg="#f4f6f5",
            fg="#60716d",
            font=("Segoe UI", 9),
            wraplength=510,
            justify="center",
        ).pack(fill="x", padx=16, pady=(6, 14))

        self.recordings_directory = Path.home() / "Videos" / "Screen Recordings"
        self.recordings = []
        self.additional_recordings = []
        self.selected_recording = None
        self.refresh_windows()
        self.refresh_recordings()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.check_messages)

    def toggle_recording(self):
        if self.processing:
            return
        if self.recording:
            self.stop_event.set()
            self.record_button.configure(text="Stopping...", state="disabled")
            self.status.set("Finishing the video...")
            return

        self.stop_event = threading.Event()
        selected_index = self.window_selector.current()
        hwnd = self.window_options[selected_index][1] if selected_index > 0 else None
        self.record_button.configure(state="disabled")
        self.window_selector.configure(state="disabled")
        self.refresh_windows_button.configure(state="disabled")
        self.status.set("Starting screen recording...")
        threading.Thread(target=self.record_screen, args=(hwnd,), daemon=True).start()

    def record_screen(self, hwnd):
        process = None
        output_path = None
        try:
            import imageio_ffmpeg

            self.recordings_directory.mkdir(parents=True, exist_ok=True)
            output_path = self.recordings_directory / (
                f"screen-recording-{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.mp4"
            )

            capture = WindowCapture(hwnd) if hwnd else ScreenCapture()
            with capture as screen:
                width = screen.width
                height = screen.height
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                process = subprocess.Popen(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "bgra",
                        "-video_size",
                        f"{width}x{height}",
                        "-framerate",
                        str(FRAME_RATE),
                        "-i",
                        "-",
                        "-an",
                        "-vf",
                        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "28",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(output_path),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

                self.messages.put(("started", None))
                frame_interval = 1 / FRAME_RATE
                next_frame = time.monotonic()
                while not self.stop_event.is_set():
                    frame = screen.grab()
                    if process.stdin is None:
                        raise RuntimeError("The video encoder did not start correctly.")
                    if hwnd:
                        pixels = frame
                    else:
                        pixels, left, top = frame
                        pixels = add_cursor_pointer(
                            pixels, width, height, left, top
                        )
                    process.stdin.write(pixels)
                    next_frame += frame_interval
                    self.stop_event.wait(max(0, next_frame - time.monotonic()))

                if process.stdin is not None:
                    process.stdin.close()
                try:
                    return_code = process.wait(timeout=30)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("The video encoder took too long to finish.") from error

            if return_code != 0:
                error_output = process.stderr.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(error_output or "The video encoder could not save the recording.")
        except Exception as error:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.stderr is not None:
                    process.stderr.close()
            if output_path is not None:
                try:
                    output_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error = RuntimeError(f"{error} Could not remove incomplete file: {cleanup_error}")
            self.messages.put(("error", str(error)))
        else:
            self.messages.put(("saved", str(output_path)))

    def check_messages(self):
        try:
            while True:
                message_type, value = self.messages.get_nowait()
                if message_type == "started":
                    self.recording = True
                    self.record_button.configure(text="Stop Recording", state="normal")
                    self.window_selector.configure(state="disabled")
                    self.refresh_windows_button.configure(state="disabled")
                    self.update_editing_controls()
                    self.status.set("Recording screen...")
                elif message_type == "saved":
                    self.recording = False
                    self.record_button.configure(text="Start Recording", state="normal")
                    self.window_selector.configure(state="readonly")
                    self.refresh_windows_button.configure(state="normal")
                    self.status.set(f"Saved: {value}")
                    self.refresh_recordings(select_path=Path(value))
                    if self.close_when_done:
                        self.root.destroy()
                        return
                elif message_type == "adjusted":
                    self.processing = False
                    self.set_editing_controls_enabled(True)
                    self.refresh_recordings(select_path=Path(value))
                    self.status.set(f"Saved adjusted copy: {value}")
                    if self.close_when_done:
                        self.root.destroy()
                        return
                else:
                    if message_type == "adjust_error":
                        self.processing = False
                        self.set_editing_controls_enabled(True)
                    else:
                        self.recording = False
                        self.record_button.configure(text="Start Recording", state="normal")
                        self.window_selector.configure(state="readonly")
                        self.refresh_windows_button.configure(state="normal")
                    self.status.set(value)
                    if self.close_when_done:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass

        self.root.after(100, self.check_messages)

    def refresh_windows(self):
        previous = self.window_selector.current()
        previous_hwnd = (
            self.window_options[previous][1]
            if 0 <= previous < len(self.window_options)
            else None
        )
        try:
            self.window_options = [("Entire screen", None), *list_windows()]
        except (OSError, RuntimeError) as error:
            self.status.set(f"Could not list open windows: {error}")
            self.window_options = [("Entire screen", None)]
        labels = ["Entire screen"] + [
            f"{title}  (window {hwnd})" for title, hwnd in self.window_options[1:]
        ]
        self.window_selector.configure(values=labels)
        selected_index = next(
            (
                index
                for index, (_title, hwnd) in enumerate(self.window_options)
                if hwnd == previous_hwnd
            ),
            0,
        )
        self.window_selector.current(selected_index)

    def refresh_recordings(self, select_path=None):
        self.recordings_directory.mkdir(parents=True, exist_ok=True)
        if select_path and select_path.parent != self.recordings_directory:
            self.additional_recordings.append(select_path)
        self.additional_recordings = [
            path for path in dict.fromkeys(self.additional_recordings) if path.is_file()
        ]
        self.recordings = list(self.recordings_directory.glob("*.mp4"))
        self.recordings.extend(
            path for path in self.additional_recordings if path not in self.recordings
        )
        self.recordings.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        self.recordings_list.delete(0, tk.END)
        for path in self.recordings:
            label = path.name
            if path.stem.endswith("-adjusted") or "-adjusted-" in path.stem:
                label += " (adjusted)"
            self.recordings_list.insert(tk.END, label)

        if select_path in self.recordings:
            index = self.recordings.index(select_path)
            self.recordings_list.selection_set(index)
            self.recordings_list.see(index)
            self.selected_recording = select_path
        elif self.selected_recording not in self.recordings:
            self.selected_recording = None
            self.recordings_list.selection_clear(0, tk.END)
        self.update_editing_controls()

    def select_recording(self, _event=None):
        selection = self.recordings_list.curselection()
        self.selected_recording = self.recordings[selection[0]] if selection else None
        self.update_editing_controls()

    def update_editing_controls(self):
        state = (
            "normal"
            if self.selected_recording and not self.processing and not self.recording
            else "disabled"
        )
        self.open_button.configure(state=state)
        self.save_button.configure(state=state)

    def set_editing_controls_enabled(self, enabled):
        self.update_editing_controls()
        state = "normal" if enabled else "disabled"
        self.record_button.configure(state=state)
        if not enabled:
            self.open_button.configure(state="disabled")
            self.save_button.configure(state="disabled")

    def open_selected(self):
        if not self.selected_recording or not self.selected_recording.is_file():
            return
        try:
            os.startfile(str(self.selected_recording))
        except OSError as error:
            self.status.set(f"Could not open recording: {error}")

    def choose_recording(self):
        selected = filedialog.askopenfilename(
            title="Choose a screen recording",
            initialdir=str(self.recordings_directory),
            filetypes=[("MP4 videos", "*.mp4"), ("All files", "*.*")],
        )
        if not selected:
            return
        path = Path(selected)
        if path not in self.recordings:
            self.recordings.insert(0, path)
            self.recordings_list.insert(0, path.name)
        self.recordings_list.selection_clear(0, tk.END)
        self.recordings_list.selection_set(self.recordings.index(path))
        self.recordings_list.see(self.recordings.index(path))
        self.selected_recording = path
        self.update_editing_controls()

    def save_adjusted_copy(self):
        if self.processing or self.recording or not self.selected_recording:
            return
        source = self.selected_recording
        if not source.is_file():
            self.status.set("The selected recording could not be found.")
            self.refresh_recordings()
            return

        brightness = self.brightness.get()
        contrast = self.contrast.get()
        output = self.available_adjusted_path(source)
        self.processing = True
        self.set_editing_controls_enabled(False)
        self.status.set("Applying brightness and contrast to a new copy...")
        threading.Thread(
            target=self.adjust_recording,
            args=(source, output, brightness, contrast),
            daemon=True,
        ).start()

    def available_adjusted_path(self, source):
        candidate = source.with_name(f"{source.stem}-adjusted{source.suffix}")
        number = 2
        while candidate.exists():
            candidate = source.with_name(
                f"{source.stem}-adjusted-{number}{source.suffix}"
            )
            number += 1
        return candidate

    def adjust_recording(self, source, output, brightness, contrast):
        try:
            import imageio_ffmpeg

            filter_value = f"eq=brightness={brightness:.2f}:contrast={contrast:.2f}"
            result = subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-vf",
                    filter_value,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or "FFmpeg could not process this recording."
                )
        except Exception as error:
            try:
                output.unlink(missing_ok=True)
            except OSError as cleanup_error:
                error = RuntimeError(f"{error} Could not remove incomplete file: {cleanup_error}")
            self.messages.put(("adjust_error", f"Could not save adjusted recording: {error}"))
        else:
            self.messages.put(("adjusted", str(output)))

    def close(self):
        if self.processing:
            self.status.set("Please wait for the adjusted video to finish saving before closing.")
            return
        if self.stop_event is not None and not self.stop_event.is_set():
            self.close_when_done = True
            self.stop_event.set()
            self.record_button.configure(state="disabled")
            self.status.set("Finishing the video before closing...")
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    ScreenRecorder(root)
    root.mainloop()


if __name__ == "__main__":
    main()
