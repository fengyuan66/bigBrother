import base64
from datetime import datetime
import json
import mimetypes
import os
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
PORT = int(os.environ.get("PORT", "8000"))
MODEL = "qwen/qwen3-vl-235b-a22b-instruct"
API_URL = "https://ai.hackclub.com/proxy/v1/chat/completions"
SOURCES_DIR = PROJECT_ROOT / "sources"
SUMMARIES_DIR = PROJECT_ROOT / "summaries"

# Keep the folder mapping exactly as requested:
# - webcam capture writes under sources/video
# - screenshare capture writes under sources/webcam
MODE_TO_SOURCE_DIR = {
    "webcam": SOURCES_DIR / "video",
    "screen": SOURCES_DIR / "webcam",
}
MODE_TO_SUMMARY_PATH = {
    "webcam": SUMMARIES_DIR / "webcam_summary.json",
    "screen": SUMMARIES_DIR / "screen_summary.json",
}


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def parse_data_url(data_url: str) -> str:
    if not data_url.startswith("data:image/"):
        raise ValueError("Expected a base64-encoded image data URL.")

    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Expected a base64-encoded image data URL.")

    base64.b64decode(encoded, validate=True)
    return data_url


def ensure_output_dirs() -> None:
    for path in MODE_TO_SOURCE_DIR.values():
        path.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)


def write_local_outputs(analysis_mode: str, prompt: str, summary: str) -> dict:
    ensure_output_dirs()
    source_dir = MODE_TO_SOURCE_DIR[analysis_mode]
    summary_path = MODE_TO_SUMMARY_PATH[analysis_mode]
    timestamp = datetime.now().isoformat(timespec="seconds")

    payload = {
        "timestamp": timestamp,
        "analysisMode": analysis_mode,
        "model": MODEL,
        "prompt": prompt,
        "summary": summary,
        "sourceFolder": str(source_dir.relative_to(PROJECT_ROOT)),
    }

    latest_json_path = source_dir / "latest.json"
    latest_txt_path = source_dir / "latest.txt"

    latest_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_txt_path.write_text(summary, encoding="utf-8")
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "latestJson": str(latest_json_path.relative_to(PROJECT_ROOT)),
        "latestText": str(latest_txt_path.relative_to(PROJECT_ROOT)),
        "summaryJson": str(summary_path.relative_to(PROJECT_ROOT)),
    }


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/analyze":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body)
            image_data_url = parse_data_url(payload["imageDataUrl"])
            user_prompt = (payload.get("prompt") or "").strip()
            analysis_mode = (payload.get("analysisMode") or "screen").strip().lower()
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        api_key = (os.environ.get("HACKCLUB_API_KEY") or "").strip()
        if not api_key:
            self.respond_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Missing API key. Add HACKCLUB_API_KEY to your .env file."},
            )
            return

        if analysis_mode == "webcam":
            prompt = (
                "You are analyzing a webcam image for another AI agent. "
                "Summarize what is happening in a concise, agent-friendly way. "
                "Focus on the person's visible actions, posture, attention, nearby objects, "
                "environment, and the most likely real-world task they are doing. "
                "State clear observations first, then short inferences, and explicitly mark uncertainty. "
                "Do not identify the person. If important details are occluded or blurry, say so."
            )
        else:
            prompt = (
                "You are analyzing a live computer screen capture. "
                "Describe exactly what is visible, focusing on apps, windows, layout, "
                "text that is readable, and the likely current task. "
                "Separate observed facts from any uncertainty. "
                "If text is too small or unclear, say that explicitly instead of guessing."
            )
        if user_prompt:
            prompt = f"{prompt}\n\nUser focus: {user_prompt}"

        upstream_body = {
            "model": MODEL,
            "temperature": 0.2,
            "max_tokens": 500,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
        }

        req = request.Request(
            API_URL,
            data=json.dumps(upstream_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=90) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            self.respond_json(
                exc.code,
                {
                    "error": "Hack Club API request failed.",
                    "details": error_body,
                },
            )
            return
        except error.URLError as exc:
            self.respond_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"Unable to reach Hack Club API: {exc.reason}"},
            )
            return

        try:
            message = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.respond_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Unexpected response from Hack Club API.",
                    "details": response_data,
                },
            )
            return

        written_files = write_local_outputs(analysis_mode, user_prompt, message)

        self.respond_json(
            HTTPStatus.OK,
            {
                "summary": message,
                "model": MODEL,
                "analysisMode": analysis_mode,
                "writtenFiles": written_files,
            },
        )

    def guess_type(self, path):
        if path.endswith(".js"):
            return "text/javascript; charset=utf-8"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    def respond_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    load_dotenv()
    ensure_output_dirs()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DemoHandler)
    print(f"Serving demo at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
