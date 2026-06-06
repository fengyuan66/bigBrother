import base64
import io
import json
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from PIL import Image
import mss

from config import env_int, load_env_file

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


APP_DIR = Path(__file__).resolve().parent
load_env_file(APP_DIR / ".env")

TAB_EXPORT_PATH = APP_DIR / "tabs.txt"
DEFAULT_BASE_URL = os.getenv("BIG_BROTHER_BASE_URL", "https://ai.hackclub.com/proxy/v1")
DEFAULT_MODEL = os.getenv("BIG_BROTHER_MODEL", "google/gemini-2.5-flash")


@dataclass
class FocusResult:
    on_task: bool
    confidence: float
    reason: str


class ScreenCapture:
    def capture_primary_png(self, max_width=1280):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)

        if image.width > max_width:
            ratio = max_width / image.width
            height = int(image.height * ratio)
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


class VisionFocusEvaluator:
    def __init__(self):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = DEFAULT_MODEL
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, goal, png_bytes):
        if not self.client:
            return self._fallback(goal)

        image_b64 = base64.b64encode(png_bytes).decode("ascii")
        prompt = (
            "You are a strict but fair focus coach. Decide if the screenshot is consistent "
            "with the user's stated task. Only judge visible screen content. "
            "Return compact JSON with keys: on_task boolean, confidence number from 0 to 1, "
            "and reason string under 120 characters.\n\n"
            f"User task: {goal}"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                },
            ],
            temperature=0,
        )

        data = json.loads(response.choices[0].message.content or "{}")
        return FocusResult(
            on_task=bool(data.get("on_task")),
            confidence=float(data.get("confidence", 0.5)),
            reason=str(data.get("reason", "No reason provided.")),
        )

    def _fallback(self, goal):
        return FocusResult(
            on_task=True,
            confidence=0.0,
            reason="AI checks disabled. Set OPENAI_API_KEY to enable screenshot evaluation.",
        )


class ReminderPopup(tk.Toplevel):
    def __init__(self, parent, goal, reason):
        super().__init__(parent)
        self.title("Big Brother Reminder")
        self.attributes("-topmost", True)
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frame,
            text="Back to task",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            frame,
            text=f"You said: {goal}",
            wraplength=420,
        ).grid(row=1, column=0, pady=(10, 4), sticky="w")

        ttk.Label(
            frame,
            text=reason,
            wraplength=420,
            foreground="#555555",
        ).grid(row=2, column=0, pady=(0, 14), sticky="w")

        ttk.Button(frame, text="I'm back", command=self.destroy).grid(row=3, column=0, sticky="e")

        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = self.winfo_screenwidth() - width - 28
        y = self.winfo_screenheight() - height - 72
        self.geometry(f"+{x}+{y}")
        self.after(12000, self.destroy)


class BigBrotherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Big Brother")
        self.geometry("560x430")
        self.minsize(520, 400)

        self.capture = ScreenCapture()
        self.evaluator = VisionFocusEvaluator()
        self.events = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.off_task_streak = 0
        self.last_popup_at = 0

        self.goal_var = tk.StringVar(value="I am going to study calculus")
        self.interval_var = tk.IntVar(value=env_int("BIG_BROTHER_INTERVAL_SECONDS", 8))
        self.threshold_var = tk.IntVar(value=env_int("BIG_BROTHER_OFF_TASK_THRESHOLD", 2))
        self.status_var = tk.StringVar(value="Ready.")
        self.streak_var = tk.StringVar(value="Off-task streak: 0")
        self.ai_var = tk.StringVar(
            value=(
                f"AI vision: enabled ({self.evaluator.model})"
                if self.evaluator.enabled
                else "AI vision: disabled"
            )
        )

        self._build_ui()
        self.after(250, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text="Big Brother", font=("Segoe UI", 22, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(root, text="Visible focus monitoring for your own screen.").grid(
            row=1, column=0, sticky="w", pady=(2, 18)
        )

        ttk.Label(root, text="Current task").grid(row=2, column=0, sticky="w")
        goal_entry = ttk.Entry(root, textvariable=self.goal_var)
        goal_entry.grid(row=3, column=0, sticky="ew", pady=(4, 14))

        controls = ttk.Frame(root)
        controls.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="Check every").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            controls,
            from_=5,
            to=60,
            textvariable=self.interval_var,
            width=6,
            justify="center",
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))
        ttk.Label(controls, text="seconds").grid(row=0, column=2, sticky="w")

        ttk.Label(controls, text="Remind after").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(
            controls,
            from_=1,
            to=10,
            textvariable=self.threshold_var,
            width=6,
            justify="center",
        ).grid(row=1, column=1, sticky="w", padx=(8, 18), pady=(10, 0))
        ttk.Label(controls, text="off-task checks").grid(row=1, column=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(root)
        buttons.grid(row=5, column=0, sticky="ew", pady=(2, 18))

        self.start_button = ttk.Button(buttons, text="Start Focus Session", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="Test Reminder", command=self._test_reminder).pack(
            side="left", padx=(10, 0)
        )

        ttk.Separator(root).grid(row=6, column=0, sticky="ew", pady=(0, 14))

        ttk.Label(root, textvariable=self.ai_var).grid(row=7, column=0, sticky="w")
        ttk.Label(root, textvariable=self.streak_var).grid(row=8, column=0, sticky="w", pady=(6, 0))
        ttk.Label(root, textvariable=self.status_var, wraplength=500).grid(
            row=9, column=0, sticky="w", pady=(6, 0)
        )

        ttk.Label(
            root,
            text="Tip: put BIG_BROTHER_API_KEY in .env to enable Hack Club AI screenshot checks.",
            foreground="#666666",
            wraplength=500,
        ).grid(row=10, column=0, sticky="w", pady=(24, 0))

    def start(self):
        goal = self.goal_var.get().strip()
        if not goal:
            messagebox.showerror("Missing task", "Tell Big Brother what you are trying to do.")
            return

        self.off_task_streak = 0
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Monitoring started.")
        self.streak_var.set("Off-task streak: 0")

        self.worker = threading.Thread(target=self._monitor_loop, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Monitoring stopped.")

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            goal = self.goal_var.get().strip()
            try:
                self.events.put(("status", "Capturing screenshot..."))
                png = self.capture.capture_primary_png()
                self.events.put(("status", "Checking focus..."))
                result = self.evaluator.evaluate(goal, png)
                self.events.put(("result", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))

            interval = max(5, int(self.interval_var.get() or 8))
            self.stop_event.wait(interval)

    def _drain_events(self):
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "status":
                self.status_var.set(payload)
            elif event == "error":
                self.status_var.set(f"Error: {payload}")
            elif event == "result":
                self._handle_result(payload)

        self.after(250, self._drain_events)

    def _handle_result(self, result):
        if result.on_task:
            self.off_task_streak = 0
            state = "On task"
        else:
            self.off_task_streak += 1
            state = "Off task"

        self.streak_var.set(f"Off-task streak: {self.off_task_streak}")
        self.status_var.set(f"{state} ({result.confidence:.0%}): {result.reason}")

        threshold = max(1, int(self.threshold_var.get() or 2))
        enough_time_since_popup = time.time() - self.last_popup_at > 20
        if self.off_task_streak >= threshold and enough_time_since_popup:
            self.last_popup_at = time.time()
            ReminderPopup(self, self.goal_var.get().strip(), result.reason)

    def _test_reminder(self):
        ReminderPopup(
            self,
            self.goal_var.get().strip() or "your task",
            "This is what the nudge looks like when you drift.",
        )

    def _on_close(self):
        self.stop()
        self.destroy()


if __name__ == "__main__":
    app = BigBrotherApp()
    app.mainloop()
