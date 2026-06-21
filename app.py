import base64
import hashlib
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

from actors import AgentActor, PersonalityActor
from agent_core import (
    AgentMemory,
    ClientActionQueue,
    ContextFiles,
    StatusFile,
    StimulusBus,
    TodoList,
    TokenLedger,
    estimate_image_tokens,
    estimate_text_tokens,
)
from browser_live_demo import BROWSERS, TAB_OUTPUT_PATH, BrowserLiveReader
from config import env_int, load_env_file
from orchestrator import AgentOrchestrator
from resources import ResourceLoader


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "webapp"
SOURCES_DIR = APP_DIR / "sources"
SUMMARIES_DIR = APP_DIR / "summaries"
STATE_DIR = APP_DIR / "state"
PERSONALITY_AUDIO_PATH = SUMMARIES_DIR / "personality_latest.mp3"
PERSONALITY_JSON_PATH = SUMMARIES_DIR / "personality_latest.json"
DEBUG_LOG_PATH = STATE_DIR / "debug_events.jsonl"
TAB_RECORDS_DIR = SOURCES_DIR / "browser" / "tab_records"
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
DEFAULT_POST_INTERVENTION_COOLDOWN_SECONDS = 15
MAX_DEBUG_EVENTS = 160
DEFAULT_POST_SPEECH_PAUSE_SECONDS = 5
MAX_TURN_HISTORY = 24


def ensure_output_dirs():
    for path in MODE_TO_SOURCE_DIR.values():
        path.mkdir(parents=True, exist_ok=True)
    (SOURCES_DIR / "browser").mkdir(parents=True, exist_ok=True)
    TAB_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


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


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


VOLATILE_LINE_PREFIXES = ("created:", "updated:", "exported", "timestamp")


def evidence_fingerprint(goal: str, prompt_text: str) -> str:
    """Hash of the evidence with volatile timestamp lines stripped, so that
    re-exports of identical content do not defeat the watcher gate."""
    stable_lines = [
        line
        for line in str(prompt_text or "").splitlines()
        if not line.strip().lower().startswith(VOLATILE_LINE_PREFIXES)
    ]
    digest_input = f"{goal}\n" + "\n".join(stable_lines)
    return hashlib.sha1(digest_input.encode("utf-8")).hexdigest()


def make_actor_stage(key: str, label: str, model: str = "") -> dict:
    return {
        "key": key,
        "label": label,
        "model": model,
        "status": "idle",
        "detail": "Waiting for work.",
        "updated_at": "",
        "updated_at_unix": 0.0,
        "last_output_at": "",
        "last_output_preview": "",
        "version": 0,
    }


def shorten_text(value: str, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def snapshot_resources_payload(resources) -> dict:
    return {
        "webcam_text": resources.webcam_text,
        "screenshare_text": resources.screenshare_text,
        "browser_text": resources.browser_text,
        "missing_sources": list(resources.missing_sources),
        "source_metadata": dict(resources.source_metadata),
        "prompt_text": resources.as_prompt_text(),
    }


class FrozenTurnResources:
    def __init__(self, payload: dict):
        self.webcam_text = str(payload.get("webcam_text", ""))
        self.screenshare_text = str(payload.get("screenshare_text", ""))
        self.browser_text = str(payload.get("browser_text", ""))
        self.missing_sources = list(payload.get("missing_sources", []))
        self.source_metadata = dict(payload.get("source_metadata", {}))
        self._prompt_text = str(payload.get("prompt_text", ""))

    def iter_sources(self, include_stale=False):
        pairs = [
            ("webcam", self.webcam_text),
            ("screenshare", self.screenshare_text),
            ("browser", self.browser_text),
        ]
        visible_pairs = []
        for name, text in pairs:
            if not text:
                continue
            metadata = self.source_metadata.get(name, {})
            if metadata.get("stale") and not include_stale:
                continue
            visible_pairs.append((name, text))
        return visible_pairs

    def as_prompt_text(self):
        return self._prompt_text


class DashboardState:
    def __init__(self):
        self.lock = threading.RLock()
        self.goal = "I am studying calculus"
        self.interval_seconds = env_int("BIG_BROTHER_INTERVAL_SECONDS", DEFAULT_TICK_SECONDS)
        self.threshold = env_int("BIG_BROTHER_OFF_TASK_THRESHOLD", DEFAULT_OFF_TASK_THRESHOLD)
        self.post_intervention_cooldown_seconds = env_int(
            "BIG_BROTHER_POST_SPEECH_PAUSE_SECONDS",
            DEFAULT_POST_INTERVENTION_COOLDOWN_SECONDS,
        )
        self.running = False
        self.off_task_streak = 0
        self.threshold_progress = 0
        self.cooldown_until = 0.0
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
            "agenda": "Planner output will appear after the agent evaluates the current evidence.",
            "rationale": "Waiting for the next agent turn.",
            "supporting_points": [],
            "actor_mode": "idle",
        }
        self.personality_output = {
            "triggered": False,
            "should_speak": False,
            "spoken_text": "Response output will appear after the agent decides a spoken response is needed.",
            "delivery_notes": "Waiting for a response-worthy agent plan.",
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
        self.last_turn_snapshot = {}
        self.turn_history = []
        self.turns_completed = 0
        self.cycle_status = "Idle."
        self.debug_events = []
        self.event_sequence = 0
        self.actor_stages = {
            "watcher": make_actor_stage("watcher", "Agent"),
            "mpa": make_actor_stage("mpa", "Planner"),
            "personality": make_actor_stage("personality", "Response"),
        }

    def snapshot(self, watcher, mpa, personality, resource_loader):
        with self.lock:
            return {
                "goal": self.goal,
                "interval_seconds": self.interval_seconds,
                "threshold": self.threshold,
                "post_intervention_cooldown_seconds": self.post_intervention_cooldown_seconds,
                "running": self.running,
                "off_task_streak": self.off_task_streak,
                "threshold_progress": self.threshold_progress,
                "cooldown_until": self.cooldown_until,
                "cooldown_remaining_seconds": max(
                    0,
                    int(self.cooldown_until - time.time()),
                )
                if self.cooldown_until
                else 0,
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
                "last_turn_snapshot": dict(self.last_turn_snapshot),
                "turn_history": list(self.turn_history),
                "turns_completed": self.turns_completed,
                "cycle_status": self.cycle_status,
                "actor_stages": {
                    key: dict(value) for key, value in self.actor_stages.items()
                },
                "debug_events": list(self.debug_events),
                "debug_log_path": str(DEBUG_LOG_PATH.relative_to(APP_DIR)),
                "snapshot_at": now_iso(),
            }


class WatcherDashboardApp:
    def __init__(self, start_orchestrator=True):
        ensure_output_dirs()
        self.state = DashboardState()
        self.resource_loader = ResourceLoader()
        self.ledger = TokenLedger()
        self.memory = AgentMemory()
        self.status_file = StatusFile()
        self.todos = TodoList()
        self.context_files = ContextFiles()
        self.client_actions = ClientActionQueue()
        self.bus = StimulusBus()
        self.watcher = AgentActor(ledger=self.ledger)
        self.mpa = None
        self.personality = PersonalityActor(ledger=self.ledger)
        self.tab_reader = BrowserLiveReader(BROWSERS[self.state.browser_name])
        self.stop_event = threading.Event()
        self.worker = None
        self.turn_counter = 0
        self._capture_hashes = {}
        self._capture_cache = {}
        self._last_agent_fingerprint = ""
        self._last_agent_plan = None
        self.orchestrator = AgentOrchestrator(self)
        if start_orchestrator:
            self.orchestrator.start()
        self._set_actor_stage("watcher", "idle", "Agent ready.", model=self.watcher.model)
        self._set_actor_stage("mpa", "idle", "Planner ready.", model=self.watcher.model)
        self._set_actor_stage("personality", "idle", "Response actor ready.", model=self.personality.model)
        self._log_event("system", "startup", "Debug instrumentation initialized.", {
            "agent_model": self.watcher.model,
            "personality_model": self.personality.model,
            "vision_model": VISION_MODEL,
        })

    def snapshot(self):
        mpa_proxy = self.mpa or self.watcher
        snap = self.state.snapshot(self.watcher, mpa_proxy, self.personality, self.resource_loader)
        snap["agent"] = {
            "status": self.status_file.get(),
            "todos": self.todos.list_all(),
            "memory_recent": self.memory.recent(10),
            "context_current": self.context_files.get_current(),
            "context_history": self.context_files.recent_history(8),
            "pending_actions": self.client_actions.pending(),
            "last_stimulus": dict(self.bus.last_stimulus),
            "token_ledger": self.ledger.snapshot(),
            "heartbeat_seconds": self.orchestrator.heartbeat_seconds,
        }
        return snap

    def _next_turn_id(self):
        with self.state.lock:
            self.turn_counter += 1
            return self.turn_counter

    def _log_event(self, component: str, phase: str, message: str, payload=None):
        ensure_output_dirs()
        event = {
            "timestamp": now_iso(),
            "component": component,
            "phase": phase,
            "message": message,
        }
        if payload is not None:
            event["payload"] = payload

        with self.state.lock:
            self.state.event_sequence += 1
            event["seq"] = self.state.event_sequence
            self.state.debug_events.append(event)
            self.state.debug_events = self.state.debug_events[-MAX_DEBUG_EVENTS:]

        try:
            with DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

        return event

    def _set_actor_stage(self, actor_key: str, status: str, detail: str, *, output_preview: str = "", model: str | None = None):
        updated_at = now_iso()
        updated_at_unix = time.time()
        with self.state.lock:
            stage = self.state.actor_stages.setdefault(
                actor_key,
                make_actor_stage(actor_key, actor_key.title(), model or ""),
            )
            next_model = model if model is not None else stage.get("model", "")
            next_output_preview = shorten_text(output_preview, 240) if output_preview else stage.get("last_output_preview", "")
            changed = (
                stage.get("status") != status
                or stage.get("detail") != detail
                or stage.get("model") != next_model
                or stage.get("last_output_preview", "") != next_output_preview
            )
            if not changed:
                return stage.get("updated_at", "")
            if model is not None:
                stage["model"] = model
            stage["status"] = status
            stage["detail"] = detail
            stage["updated_at"] = updated_at
            stage["updated_at_unix"] = updated_at_unix
            stage["version"] = int(stage.get("version", 0)) + 1
            if output_preview:
                stage["last_output_at"] = updated_at
                stage["last_output_preview"] = next_output_preview
        return updated_at

    def _resource_max_age_seconds(self):
        with self.state.lock:
            interval_seconds = self.state.interval_seconds
        return max(10, int(interval_seconds) * 2 + 2)

    def _idle_mpa_output(self):
        return {
            "triggered": False,
            "should_intervene": False,
            "agenda": "Planner output will appear after the agent evaluates the current evidence.",
            "rationale": "Waiting for the next agent turn.",
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
        preserve_turn_history=False,
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
        if not preserve_turn_history:
            self.state.last_turn_snapshot = {}
            self.state.turn_history = []
            self.state.turns_completed = 0
        self.state.cycle_status = "Idle."

    def _record_turn_snapshot(self, turn_snapshot: dict):
        compact = {
            "turn_id": turn_snapshot.get("turn_id"),
            "created_at": turn_snapshot.get("created_at", ""),
            "reason": turn_snapshot.get("reason", ""),
            "goal": turn_snapshot.get("goal", ""),
            "resource_revision": turn_snapshot.get("resource_revision", 0),
            "available_sources": list(turn_snapshot.get("available_sources", [])),
            "missing_sources": list(turn_snapshot.get("missing_sources", [])),
            "prompt_text": turn_snapshot.get("prompt_text", ""),
        }
        with self.state.lock:
            self.state.last_turn_snapshot = dict(turn_snapshot)
            self.state.turn_history.append(compact)
            self.state.turn_history = self.state.turn_history[-MAX_TURN_HISTORY:]

    def _collect_turn_snapshot(self, *, reason: str):
        turn_id = self._next_turn_id()
        self._log_event("turn", "collecting", "Collecting deliberate turn snapshot.", {
            "turn_id": turn_id,
            "reason": reason,
        })
        with self.state.lock:
            self.state.cycle_status = f"Collecting turn {turn_id} snapshot..."
            self.state.status = f"Collecting turn {turn_id} snapshot..."

        self._sync_browser_export(retries=1, delay_seconds=0.2)
        resources = self._refresh_resource_debug()
        with self.state.lock:
            snapshot = {
                "turn_id": turn_id,
                "created_at": now_iso(),
                "reason": reason,
                "goal": self.state.goal,
                "threshold": self.state.threshold,
                "resource_revision": self.state.resource_revision,
                "browser_export": dict(self.state.last_export),
                "last_stimulus": dict(self.bus.last_stimulus),
                "resources": snapshot_resources_payload(resources),
                "available_sources": [name for name, _ in resources.iter_sources(include_stale=False)],
                "missing_sources": list(resources.missing_sources),
                "prompt_text": resources.as_prompt_text(),
                "last_analysis": dict(self.state.last_analysis),
            }
            self.state.cycle_status = f"Turn {turn_id} snapshot frozen."

        self._record_turn_snapshot(snapshot)
        self._log_event("turn", "snapshot_frozen", "Deliberate turn snapshot frozen.", snapshot)
        return snapshot

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
        self._log_event("browser", "launch_start", "Launching tracked browser.", {
            "browser": current_browser,
            "url": launch_url,
        })
        self.tab_reader.launch(launch_url)
        self._sync_browser_export(retries=6, delay_seconds=0.5)
        self._refresh_resource_debug()
        with self.state.lock:
            self.state.capture_status = f"Launched {current_browser} for browser monitoring."
            self.state.status = f"Browser launched at {launch_url}"
        self._log_event("browser", "launch_complete", "Tracked browser launched.", {
            "browser": current_browser,
            "url": launch_url,
        })
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
            self.state.cooldown_until = 0.0
            self.state.required_reassessment_revision = 0
            self.state.speech_grace_until = 0.0
            self.state.mpa_output = self._idle_mpa_output()
            self.state.personality_output = self._idle_personality_output()
            self.state.running = True
            self.state.session_deadline_at = time.time() + max(
                DEFAULT_TICK_SECONDS, int(self.state.session_duration_seconds)
            )
            self.state.status = "Monitoring active. Waiting for the next deliberate cycle."
            self.state.cycle_status = "Armed and waiting for the next cycle."
            self.state.last_error = ""
        self._log_event("session", "start", "Monitoring session started.", {
            "goal": self.state.goal,
            "interval_seconds": self.state.interval_seconds,
            "threshold": self.state.threshold,
            "duration_seconds": self.state.session_duration_seconds,
            "cooldown_seconds": self.state.post_intervention_cooldown_seconds,
        })
        self._last_agent_fingerprint = ""
        self._last_agent_plan = None
        self.stop_event.clear()
        self.orchestrator.note_session_started()
        if not self.worker or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._monitor_loop, daemon=True)
            self.worker.start()

    def stop_monitoring(self):
        with self.state.lock:
            self.state.running = False
            self.state.session_deadline_at = 0.0
            self.state.cooldown_until = 0.0
            self.state.speech_grace_until = 0.0
            self.state.status = "Monitoring stopped."
        self._set_actor_stage("watcher", "idle", "Watcher stopped.")
        self._set_actor_stage("mpa", "idle", "MPA stopped.")
        self._set_actor_stage("personality", "idle", "Personality stopped.")
        self._log_event("session", "stop", "Monitoring session stopped.")
        self.orchestrator.note_session_stopped()
        self.stop_event.set()

    def reset_stats(self):
        self.stop_event.set()
        with self.state.lock:
            self._clear_runtime_stats(
                clear_resource_records=True,
                status="Session stats reset.",
            )
            self.state.cooldown_until = 0.0
        self._set_actor_stage("watcher", "idle", "Watcher reset.")
        self._set_actor_stage("mpa", "idle", "MPA reset.")
        self._set_actor_stage("personality", "idle", "Personality reset.")
        self._log_event("session", "reset", "Session stats reset.")

    def run_once(self, goal=None, interval_seconds=None, threshold=None, reason="manual_run"):
        with self.state.lock:
            if goal is not None and goal.strip():
                self.state.goal = goal.strip()
            if interval_seconds is not None:
                self.state.interval_seconds = max(DEFAULT_TICK_SECONDS, int(interval_seconds))
            if threshold is not None:
                self.state.threshold = max(1, int(threshold))
            self.state.status = "Running one deliberate cycle..."
            self.state.cycle_status = "Preparing one deliberate cycle..."
            self.state.last_error = ""
        turn_snapshot = self._collect_turn_snapshot(reason=reason)
        self._evaluate_turn_snapshot(turn_snapshot)

    def run_turn(self, reason="agent_stimulus"):
        """Orchestrator-driven deliberate turn using current session settings."""
        with self.state.lock:
            self.state.status = f"Running agent turn ({reason})..."
            self.state.cycle_status = f"Agent turn pending ({reason})."
        turn_snapshot = self._collect_turn_snapshot(reason=reason)
        self._evaluate_turn_snapshot(turn_snapshot)

    def ingest_stimulus(self, stimulus_type: str, payload: dict | None = None):
        """Client-reported stimuli (inactivity, activity, frame_unchanged, manual)."""
        stimulus_type = str(stimulus_type or "").strip()
        allowed = {"inactivity", "activity", "frame_unchanged", "manual"}
        if stimulus_type not in allowed:
            raise ValueError(f"Unknown stimulus type '{stimulus_type}'.")
        payload = dict(payload or {})

        if stimulus_type == "frame_unchanged":
            # Zero-token freshness refresh for an unchanged client frame.
            mode = str(payload.get("mode", "")).strip().lower()
            cached = self._capture_cache.get(mode)
            if mode in MODE_TO_SOURCE_DIR and cached:
                write_local_outputs(mode, "", cached)
                self.ledger.record_skip(
                    f"vlm:{mode}",
                    estimate_image_tokens(
                        width=int(payload.get("width", 0) or 0),
                        height=int(payload.get("height", 0) or 0),
                    )
                    + 400,
                )
                with self.state.lock:
                    self.state.resource_revision += 1
        accepted = self.bus.emit(stimulus_type, payload)
        return {"accepted": accepted, "type": stimulus_type}

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
                preserve_turn_history=True,
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
        self._log_event("browser", "export_start", "Refreshing browser tab export.")
        path, count = self._sync_browser_export(retries=4, delay_seconds=0.5)
        self._refresh_resource_debug()
        self._log_event("browser", "export_complete", "Browser tab export refreshed.", {
            "path": str(path),
            "count": count,
        })
        return {"path": str(path), "count": count}

    def _idle_personality_output(self):
        return {
            "triggered": False,
            "should_speak": False,
            "spoken_text": "Response output will appear after the agent decides a spoken response is needed.",
            "delivery_notes": "Waiting for a response-worthy agent plan.",
            "actor_mode": "idle",
            "event_id": "",
            "audio_generated": False,
            "audio_url": "",
            "audio_error": "",
        }

    def _sync_browser_export(self, retries=1, delay_seconds=0.25):
        tabs = self.tab_reader.read_tabs(retries=retries, delay_seconds=delay_seconds)
        path, count = self.tab_reader.export_tabs(
            TAB_OUTPUT_PATH,
            retries=retries,
            delay_seconds=delay_seconds,
        )
        self.tab_reader.write_index(retries=retries, delay_seconds=delay_seconds)
        self.tab_reader.write_summary(retries=retries, delay_seconds=delay_seconds)
        tab_records = self.tab_reader.sync_tab_records(tabs=tabs)
        with self.state.lock:
            self.state.last_export = {
                "path": str(path),
                "count": count,
                "exported_at": now_iso(),
                "tab_records": len(tab_records),
            }
            self.state.resource_revision += 1
            self.state.status = f"Exported {count} browser tabs to {path.name}."
        self.context_files.write_snapshot(
            f"Browser export refreshed with {count} tab(s).",
            meta={
                "open_tab_ids": [str(tab.get("id", "")) for tab in tabs if tab.get("id")],
                "resource_summary": {"browser_tab_count": count},
            },
        )
        return path, count

    def _save_tab_record_image(self, image_data_url: str, tab_id: str):
        tab_id = str(tab_id or "").strip()
        if not tab_id or "," not in image_data_url:
            return ""
        _, encoded = image_data_url.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        path = TAB_RECORDS_DIR / f"{tab_id}.jpg"
        path.write_bytes(image_bytes)
        return str(path.relative_to(APP_DIR))

    def _queue_requested_resource(self, request_spec: dict):
        request_type = str(request_spec.get("type", "")).strip()
        if not request_type:
            return {}
        type_map = {
            "screen_scan": "screen_scan",
            "webcam_scan": "webcam_scan",
            "browser_rag": "browser_rag",
            "browser_scan": "browser_rag",
        }
        action_type = type_map.get(request_type, request_type)
        dedupe_key = request_spec.get("dedupe_key") or f"{action_type}:{request_spec.get('source', '')}:{request_spec.get('reason', '')}"
        action = self.client_actions.enqueue(action_type, request_spec, dedupe_key=dedupe_key)
        self._log_event("agent", "resource_requested", "Agent queued a client resource action.", action)
        return action

    def complete_client_action(self, action_id: str, result: dict | None = None):
        completed = self.client_actions.complete(action_id, result=result)
        if completed:
            self.memory.append("client_action", f"Completed client action {completed.get('type')}.", meta=completed)
        return completed

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

    def analyze_capture(self, analysis_mode: str, prompt: str, image_data_url: str, metadata: dict | None = None):
        analysis_mode = analysis_mode.strip().lower()
        if analysis_mode not in MODE_TO_SOURCE_DIR:
            raise ValueError("analysisMode must be 'webcam' or 'screen'.")
        metadata = dict(metadata or {})
        self._log_event("capture", "analyze_start", "Capture analysis requested.", {
            "analysis_mode": analysis_mode,
            "prompt": prompt.strip(),
            "metadata": metadata,
        })

        api_key = (os.getenv("BIG_BROTHER_API_KEY") or os.getenv("HACKCLUB_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("Missing API key. Set BIG_BROTHER_API_KEY in .env.")

        image_data_url = parse_data_url(image_data_url)
        request_prompt = build_vision_prompt(analysis_mode, prompt.strip())

        # VLM dedupe: an identical frame returns the cached summary with zero
        # tokens, refreshing the resource files so freshness tracking holds.
        image_hash = hashlib.sha1(image_data_url.encode("utf-8")).hexdigest()
        cached_summary = self._capture_cache.get(analysis_mode)
        if cached_summary and self._capture_hashes.get(analysis_mode) == image_hash:
            written_files = write_local_outputs(analysis_mode, prompt.strip(), cached_summary)
            tab_record_image = ""
            if analysis_mode == "screen" and metadata.get("tab_id"):
                tab_record_image = self._save_tab_record_image(image_data_url, metadata.get("tab_id"))
            self.ledger.record_skip(
                f"vlm:{analysis_mode}",
                estimate_image_tokens(base64_length=len(image_data_url))
                + estimate_text_tokens(request_prompt)
                + estimate_text_tokens(cached_summary),
            )
            self._refresh_resource_debug()
            with self.state.lock:
                self.state.resource_revision += 1
            self._log_event("capture", "analyze_cached", "Identical frame; VLM call skipped.", {
                "analysis_mode": analysis_mode,
            })
            return {
                "summary": cached_summary,
                "model": VISION_MODEL,
                "analysisMode": analysis_mode,
                "writtenFiles": written_files,
                "cached": True,
                "tabRecordImage": tab_record_image,
            }

        upstream_body = {
            "model": VISION_MODEL,
            "temperature": 0.2,
            "max_tokens": 300,
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

        usage = response_data.get("usage") or {}
        self.ledger.record_call(
            f"vlm:{analysis_mode}",
            usage.get("prompt_tokens")
            or estimate_image_tokens(base64_length=len(image_data_url))
            + estimate_text_tokens(request_prompt),
            usage.get("completion_tokens") or estimate_text_tokens(message),
        )
        summary_changed = self._capture_cache.get(analysis_mode) != message
        self._capture_hashes[analysis_mode] = image_hash
        self._capture_cache[analysis_mode] = message

        written_files = write_local_outputs(analysis_mode, prompt.strip(), message)
        tab_record_image = ""
        if analysis_mode == "screen" and metadata.get("tab_id"):
            tab_record_image = self._save_tab_record_image(image_data_url, metadata.get("tab_id"))
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
        self._log_event("capture", "analyze_complete", "Capture analysis complete.", {
            "analysis_mode": analysis_mode,
            "summary": message,
            "written_files": written_files,
            "tab_record_image": tab_record_image,
        })
        if summary_changed:
            self.memory.append("observation", f"{analysis_mode} summary updated.", meta={"summary": message[:300]})
            self.bus.emit("capture_updated", {"analysis_mode": analysis_mode})

        return {
            "summary": message,
            "model": VISION_MODEL,
            "analysisMode": analysis_mode,
            "writtenFiles": written_files,
            "tabRecordImage": tab_record_image,
        }

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            if self._expire_session_if_needed():
                break
            grace_remaining = self._speech_grace_remaining()
            if grace_remaining > 0:
                self.stop_event.wait(min(float(self.state.interval_seconds), grace_remaining))
                continue
            with self.state.lock:
                interval_seconds = self.state.interval_seconds
                running = self.state.running
                deadline_at = self.state.session_deadline_at
                self.state.cycle_status = "Waiting for the next deliberate cycle."
            if not running:
                break
            remaining = max(0.0, deadline_at - time.time()) if deadline_at else float(interval_seconds)
            wait_seconds = min(float(interval_seconds), remaining) if deadline_at else float(interval_seconds)
            self.stop_event.wait(wait_seconds)

    def _refresh_resource_debug(self):
        resources = self.resource_loader.load(max_age_seconds=self._resource_max_age_seconds())
        with self.state.lock:
            self.state.resources = {
                "webcam": resources.describe_source("webcam") or "No webcam resource text found.",
                "screenshare": resources.describe_source("screenshare") or "No screenshare resource text found.",
                "browser": resources.describe_source("browser") or "No browser resource text found.",
            }
        self._log_event("resources", "refresh", "Loaded current watcher resources.", {
            "webcam": resources.describe_source("webcam"),
            "screenshare": resources.describe_source("screenshare"),
            "browser": resources.describe_source("browser"),
            "prompt_text": resources.as_prompt_text(),
            "source_metadata": resources.source_metadata,
            "missing_sources": resources.missing_sources,
        })
        return resources

    def _evaluate_turn_snapshot(self, turn_snapshot: dict):
        turn_id = int(turn_snapshot.get("turn_id", self._next_turn_id()))
        try:
            with self.state.lock:
                self.state.status = f"Evaluating turn {turn_id} snapshot..."
                self.state.cycle_status = f"Evaluating turn {turn_id} snapshot..."
                cooldown_remaining = max(0.0, self.state.cooldown_until - time.time())
                if cooldown_remaining > 0:
                    self._set_actor_stage(
                        "watcher",
                        "cooldown",
                        f"Response cooldown active for {cooldown_remaining:.1f}s.",
                    )
                    self._set_actor_stage("mpa", "idle", "Planner waiting during response cooldown.")
                    self._set_actor_stage("personality", "idle", "Response actor waiting during cooldown.")
                    self.state.watcher_output = {
                        "off_task": False,
                        "confidence": 0.0,
                        "summary": (
                            f"Agent paused for cooldown ({cooldown_remaining:.1f}s remaining). "
                            "Resources may keep updating, but no new response will be prepared yet."
                        ),
                        "relevant_evidence": [],
                        "actor_mode": "cooldown",
                    }
                    self.state.mpa_output = self._idle_mpa_output()
                    self.state.personality_output = self._idle_personality_output()
                    self.state.status = (
                        f"Cooldown active for {cooldown_remaining:.1f}s. "
                        "Waiting before evaluating new signals."
                    )
                    self.state.last_error = ""
                    self.state.last_turn_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._log_event("agent", "cooldown", "Agent turn skipped due to cooldown.", {
                        "turn_id": turn_id,
                        "cooldown_remaining_seconds": round(cooldown_remaining, 2),
                    })
                    self._log_event("turn", "complete", "Agent turn ended during cooldown.", {
                        "turn_id": turn_id,
                    })
                    return
                self.state.status = "Agent reviewing current context..."
                self.state.cycle_status = f"Agent evaluating turn {turn_id}."
                goal = self.state.goal
                cooldown_seconds = max(0, self.state.post_intervention_cooldown_seconds)
                current_revision = int(turn_snapshot.get("resource_revision", self.state.resource_revision))
                required_revision = self.state.required_reassessment_revision
            resources = FrozenTurnResources(dict(turn_snapshot.get("resources", {})))
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
                    self.state.last_turn_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._set_actor_stage("watcher", "idle", "Waiting for fresh post-reset resource updates.")
                self._set_actor_stage("mpa", "idle", "Planner waiting for reassessment gate.")
                self._set_actor_stage("personality", "idle", "Response actor waiting for reassessment gate.")
                self._log_event("agent", "idle", "Agent blocked pending fresh reassessment resources.", {
                    "turn_id": turn_id,
                    "resource_revision": current_revision,
                    "required_reassessment_revision": required_revision,
                })
                self._log_event("turn", "complete", "Agent turn skipped pending reassessment resources.", {
                    "turn_id": turn_id,
                })
                return
            # Fingerprint gate: identical goal + frozen resource text means no
            # new evidence, so the agent call is skipped entirely.
            prompt_text = str(turn_snapshot.get("prompt_text", ""))
            fingerprint = evidence_fingerprint(goal, prompt_text)
            if (
                fingerprint == self._last_agent_fingerprint
                and self._last_agent_plan is not None
            ):
                self.ledger.record_skip(
                    "agent",
                    estimate_text_tokens(prompt_text) + 400,
                )
                with self.state.lock:
                    self.state.status = "No new evidence since the last turn; agent call skipped."
                    self.state.cycle_status = f"Turn {turn_id} skipped (unchanged evidence)."
                    self.state.last_turn_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.state.last_error = ""
                self._set_actor_stage("watcher", "idle", "Skipped: evidence unchanged since last turn.")
                self._log_event("agent", "cached", "Agent skipped: unchanged evidence fingerprint.", {
                    "turn_id": turn_id,
                    "fingerprint": fingerprint[:12],
                })
                self._log_event("turn", "complete", "Turn skipped with unchanged evidence.", {
                    "turn_id": turn_id,
                })
                return
            stimulus_type = str(turn_snapshot.get("reason", "")).strip()
            current_context = self.context_files.get_current()
            historic_context = self.context_files.recent_history(8)
            self._set_actor_stage("watcher", "reading", "Agent reading current context and resources.")
            self._log_event("agent", "reading", "Agent received fresh resources.", {
                "turn_id": turn_id,
                "goal": goal,
                "resources_prompt": turn_snapshot.get("prompt_text", ""),
                "snapshot_reason": turn_snapshot.get("reason", ""),
            })
            self._set_actor_stage("watcher", "processing", "Agent is deciding whether the evidence is sufficient.")
            self._log_event("agent", "processing", "Agent model evaluation started.", {
                "turn_id": turn_id,
                "model": self.watcher.model,
            })
            decision = self.watcher.evaluate(
                goal,
                resources,
                stimulus_type=stimulus_type,
                current_context=current_context,
                historic_context=historic_context,
            )
            self._last_agent_fingerprint = fingerprint
            self._last_agent_plan = decision
            turn_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self._set_actor_stage(
                "watcher",
                "writing",
                "Agent wrote its situation assessment.",
                output_preview=decision.summary,
            )
            self._log_event("agent", "writing", "Agent plan received.", {
                "turn_id": turn_id,
                "output": {
                    "sufficient": decision.sufficient,
                    "focus_state": decision.focus_state,
                    "summary": decision.summary,
                    "evidence": list(decision.evidence),
                    "response_required": decision.response_required,
                    "requested_resources": list(decision.requested_resources),
                    "todo_writes": list(decision.todo_writes),
                    "notes": list(decision.notes),
                    "actor_mode": decision.actor_mode,
                },
            })
            requested_actions = []
            for item in decision.requested_resources:
                action = self._queue_requested_resource(item)
                if action:
                    requested_actions.append(action)
            for todo in decision.todo_writes:
                note = str(todo.get("note", "")).strip()
                if note:
                    created = self.todos.add(
                        note,
                        due_in_seconds=float(todo.get("due_in_seconds", 0) or 0),
                        kind=str(todo.get("kind", "scheduled")),
                    )
                    requested_actions.append({"todo": created})
            planner_summary = (
                "Current evidence is sufficient."
                if decision.sufficient
                else "More evidence requested before responding."
            )
            stimulus_payload = dict((turn_snapshot.get("last_stimulus") or {}).get("payload", {}))
            if stimulus_type in {"stimulus:tab_opened", "stimulus:tab_refreshed"}:
                for tab in stimulus_payload.get("tabs", [])[:5]:
                    tab_id = str(tab.get("id", "")).strip()
                    tab_url = str(tab.get("url", "")).strip()
                    if not tab_id and not tab_url:
                        continue
                    requested_actions.append(
                        self._queue_requested_resource(
                            {
                                "type": "screen_scan",
                                "reason": "Keep a record image for the changed tab.",
                                "source": "screen",
                                "tab_id": tab_id,
                                "tab_url": tab_url,
                                "dedupe_key": f"screen_scan:{tab_id or tab_url}",
                            }
                        )
                    )
            mpa_result = {
                "triggered": True,
                "should_intervene": bool(decision.response_required),
                "agenda": decision.response_text or planner_summary,
                "rationale": planner_summary,
                "supporting_points": list(decision.evidence) + [str(item.get("reason", "")) for item in decision.requested_resources if item.get("reason")],
                "actor_mode": decision.actor_mode,
                "requested_actions": requested_actions,
                "sufficient": decision.sufficient,
                "focus_state": decision.focus_state,
                "notes": list(decision.notes),
            }
            self._set_actor_stage("mpa", "writing", "Planner updated resource requests and next actions.", output_preview=mpa_result["agenda"])
            self._log_event("mpa", "writing", "Planner state updated.", {
                "turn_id": turn_id,
                "planner_output": mpa_result,
            })
            if not decision.sufficient or not decision.response_required:
                personality_output = self._idle_personality_output()
                self._set_actor_stage("personality", "idle", "Response actor waiting until enough evidence exists.")
                self._log_event("personality", "idle", "Response actor skipped because no response was requested.", {
                    "turn_id": turn_id,
                })
            else:
                personality_mpa = type("PlannerAgenda", (), {
                    "triggered": True,
                    "should_intervene": True,
                    "agenda": decision.response_text,
                    "rationale": decision.summary,
                    "supporting_points": list(decision.evidence),
                })()
                watcher_like = [type("Evidence", (), {
                    "summary": decision.summary,
                    "relevant_evidence": list(decision.evidence),
                })()]
                self._set_actor_stage("personality", "reading", "Response actor reading the plan and evidence.")
                self._log_event("personality", "reading", "Personality received agenda and evidence.", {
                    "turn_id": turn_id,
                    "agenda": decision.response_text,
                    "rationale": decision.summary,
                    "supporting_points": list(decision.evidence),
                })
                self._set_actor_stage("personality", "processing", "Response actor is generating the spoken intervention.")
                self._log_event("personality", "processing", "Personality evaluation started.", {
                    "turn_id": turn_id,
                    "model": self.personality.model,
                })
                personality_result = self.personality.evaluate(goal, personality_mpa, watcher_like)
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
                self._set_actor_stage(
                    "personality",
                    "writing",
                    "Response actor wrote the spoken intervention.",
                    output_preview=personality_result.spoken_text,
                )
                self._log_event("personality", "writing", "Personality result received.", {
                    "turn_id": turn_id,
                    "output": {
                        "triggered": personality_result.triggered,
                        "should_speak": personality_result.should_speak,
                        "spoken_text": personality_result.spoken_text,
                        "delivery_notes": personality_result.delivery_notes,
                        "actor_mode": personality_result.actor_mode,
                    },
                })
                if personality_result.should_speak:
                    self._log_event("audio", "processing", "Starting ElevenLabs synthesis.", {
                        "turn_id": turn_id,
                        "text": personality_result.spoken_text,
                    })
                    personality_output.update(self._synthesize_personality_audio(personality_result.spoken_text))
                    self._log_event("audio", "writing", "Audio synthesis finished.", {
                        "turn_id": turn_id,
                        "audio_generated": personality_output.get("audio_generated", False),
                        "audio_url": personality_output.get("audio_url", ""),
                        "audio_error": personality_output.get("audio_error", ""),
                    })
                    if cooldown_seconds > 0:
                        with self.state.lock:
                            self.state.cooldown_until = time.time() + cooldown_seconds
                        self._log_event("agent", "cooldown_scheduled", "Post-response cooldown scheduled.", {
                            "turn_id": turn_id,
                            "cooldown_seconds": cooldown_seconds,
                        })
            context_entry = self.context_files.write_snapshot(
                decision.summary,
                focus_state=decision.focus_state,
                notes=decision.notes,
                meta={
                    "last_stimulus": stimulus_type,
                    "last_turn_reason": stimulus_type,
                    "resource_summary": {
                        "available_sources": list(turn_snapshot.get("available_sources", [])),
                        "missing_sources": list(turn_snapshot.get("missing_sources", [])),
                    },
                },
            )
            self.status_file.update(
                focus_state=decision.focus_state,
                last_turn_at=turn_time,
                last_turn_reason=stimulus_type,
                notes=decision.summary,
            )
            self.memory.append(
                "agent_turn",
                decision.summary,
                meta={
                    "stimulus": stimulus_type,
                    "focus_state": decision.focus_state,
                    "evidence": list(decision.evidence),
                    "requested_resources": list(decision.requested_resources),
                },
            )
            with self.state.lock:
                self.state.off_task_streak = 1 if decision.focus_state == "distracted" else 0
                self.state.threshold_progress = len(decision.requested_resources)
                self.state.watcher_output = {
                    "off_task": decision.focus_state == "distracted",
                    "confidence": 1.0 if decision.sufficient else 0.5,
                    "summary": decision.summary,
                    "relevant_evidence": list(decision.evidence),
                    "actor_mode": decision.actor_mode,
                }
                self.state.required_reassessment_revision = 0
                self.state.turns_completed += 1
                self.state.mpa_output = dict(mpa_result)
                self.state.personality_output = dict(personality_output)
                if self.state.personality_output["triggered"] and self.state.personality_output["should_speak"]:
                    self.state.status = (
                        f"{decision.focus_state.title()}. "
                        f"Personality line ready: {self.state.personality_output['spoken_text']}"
                    )
                elif decision.requested_resources:
                    self.state.status = (
                        f"{decision.focus_state.title()}. "
                        f"Agent requested {len(decision.requested_resources)} more resource(s)."
                    )
                else:
                    self.state.status = f"{decision.focus_state.title()}: {decision.summary}"
                self.state.last_turn_at = turn_time
                self.state.last_error = ""
                self.state.cycle_status = f"Turn {turn_id} complete."
                self.state.resources["browser"] = self.state.resources["browser"]
            self._set_actor_stage("watcher", "idle", "Watcher is idle until the next tick.")
            self._set_actor_stage("mpa", "idle", "Planner is idle until the next agent turn.", output_preview=mpa_result["agenda"])
            self._set_actor_stage(
                "personality",
                "idle",
                "Response actor is idle until the next response-worthy turn.",
                output_preview=personality_output.get("spoken_text", ""),
            )
            self._log_event("turn", "complete", "Agent turn completed.", {
                "turn_id": turn_id,
                "reason": turn_snapshot.get("reason", ""),
                "focus_state": decision.focus_state,
                "requested_resources": len(decision.requested_resources),
                "context_updated_at": context_entry.get("timestamp", ""),
            })
        except Exception as exc:
            self._set_actor_stage("watcher", "idle", f"Agent error: {exc}")
            self._set_actor_stage("mpa", "idle", "Planner halted due to upstream error.")
            self._set_actor_stage("personality", "idle", "Response actor halted due to upstream error.")
            self._log_event("turn", "error", "Agent turn failed.", {
                "turn_id": turn_id,
                "error": str(exc),
            })
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
        if parsed.path == "/artifacts/debug-events.jsonl":
            return self._serve_file(DEBUG_LOG_PATH, "application/jsonl; charset=utf-8")
        if parsed.path == "/api/state":
            return self._json_response(APP.snapshot())
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
                    reason=payload.get("reason", "manual_run"),
                )
                return self._json_response({"ok": True, "state": APP.snapshot()})

            if parsed.path == "/api/start":
                APP.start_monitoring(
                    goal=payload.get("goal", APP.state.goal),
                    interval_seconds=payload.get("interval_seconds", APP.state.interval_seconds),
                    threshold=payload.get("threshold", APP.state.threshold),
                    duration_seconds=payload.get("duration_seconds", APP.state.session_duration_seconds),
                )
                return self._json_response({"ok": True, "state": APP.snapshot()})

            if parsed.path == "/api/stop":
                APP.stop_monitoring()
                return self._json_response({"ok": True, "state": APP.snapshot()})

            if parsed.path == "/api/reset-stats":
                APP.reset_stats()
                return self._json_response({"ok": True, "state": APP.snapshot()})

            if parsed.path == "/api/stimulus":
                result = APP.ingest_stimulus(
                    stimulus_type=payload.get("type", ""),
                    payload=payload.get("payload", {}),
                )
                return self._json_response({"ok": True, "stimulus": result})

            if parsed.path == "/api/speech-finished":
                APP.note_speech_finished(payload.get("pause_seconds"))
                return self._json_response({"ok": True, "state": APP.snapshot()})

            if parsed.path == "/api/client-action-complete":
                result = APP.complete_client_action(
                    action_id=payload.get("action_id", ""),
                    result=payload.get("result", {}),
                )
                return self._json_response({"ok": True, "action": result, "state": APP.snapshot()})

            if parsed.path == "/api/export-tabs":
                APP.configure_browser(
                    browser_name=payload.get("browser_name"),
                    browser_url=payload.get("browser_url"),
                )
                export_info = APP.export_tabs()
                return self._json_response({"ok": True, "export": export_info, "state": APP.snapshot()})

            if parsed.path == "/api/launch-browser":
                launch_info = APP.launch_browser(
                    browser_name=payload.get("browser_name"),
                    browser_url=payload.get("browser_url"),
                )
                return self._json_response({"ok": True, "launch": launch_info, "state": APP.snapshot()})

            if parsed.path == "/api/analyze":
                result = APP.analyze_capture(
                    analysis_mode=payload.get("analysisMode", ""),
                    prompt=payload.get("prompt", ""),
                    image_data_url=payload.get("imageDataUrl", ""),
                    metadata=payload.get("metadata", {}),
                )
                return self._json_response({"ok": True, **result, "state": APP.snapshot()})
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
