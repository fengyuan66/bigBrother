import json
import os
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlparse

from config import env_int, load_env_file

try:
    import websocket
except ImportError:
    websocket = None


APP_DIR = Path(__file__).resolve().parent
load_env_file(APP_DIR / ".env")

SOURCES_DIR = APP_DIR / "sources"
BROWSER_DIR = SOURCES_DIR / "browser"
BROWSER_TABS_DIR = BROWSER_DIR / "tabs"
SUMMARIES_DIR = APP_DIR / "summaries"
STATE_DIR = APP_DIR / "state"

OUTPUT_PATH = BROWSER_DIR / "browser_live.txt"
TAB_OUTPUT_PATH = BROWSER_DIR / "tabs.txt"
INDEX_OUTPUT_PATH = BROWSER_DIR / "index.json"
SUMMARY_OUTPUT_PATH = SUMMARIES_DIR / "browser_summary.json"


def ensure_output_dirs():
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_TABS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


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
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            start_url,
        ]
        subprocess.Popen(args)

    def _read_tabs_once(self):
        with urlopen(f"http://127.0.0.1:{self.config.port}/json/list", timeout=1.5) as response:
            pages = json.loads(response.read().decode("utf-8"))

        tabs = []
        for page in pages:
            if page.get("type") != "page":
                continue
            url = page.get("url", "")
            if url.startswith(("devtools://", "chrome://", "edge://", "brave://")):
                continue
            tabs.append(page)
        return tabs

    def read_tabs(self, retries=1, delay_seconds=0.35):
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                tabs = self._read_tabs_once()
            except (OSError, URLError, TimeoutError, json.JSONDecodeError):
                tabs = []
            if tabs or attempt == attempts - 1:
                return tabs
            time.sleep(max(0.05, float(delay_seconds)))
        return []

    def export_tabs(self, output_path=TAB_OUTPUT_PATH, retries=4, delay_seconds=0.5):
        ensure_output_dirs()
        tabs = self.read_tabs(retries=retries, delay_seconds=delay_seconds)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "Big Brother Browser Tab Export",
            f"Created: {now}",
            f"Browser: {self.config.name}",
            "",
        ]

        if not tabs:
            lines.extend(
                [
                    "No tabs found yet.",
                    "Use Launch Browser in this app, then navigate inside that browser window.",
                ]
            )
        else:
            for index, tab in enumerate(tabs, start=1):
                url = tab.get("url", "")
                domain = urlparse(url).netloc or "(no domain)"
                lines.extend(
                    [
                        f"{index}. {tab.get('title') or '(untitled)'}",
                        f"   URL: {url}",
                        f"   Domain: {domain}",
                        "",
                    ]
                )

        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path, len(tabs)

    def write_index(self, output_path=INDEX_OUTPUT_PATH, retries=4, delay_seconds=0.5):
        ensure_output_dirs()
        tabs = self.read_tabs(retries=retries, delay_seconds=delay_seconds)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data = {
            "updated": now,
            "browser": self.config.name,
            "tab_count": len(tabs),
            "tabs": [
                {
                    "tab_id": tab.get("id"),
                    "title": tab.get("title", ""),
                    "url": tab.get("url", ""),
                    "domain": urlparse(tab.get("url", "")).netloc,
                }
                for tab in tabs
            ],
        }
        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return output_path, data

    def write_summary(self, output_path=SUMMARY_OUTPUT_PATH, retries=4, delay_seconds=0.5):
        ensure_output_dirs()
        tabs = self.read_tabs(retries=retries, delay_seconds=delay_seconds)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        domains = []
        seen_domains = set()
        for tab in tabs:
            domain = urlparse(tab.get("url", "")).netloc
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                domains.append(domain)

        summary = {
            "updated": now,
            "source": "browser",
            "browser": self.config.name,
            "tab_count": len(tabs),
            "top_domains": domains[:10],
            "active_signals": [
                {
                    "title": tab.get("title", ""),
                    "url": tab.get("url", ""),
                }
                for tab in tabs[:5]
            ],
            "summary": (
                "No tabs found in the demo browser."
                if not tabs
                else f"{len(tabs)} open tab(s) in {self.config.name}. "
                f"Top domains: {', '.join(domains[:3]) or 'none'}."
            ),
        }
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return output_path, summary

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
        ensure_output_dirs()
        self.title("Big Brother Browser Live Demo")
        self.geometry("920x680")
        self.minsize(760, 520)

        self.browser_var = tk.StringVar(value=os.getenv("BIG_BROTHER_DEMO_BROWSER", "Chrome"))
        self.url_var = tk.StringVar(
            value=os.getenv("BIG_BROTHER_DEMO_URL", "https://en.wikipedia.org/wiki/Calculus")
        )
        self.interval_var = tk.IntVar(value=env_int("BIG_BROTHER_DEMO_INTERVAL_SECONDS", 4))
        self.status_var = tk.StringVar(value="Launch a demo browser, then start live output.")
        self.files_var = tk.StringVar(
            value="Outputs: sources/browser/ + summaries/browser_summary.json"
        )
        self.running = False
        self.worker = None
        self.stop_event = threading.Event()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

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
            from_=4,
            to=60,
            textvariable=self.interval_var,
            width=5,
            justify="center",
        ).grid(row=0, column=5, padx=(8, 4), sticky="w")
        ttk.Label(controls, text="sec").grid(row=0, column=6, sticky="w")

        buttons = ttk.Frame(root)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 12))
        ttk.Button(buttons, text="Launch Browser", command=self.launch_browser).pack(side="left")
        ttk.Button(buttons, text="Export Tabs", command=self.export_tabs).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="Snapshot Once", command=self.snapshot_once).pack(
            side="left", padx=(10, 0)
        )
        self.start_button = ttk.Button(buttons, text="Start Live Output", command=self.start)
        self.start_button.pack(side="left", padx=(10, 0))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))

        ttk.Label(root, textvariable=self.files_var, foreground="#666666").grid(
            row=4, column=0, sticky="w", pady=(0, 8)
        )

        self.output = tk.Text(root, wrap="word", font=("Consolas", 10), undo=False)
        self.output.grid(row=5, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(root, command=self.output.yview)
        scrollbar.grid(row=5, column=1, sticky="ns")
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

    def snapshot_once(self):
        reader = self._reader()
        text = self._snapshot(reader)
        ensure_output_dirs()
        OUTPUT_PATH.write_text(text, encoding="utf-8")
        self._show_snapshot(text)
        self.export_tabs(show_message=False)
        reader.write_index()
        reader.write_summary()

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
            ensure_output_dirs()
            OUTPUT_PATH.write_text(text, encoding="utf-8")
            reader.export_tabs(retries=2, delay_seconds=0.25)
            reader.write_index(retries=2, delay_seconds=0.25)
            reader.write_summary(retries=2, delay_seconds=0.25)
            self.after(0, self._show_snapshot, text)
            self.stop_event.wait(max(4, int(self.interval_var.get() or 4)))

    def _snapshot(self, reader):
        tabs = reader.read_tabs(retries=3, delay_seconds=0.4)
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
        self.status_var.set(
            "Live output updating. Saved browser files under sources/browser and summaries."
        )

    def export_tabs(self, show_message=True):
        reader = self._reader()
        path, count = reader.export_tabs()
        self.status_var.set(f"Exported {count} tabs to {path.name}.")
        if show_message:
            messagebox.showinfo(
                "Tabs exported",
                f"Exported {count} tabs to:\n{path}\n\n"
                "If this found 0 tabs, use Launch Browser in this app first.",
            )

    def _reader(self):
        return BrowserLiveReader(BROWSERS[self.browser_var.get()])

    def _on_close(self):
        self.stop()
        self.destroy()


if __name__ == "__main__":
    app = BrowserDemoApp()
    app.mainloop()

