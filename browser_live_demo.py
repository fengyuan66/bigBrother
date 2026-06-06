import json
import os
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.error import URLError
from urllib.request import urlopen

try:
    import websocket
except ImportError:
    websocket = None


APP_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = APP_DIR / "browser_live.txt"


@dataclass
class BrowserConfig:
    name: str
    port: int
    paths: list[str]


BROWSERS = {
    "Chrome": BrowserConfig(
        name="Chrome",
        port=9332,
        paths=[
            rf"{os.environ.get('ProgramFiles', '')}\Google\Chrome\Application\chrome.exe",
            rf"{os.environ.get('ProgramFiles(x86)', '')}\Google\Chrome\Application\chrome.exe",
            rf"{os.environ.get('LocalAppData', '')}\Google\Chrome\Application\chrome.exe",
        ],
    ),
    "Edge": BrowserConfig(
        name="Edge",
        port=9333,
        paths=[
            rf"{os.environ.get('ProgramFiles', '')}\Microsoft\Edge\Application\msedge.exe",
            rf"{os.environ.get('ProgramFiles(x86)', '')}\Microsoft\Edge\Application\msedge.exe",
        ],
    ),
    "Brave": BrowserConfig(
        name="Brave",
        port=9334,
        paths=[
            rf"{os.environ.get('ProgramFiles', '')}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{os.environ.get('ProgramFiles(x86)', '')}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{os.environ.get('LocalAppData', '')}\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
    ),
}


class BrowserLiveReader:
    def __init__(self, config):
        self.config = config

    def find_browser(self):
        for path in self.config.paths:
            if path and Path(path).exists():
                return path
        return None

    def launch(self, start_url):
        browser_path = self.find_browser()
        if not browser_path:
            raise RuntimeError(f"Could not find {self.config.name}.")

        profile_dir = APP_DIR / f".demo-profile-{self.config.name.lower()}"
        profile_dir.mkdir(exist_ok=True)

        args = [
            browser_path,
            f"--remote-debugging-port={self.config.port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            start_url,
        ]
        subprocess.Popen(args)

    def read_tabs(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.config.port}/json/list", timeout=1.5) as response:
                pages = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            return []

        tabs = []
        for page in pages:
            if page.get("type") != "page":
                continue
            url = page.get("url", "")
            if url.startswith(("devtools://", "chrome://", "edge://", "brave://")):
                continue
            tabs.append(page)
        return tabs

    def read_page_text(self, tab):
        if not websocket:
            return "Install websocket-client to read page text: pip install websocket-client"

        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            return "No debugger WebSocket URL for this tab."

        expression = """
(() => {
  const title = document.title || "";
  const meta = document.querySelector('meta[name="description"]')?.content || "";
  const body = document.body ? document.body.innerText : "";
  return [title, meta, body].filter(Boolean).join("\\n\\n").slice(0, 12000);
})()
"""
        payload = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }

        try:
            ws = websocket.create_connection(ws_url, timeout=2)
            ws.send(json.dumps(payload))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    ws.close()
                    return (
                        message.get("result", {})
                        .get("result", {})
                        .get("value", "")
                    ) or "(No visible page text.)"
        except Exception as exc:
            return f"Could not read page text: {exc}"


class BrowserDemoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Big Brother Browser Live Demo")
        self.geometry("920x680")
        self.minsize(760, 520)

        self.browser_var = tk.StringVar(value="Chrome")
        self.url_var = tk.StringVar(value="https://en.wikipedia.org/wiki/Calculus")
        self.interval_var = tk.IntVar(value=2)
        self.status_var = tk.StringVar(value="Launch a demo browser, then start live output.")
        self.running = False
        self.worker = None
        self.stop_event = threading.Event()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        ttk.Label(root, text="Browser Live Demo", font=("Segoe UI", 20, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(root, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 14))

        controls = ttk.Frame(root)
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="Browser").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            controls,
            textvariable=self.browser_var,
            values=list(BROWSERS.keys()),
            width=10,
            state="readonly",
        ).grid(row=0, column=1, padx=(8, 16), sticky="w")

        ttk.Label(controls, text="URL").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.url_var).grid(row=0, column=3, padx=(8, 16), sticky="ew")

        ttk.Label(controls, text="Every").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(
            controls,
            from_=1,
            to=15,
            textvariable=self.interval_var,
            width=5,
            justify="center",
        ).grid(row=0, column=5, padx=(8, 4), sticky="w")
        ttk.Label(controls, text="sec").grid(row=0, column=6, sticky="w")

        buttons = ttk.Frame(root)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 12))
        ttk.Button(buttons, text="Launch Browser", command=self.launch_browser).pack(side="left")
        self.start_button = ttk.Button(buttons, text="Start Live Output", command=self.start)
        self.start_button.pack(side="left", padx=(10, 0))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))

        self.output = tk.Text(root, wrap="word", font=("Consolas", 10), undo=False)
        self.output.grid(row=4, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(root, command=self.output.yview)
        scrollbar.grid(row=4, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)

    def launch_browser(self):
        try:
            reader = self._reader()
            reader.launch(self.url_var.get().strip() or "about:blank")
            self.status_var.set("Browser launched. Open/navigate tabs, then start live output.")
        except Exception as exc:
            messagebox.showerror("Launch failed", str(exc))

    def start(self):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(f"Stopped. Last output saved to {OUTPUT_PATH.name}.")

    def _loop(self):
        reader = self._reader()
        while not self.stop_event.is_set():
            text = self._snapshot(reader)
            OUTPUT_PATH.write_text(text, encoding="utf-8")
            self.after(0, self._show_snapshot, text)
            self.stop_event.wait(max(1, int(self.interval_var.get() or 2)))

    def _snapshot(self, reader):
        tabs = reader.read_tabs()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "Big Brother Browser Live Output",
            f"Updated: {now}",
            f"Browser: {reader.config.name}",
            "",
        ]

        if not tabs:
            lines.extend(
                [
                    "No tabs found yet.",
                    "Use Launch Browser in this app, then navigate inside that browser window.",
                ]
            )
            return "\n".join(lines)

        for index, tab in enumerate(tabs, start=1):
            lines.extend(
                [
                    "=" * 80,
                    f"Tab {index}: {tab.get('title') or '(untitled)'}",
                    f"URL: {tab.get('url', '')}",
                    "-" * 80,
                    reader.read_page_text(tab),
                    "",
                ]
            )
        return "\n".join(lines)

    def _show_snapshot(self, text):
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.status_var.set(f"Live output updating. Saved to {OUTPUT_PATH.name}.")

    def _reader(self):
        return BrowserLiveReader(BROWSERS[self.browser_var.get()])

    def _on_close(self):
        self.stop()
        self.destroy()


if __name__ == "__main__":
    app = BrowserDemoApp()
    app.mainloop()

