import base64
import json
import os
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from actors import MainProcessingActor, PersonalityActor, WatcherActor
from browser_live_demo import BROWSERS, TAB_OUTPUT_PATH, BrowserLiveReader
from config import env_int, load_env_file
from resources import ResourceLoader


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "webapp"
SOURCES_DIR = APP_DIR / "sources"
SUMMARIES_DIR = APP_DIR / "summaries"
PERSONALITY_AUDIO_PATH = SUMMARIES_DIR / "personality_latest.mp3"
PERSONALITY_JSON_PATH = SUMMARIES_DIR / "personality_latest.json"
load_env_file(APP_DIR / ".env")

VISION_MODEL = os.getenv("BIG_BROTHER_VISION_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")
API_URL = os.getenv("BIG_BROTHER_BASE_URL", "https://ai.hackclub.com/proxy/v1").rstrip("/") + "/chat/completions"

MODE_TO_SOURCE_DIR = {
    "webcam": SOURCES_DIR / "webcam",
    "screen": SOURCES_DIR / "video",
}
MODE_TO_SUMMARY_PATH = {
    "webcam": SUMMARIES_DIR / "webcam_summary.json",
    "screen": SUMMARIES_DIR / "screen_summary.json",
}
DEFAULT_TICK_SECONDS = 3
DEFAULT_OFF_TASK_THRESHOLD = 1
DEFAULT_POST_SPEECH_PAUSE_SECONDS = 5


def ensure_output_dirs():
    for path in MODE_TO_SOURCE_DIR.values():
        path.mkdir(parents=True, exist_ok=True)
    (SOURCES_DIR / "browser").mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)


def parse_data_url(data_url: str) -> str:
    if not data_url.startswith("data:image/"):
        raise ValueError("Expected a base64-encoded image data URL.")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Expected a base64-encoded image data URL.")
    base64.b64decode(encoded, validate=True)
    return data_url


def build_vision_prompt(analysis_mode: str, user_prompt: str) -> str:
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
            "readable text, and the likely current task. "
            "Separate observed facts from any uncertainty. "
            "If text is too small or unclear, say that explicitly instead of guessing."
        )
    if user_prompt:
        prompt = f"{prompt}\n\nUser focus: {user_prompt}"
    return prompt


def write_local_outputs(analysis_mode: str, prompt: str, summary: str) -> dict:
    ensure_output_dirs()
    source_dir = MODE_TO_SOURCE_DIR[analysis_mode]
    summary_path = MODE_TO_SUMMARY_PATH[analysis_mode]
    timestamp = datetime.now().isoformat(timespec="seconds")

    payload = {
        "timestamp": timestamp,
        "analysisMode": analysis_mode,
        "model": VISION_MODEL,
        "prompt": prompt,
        "summary": summary,
        "sourceFolder": str(source_dir.relative_to(APP_DIR)),
    }

    latest_json_path = source_dir / "latest.json"
    latest_txt_path = source_dir / "latest.txt"
    latest_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_txt_path.write_text(summary, encoding="utf-8")
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "latestJson": str(latest_json_path.relative_to(APP_DIR)),
        "latestText": str(latest_txt_path.relative_to(APP_DIR)),
        "summaryJson": str(summary_path.relative_to(APP_DIR)),
    }


class DashboardState:
    def __init__(self):
        self.lock = threading.Lock()
        self.goal = "I am studying calculus"
        self.interval_seconds = env_int("BIG_BROTHER_INTERVAL_SECONDS", DEFAULT_TICK_SECONDS)
        self.threshold = env_int("BIG_BROTHER_OFF_TASK_THRESHOLD", DEFAULT_OFF_TASK_THRESHOLD)
        self.running = False
        self.off_task_streak = 0
        self.threshold_progress = 0
        self.resource_revision = 0
        self.required_reassessment_revision = 0
        self.session_duration_seconds = env_int("BIG_BROTHER_SESSION_DURATION_SECONDS", 900)
        self.session_deadline_at = 0.0
        self.post_speech_pause_seconds = env_int(
            "BIG_BROTHER_POST_SPEECH_PAUSE_SECONDS", DEFAULT_POST_SPEECH_PAUSE_SECONDS
        )
        self.speech_grace_until = 0.0
        self.status = "Ready."
        self.last_error = ""
        self.last_turn_at = ""
        self.resources = {
            "webcam": "Waiting for webcam resource text.",
            "screenshare": "Waiting for screenshare resource text.",
            "browser": "Waiting for browser export text.",
        }
        self.watcher_output = {
            "off_task": False,
            "confidence": 0.0,
            "summary": "Watcher output will appear here after a run.",
            "relevant_evidence": [],
            "actor_mode": "unknown",
        }
        self.mpa_output = {
            "triggered": False,
            "should_intervene": False,
            "agenda": "MPA output will appear after the watcher hits the threshold.",
            "rationale": "Waiting for consecutive watcher positives.",
            "supporting_points": [],
            "actor_mode": "idle",
        }
        self.personality_output = {
            "triggered": False,
            "should_speak": False,
            "spoken_text": "Personality output will appear after the MPA prepares an agenda.",
            "delivery_notes": "Waiting for an MPA agenda.",
            "actor_mode": "idle",
            "event_id": "",
            "audio_generated": False,
            "audio_url": "",
            "audio_error": "",
        }
        self.last_export = {"path": "", "count": 0}
        self.capture_status = "No capture source active."
        self.vision_model = VISION_MODEL
        self.browser_name = os.getenv("BIG_BROTHER_DEMO_BROWSER", "Edge")
        self.browser_url = os.getenv("BIG_BROTHER_DEMO_URL", "https://www.google.com")
        self.last_analysis = {
            "analysisMode": "",
            "summary": "",
            "writtenFiles": {},
        }

    def snapshot(self, watcher, mpa, personality, resource_loader):
        with self.lock:
            return {
                "goal": self.goal,
                "interval_seconds": self.interval_seconds,
                "threshold": self.threshold,
                "running": self.running,
                "off_task_streak": self.off_task_streak,
                "threshold_progress": self.threshold_progress,
                "resource_revision": self.resource_revision,
                "required_reassessment_revision": self.required_reassessment_revision,
                "session_duration_seconds": self.session_duration_seconds,
                "session_deadline_at": self.session_deadline_at,
                "session_remaining_seconds": max(
                    0,
                    int(self.session_deadline_at - time.time()),
                )
                if self.running and self.session_deadline_at
                else 0,
                "post_speech_pause_seconds": self.post_speech_pause_seconds,
                "speech_grace_remaining_seconds": max(
                    0,
                    int(self.speech_grace_until - time.time()),
                )
                if self.speech_grace_until
                else 0,
                "status": self.status,
                "last_error": self.last_error,
                "last_turn_at": self.last_turn_at,
                "resources": dict(self.resources),
                "watcher_output": dict(self.watcher_output),
                "watcher_enabled": watcher.enabled,
                "watcher_model": watcher.model,
                "mpa_output": dict(self.mpa_output),
                "mpa_enabled": mpa.enabled,
                "mpa_model": mpa.model,
                "personality_output": dict(self.personality_output),
                "personality_enabled": personality.enabled,
                "personality_model": personality.model,
                "vision_model": self.vision_model,
                "capture_status": self.capture_status,
                "browser_name": self.browser_name,
                "browser_url": self.browser_url,
                "available_browsers": list(BROWSERS.keys()),
                "paths": resource_loader.describe_paths(),
                "last_export": dict(self.last_export),
                "last_analysis": dict(self.last_analysis),
            }


class WatcherDashboardApp:
    def __init__(self):
        ensure_output_dirs()
        self.state = DashboardState()
        self.resource_loader = ResourceLoader()
        self.watcher = WatcherActor()
        self.mpa = MainProcessingActor()
        self.personality = PersonalityActor()
        self.tab_reader = BrowserLiveReader(BROWSERS[self.state.browser_name])
        self.stop_event = threading.Event()
        self.worker = None
        self.pending_watcher_hits = []

    def _idle_mpa_output(self):
        return {
            "triggered": False,
            "should_intervene": False,
            "agenda": "MPA output will appear after the watcher hits the threshold.",
            "rationale": "Waiting for consecutive watcher positives.",
            "supporting_points": [],
            "actor_mode": "idle",
        }

    def _resource_record_paths(self):
        candidate_groups = [
            getattr(self.resource_loader, "webcam_candidates", []),
            getattr(self.resource_loader, "screenshare_candidates", []),
            getattr(self.resource_loader, "browser_candidates", []),
            list(MODE_TO_SUMMARY_PATH.values()),
        ]
        seen = set()
        ordered_paths = []
        for group in candidate_groups:
            for path in group:
                resolved = Path(path)
                key = str(resolved)
                if key in seen:
                    continue
                seen.add(key)
                ordered_paths.append(resolved)
        return ordered_paths

    def _clear_resource_records(self):
        for path in self._resource_record_paths():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            except OSError:
                continue

    def _clear_runtime_stats(
        self,
        *,
        preserve_running=False,
        preserve_deadline=False,
        preserve_speech_grace=False,
        clear_resource_records=False,
        status="Session stats reset.",
    ):
        if not preserve_running:
            self.state.running = False
        self.state.off_task_streak = 0
        self.state.threshold_progress = 0
        self.state.required_reassessment_revision = 0
        if not preserve_deadline:
            self.state.session_deadline_at = 0.0
        if not preserve_speech_grace:
            self.state.speech_grace_until = 0.0
        self.state.status = status
        self.state.last_error = ""
        self.state.last_turn_at = ""
        self.state.watcher_output = {
            "off_task": False,
            "confidence": 0.0,
            "summary": "Watcher output will appear here after a run.",
            "relevant_evidence": [],
            "actor_mode": "unknown",
        }
        self.state.mpa_output = self._idle_mpa_output()
        self.state.personality_output = self._idle_personality_output()
        self.pending_watcher_hits = []
        if clear_resource_records:
            self.state.required_reassessment_revision = self.state.resource_revision + 1
            self._clear_resource_records()
            self.state.resources = {
                "webcam": "Waiting for webcam resource text.",
                "screenshare": "Waiting for screenshare resource text.",
                "browser": "Waiting for browser export text.",
            }
            self.state.last_export = {"path": "", "count": 0}
            self.state.last_analysis = {
                "analysisMode": "",
                "summary": "",
                "writtenFiles": {},
            }

    def configure_browser(self, browser_name: str | None = None, browser_url: str | None = None):
        with self.state.lock:
            if browser_name:
                if browser_name not in BROWSERS:
                    raise ValueError(f"Unknown browser '{browser_name}'.")
                self.state.browser_name = browser_name
                self.tab_reader = BrowserLiveReader(BROWSERS[browser_name])
            if browser_url is not None:
                cleaned = browser_url.strip()
                self.state.browser_url = cleaned or self.state.browser_url

    def launch_browser(self, browser_name: str | None = None, browser_url: str | None = None):
        self.configure_browser(browser_name, browser_url)
        with self.state.lock:
            launch_url = self.state.browser_url
            current_browser = self.state.browser_name
        self.tab_reader.launch(launch_url)
        self._sync_browser_export(retries=6, delay_seconds=0.5)
        self._refresh_resource_debug()
        with self.state.lock:
            self.state.capture_status = f"Launched {current_browser} for browser monitoring."
            self.state.status = f"Browser launched at {launch_url}"
        return {"browser": current_browser, "url": launch_url}

    def start_monitoring(self, goal, interval_seconds, threshold, duration_seconds=None):
        with self.state.lock:
            self.state.goal = goal.strip() or self.state.goal
            self.state.interval_seconds = max(DEFAULT_TICK_SECONDS, int(interval_seconds))
            self.state.threshold = max(1, int(threshold))
            if duration_seconds is not None:
                self.state.session_duration_seconds = max(DEFAULT_TICK_SECONDS, int(duration_seconds))
            self.state.off_task_streak = 0
            self.state.threshold_progress = 0
            self.state.required_reassessment_revision = 0
            self.state.speech_grace_until = 0.0
            self.state.mpa_output = self._idle_mpa_output()
            self.state.personality_output = self._idle_personality_output()
            self.state.running = True
            self.state.session_deadline_at = time.time() + max(
                DEFAULT_TICK_SECONDS, int(self.state.session_duration_seconds)
            )
            self.state.status = "Monitoring started."
            self.state.last_error = ""
        self.pending_watcher_hits = []
        self.stop_event.clear()
        if not self.worker or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._monitor_loop, daemon=True)
            self.worker.start()

    def stop_monitoring(self):
        with self.state.lock:
            self.state.running = False
            self.state.session_deadline_at = 0.0
            self.state.speech_grace_until = 0.0
            self.state.status = "Monitoring stopped."
        self.stop_event.set()

    def reset_stats(self):
        self.stop_event.set()
        with self.state.lock:
            self._clear_runtime_stats(
                clear_resource_records=True,
                status="Session stats reset.",
            )

    def run_once(self, goal=None, interval_seconds=None, threshold=None):
        with self.state.lock:
            if goal is not None and goal.strip():
                self.state.goal = goal.strip()
            if interval_seconds is not None:
                self.state.interval_seconds = max(DEFAULT_TICK_SECONDS, int(interval_seconds))
            if threshold is not None:
                self.state.threshold = max(1, int(threshold))
            self.state.status = "Checking resources once..."
            self.state.last_error = ""
        self._run_single_turn()

    def _expire_session_if_needed(self):
        with self.state.lock:
            if not self.state.running or not self.state.session_deadline_at:
                return False
            if time.time() < self.state.session_deadline_at:
                return False
            self.state.running = False
            self.state.session_deadline_at = 0.0
            self.state.status = "Timed session complete."
        self.stop_event.set()
        return True

    def note_speech_finished(self, pause_seconds=None):
        pause_window = (
            self.state.post_speech_pause_seconds
            if pause_seconds is None
            else max(0, int(pause_seconds))
        )
        with self.state.lock:
            self.state.speech_grace_until = time.time() + pause_window if pause_window else 0.0
            self._clear_runtime_stats(
                preserve_running=True,
                preserve_deadline=True,
                preserve_speech_grace=True,
                clear_resource_records=True,
                status=(
                    f"Post-intervention pause for {pause_window} seconds."
                    if pause_window
                    else "Voice playback finished."
                ),
            )
        return pause_window

    def _speech_grace_remaining(self):
        with self.state.lock:
            grace_until = self.state.speech_grace_until
            running = self.state.running
        if not running or not grace_until:
            return 0.0
        return max(0.0, grace_until - time.time())

    def export_tabs(self):
        path, count = self._sync_browser_export(retries=4, delay_seconds=0.5)
        self._refresh_resource_debug()
        return {"path": str(path), "count": count}

    def _idle_personality_output(self):
        return {
            "triggered": False,
            "should_speak": False,
            "spoken_text": "Personality output will appear after the MPA prepares an agenda.",
            "delivery_notes": "Waiting for an MPA agenda.",
            "actor_mode": "idle",
            "event_id": "",
            "audio_generated": False,
            "audio_url": "",
            "audio_error": "",
        }

    def _sync_browser_export(self, retries=1, delay_seconds=0.25):
        path, count = self.tab_reader.export_tabs(
            TAB_OUTPUT_PATH,
            retries=retries,
            delay_seconds=delay_seconds,
        )
        with self.state.lock:
            self.state.last_export = {"path": str(path), "count": count}
            self.state.resource_revision += 1
            self.state.status = f"Exported {count} browser tabs to {path.name}."
        return path, count

    def _synthesize_personality_audio(self, spoken_text: str):
        api_key = (
            os.getenv("BIG_BROTHER_ELEVENLABS_API_KEY")
            or os.getenv("ELEVENLABS_API_KEY")
            or ""
        ).strip()
        voice_id = os.getenv("BIG_BROTHER_ELEVENLABS_VOICE_ID", "").strip()
        model_id = os.getenv("BIG_BROTHER_ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
        if not spoken_text.strip():
            return {
                "audio_generated": False,
                "audio_url": "",
                "audio_error": "No spoken text available for synthesis.",
            }
        if not api_key:
            return {
                "audio_generated": False,
                "audio_url": "",
                "audio_error": "Missing ELEVENLABS_API_KEY.",
            }
        if not voice_id:
            return {
                "audio_generated": False,
                "audio_url": "",
                "audio_error": "Missing BIG_BROTHER_ELEVENLABS_VOICE_ID.",
            }

        payload = {
            "text": spoken_text,
            "model_id": model_id,
            "voice_settings": {
                "stability": float(os.getenv("BIG_BROTHER_ELEVENLABS_STABILITY", "0.45")),
                "similarity_boost": float(os.getenv("BIG_BROTHER_ELEVENLABS_SIMILARITY", "0.8")),
            },
        }
        req = request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=90) as response:
                audio_bytes = response.read()
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            return {
                "audio_generated": False,
                "audio_url": "",
                "audio_error": f"ElevenLabs request failed: {error_body}",
            }
        except error.URLError as exc:
            return {
                "audio_generated": False,
                "audio_url": "",
                "audio_error": f"Unable to reach ElevenLabs: {exc.reason}",
            }

        PERSONALITY_AUDIO_PATH.write_bytes(audio_bytes)
        PERSONALITY_JSON_PATH.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "voice_id": voice_id,
                    "model_id": model_id,
                    "text": spoken_text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "audio_generated": True,
            "audio_url": f"/artifacts/personality-latest.mp3?ts={int(time.time())}",
            "audio_error": "",
        }

    def analyze_capture(self, analysis_mode: str, prompt: str, image_data_url: str):
        analysis_mode = analysis_mode.strip().lower()
        if analysis_mode not in MODE_TO_SOURCE_DIR:
            raise ValueError("analysisMode must be 'webcam' or 'screen'.")

        api_key = (os.getenv("BIG_BROTHER_API_KEY") or os.getenv("HACKCLUB_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("Missing API key. Set BIG_BROTHER_API_KEY in .env.")

        image_data_url = parse_data_url(image_data_url)
        request_prompt = build_vision_prompt(analysis_mode, prompt.strip())

        upstream_body = {
            "model": VISION_MODEL,
            "temperature": 0.2,
            "max_tokens": 500,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": request_prompt},
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
            raise RuntimeError(f"Hack Club API request failed.\n\n{error_body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Unable to reach Hack Club API: {exc.reason}") from exc

        try:
            message = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected response from Hack Club API: {response_data}") from exc

        written_files = write_local_outputs(analysis_mode, prompt.strip(), message)
        self._refresh_resource_debug()
        with self.state.lock:
            self.state.resource_revision += 1
            self.state.capture_status = (
                "Webcam summary updated."
                if analysis_mode == "webcam"
                else "Screenshare summary updated."
            )
            self.state.last_analysis = {
                "analysisMode": analysis_mode,
                "summary": message,
                "writtenFiles": written_files,
            }

        return {
            "summary": message,
            "model": VISION_MODEL,
            "analysisMode": analysis_mode,
            "writtenFiles": written_files,
        }

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            if self._expire_session_if_needed():
                break
            grace_remaining = self._speech_grace_remaining()
            if grace_remaining > 0:
                self.stop_event.wait(min(float(self.state.interval_seconds), grace_remaining))
                continue
            self._run_single_turn()
            if self._expire_session_if_needed():
                break
            with self.state.lock:
                interval_seconds = self.state.interval_seconds
                running = self.state.running
                deadline_at = self.state.session_deadline_at
            if not running:
                break
            remaining = max(0.0, deadline_at - time.time()) if deadline_at else float(interval_seconds)
            wait_seconds = min(float(interval_seconds), remaining) if deadline_at else float(interval_seconds)
            self.stop_event.wait(wait_seconds)

    def _refresh_resource_debug(self):
        resources = self.resource_loader.load()
        with self.state.lock:
            self.state.resources = {
                "webcam": resources.webcam_text or "No webcam resource text found.",
                "screenshare": resources.screenshare_text or "No screenshare resource text found.",
                "browser": resources.browser_text or "No browser resource text found.",
            }
        return resources

    def _run_single_turn(self):
        try:
            with self.state.lock:
                self.state.status = "Refreshing browser export..."
            self._sync_browser_export(retries=1, delay_seconds=0.2)
            with self.state.lock:
                self.state.status = "Loading resource files..."
            resources = self._refresh_resource_debug()
            with self.state.lock:
                self.state.status = "Watcher reviewing evidence..."
                goal = self.state.goal
                threshold = self.state.threshold
                current_revision = self.state.resource_revision
                required_revision = self.state.required_reassessment_revision
            if current_revision < required_revision:
                with self.state.lock:
                    self.state.watcher_output = {
                        "off_task": False,
                        "confidence": 0.0,
                        "summary": "Waiting for fresh post-reset resource updates before reassessing.",
                        "relevant_evidence": [],
                        "actor_mode": "idle",
                    }
                    self.state.mpa_output = self._idle_mpa_output()
                    self.state.personality_output = self._idle_personality_output()
                    self.state.status = "Waiting for a fresh post-reset reassessment cycle."
                    self.state.last_error = ""
                return
            decision = self.watcher.evaluate(goal, resources)
            turn_time = time.strftime("%Y-%m-%d %H:%M:%S")
            if decision.off_task:
                self.pending_watcher_hits.append(decision)
                self.pending_watcher_hits = self.pending_watcher_hits[-max(1, threshold):]
            else:
                self.pending_watcher_hits = []

            if len(self.pending_watcher_hits) >= threshold:
                mpa_result = self.mpa.evaluate(goal, list(self.pending_watcher_hits))
            else:
                turns_left = max(0, threshold - len(self.pending_watcher_hits))
                mpa_result = {
                    "triggered": False,
                    "should_intervene": False,
                    "agenda": "MPA output will appear after the watcher hits the threshold.",
                    "rationale": (
                        "Waiting for consecutive watcher positives."
                        if turns_left
                        else "Watcher threshold met."
                    ),
                    "supporting_points": [],
                    "actor_mode": "idle",
                }
            if isinstance(mpa_result, dict) or not mpa_result.triggered or not mpa_result.should_intervene:
                personality_output = self._idle_personality_output()
            else:
                personality_result = self.personality.evaluate(goal, mpa_result, list(self.pending_watcher_hits))
                personality_output = {
                    "triggered": personality_result.triggered,
                    "should_speak": personality_result.should_speak,
                    "spoken_text": personality_result.spoken_text,
                    "delivery_notes": personality_result.delivery_notes,
                    "actor_mode": personality_result.actor_mode,
                    "event_id": turn_time,
                    "audio_generated": False,
                    "audio_url": "",
                    "audio_error": "",
                }
                if personality_result.should_speak:
                    personality_output.update(self._synthesize_personality_audio(personality_result.spoken_text))
            with self.state.lock:
                if decision.off_task:
                    self.state.off_task_streak += 1
                    self.state.threshold_progress = len(self.pending_watcher_hits)
                    state_label = "Off task"
                else:
                    self.state.off_task_streak = 0
                    self.state.threshold_progress = 0
                    state_label = "On task"
                self.state.watcher_output = {
                    "off_task": decision.off_task,
                    "confidence": decision.confidence,
                    "summary": decision.summary,
                    "relevant_evidence": list(decision.relevant_evidence),
                    "actor_mode": decision.actor_mode,
                }
                self.state.required_reassessment_revision = 0
                if isinstance(mpa_result, dict):
                    self.state.mpa_output = dict(mpa_result)
                else:
                    self.state.mpa_output = {
                        "triggered": mpa_result.triggered,
                        "should_intervene": mpa_result.should_intervene,
                        "agenda": mpa_result.agenda,
                        "rationale": mpa_result.rationale,
                        "supporting_points": list(mpa_result.supporting_points),
                        "actor_mode": mpa_result.actor_mode,
                    }
                self.state.personality_output = dict(personality_output)
                if self.state.personality_output["triggered"] and self.state.personality_output["should_speak"]:
                    self.state.status = (
                        f"{state_label} ({decision.confidence:.0%}). "
                        f"Personality line ready: {self.state.personality_output['spoken_text']}"
                    )
                elif self.state.mpa_output["triggered"] and self.state.mpa_output["should_intervene"]:
                    self.state.status = (
                        f"{state_label} ({decision.confidence:.0%}). "
                        f"MPA agenda ready: {self.state.mpa_output['agenda']}"
                    )
                else:
                    self.state.status = f"{state_label} ({decision.confidence:.0%}): {decision.summary}"
                self.state.last_turn_at = turn_time
                self.state.last_error = ""
        except Exception as exc:
            with self.state.lock:
                self.state.last_error = str(exc)
                self.state.status = f"Error: {exc}"


APP = WatcherDashboardApp()


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if parsed.path == "/app.js":
            return self._serve_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
        if parsed.path == "/styles.css":
            return self._serve_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
        if parsed.path == "/artifacts/personality-latest.mp3":
            return self._serve_file(PERSONALITY_AUDIO_PATH, "audio/mpeg")
        if parsed.path == "/api/state":
            return self._json_response(APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader))
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self._read_json_body()
        try:
            if parsed.path == "/api/run-once":
                APP.run_once(
                    goal=payload.get("goal"),
                    interval_seconds=payload.get("interval_seconds", APP.state.interval_seconds),
                    threshold=payload.get("threshold", APP.state.threshold),
                )
                return self._json_response({"ok": True, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/start":
                APP.start_monitoring(
                    goal=payload.get("goal", APP.state.goal),
                    interval_seconds=payload.get("interval_seconds", APP.state.interval_seconds),
                    threshold=payload.get("threshold", APP.state.threshold),
                    duration_seconds=payload.get("duration_seconds", APP.state.session_duration_seconds),
                )
                return self._json_response({"ok": True, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/stop":
                APP.stop_monitoring()
                return self._json_response({"ok": True, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/reset-stats":
                APP.reset_stats()
                return self._json_response({"ok": True, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/speech-finished":
                APP.note_speech_finished(payload.get("pause_seconds"))
                return self._json_response({"ok": True, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/export-tabs":
                APP.configure_browser(
                    browser_name=payload.get("browser_name"),
                    browser_url=payload.get("browser_url"),
                )
                export_info = APP.export_tabs()
                return self._json_response({"ok": True, "export": export_info, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/launch-browser":
                launch_info = APP.launch_browser(
                    browser_name=payload.get("browser_name"),
                    browser_url=payload.get("browser_url"),
                )
                return self._json_response({"ok": True, "launch": launch_info, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})

            if parsed.path == "/api/analyze":
                result = APP.analyze_capture(
                    analysis_mode=payload.get("analysisMode", ""),
                    prompt=payload.get("prompt", ""),
                    image_data_url=payload.get("imageDataUrl", ""),
                )
                return self._json_response({"ok": True, **result, "state": APP.state.snapshot(APP.watcher, APP.mpa, APP.personality, APP.resource_loader)})
        except ValueError as exc:
            return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format, *args):
        return

    def _serve_file(self, path, content_type):
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))


def main():
    host = "127.0.0.1"
    port = 8765
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Big Brother web app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.stop_monitoring()
        server.server_close()


if __name__ == "__main__":
    main()
