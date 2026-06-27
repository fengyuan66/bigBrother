import base64
import hashlib
import json
import os
import socket
import threading
import time
import base64
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
from browser_live_demo import BROWSERS, BrowserEventMonitor, BrowserLiveReader
from config import env_int, load_env_file
from orchestrator import AgentOrchestrator
from resources import ResourceLoader


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "webapp"
SOURCES_DIR = APP_DIR / "sources"
SUMMARIES_DIR = APP_DIR / "summaries"
STATE_DIR = APP_DIR / "state"
DEBUG_LOG_PATH = STATE_DIR / "debug_events.jsonl"
PERSONALITY_AUDIO_PATH = SUMMARIES_DIR / "personality_latest.mp3"
PERSONALITY_JSON_PATH = SUMMARIES_DIR / "personality_latest.json"
TAB_RECORDS_DIR = SOURCES_DIR / "browser" / "tab_records"

load_env_file(APP_DIR / ".env")

VISION_MODEL = os.getenv("BIG_BROTHER_VISION_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")
API_URL = os.getenv("BIG_BROTHER_BASE_URL", "https://ai.hackclub.com/proxy/v1").rstrip("/") + "/chat/completions"
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_SESSION_SECONDS = 15 * 60
MAX_DEBUG_EVENTS = 200

MODE_TO_SOURCE_DIR = {
    "webcam": SOURCES_DIR / "webcam",
    "screen": SOURCES_DIR / "video",
}
MODE_TO_SUMMARY_PATH = {
    "webcam": SUMMARIES_DIR / "webcam_summary.json",
    "screen": SUMMARIES_DIR / "screen_summary.json",
}
VOLATILE_LINE_PREFIXES = ("created:", "updated:", "timestamp", "exported")
POST_SPEECH_GRACE_SECONDS = 0
FRESH_BROWSER_EXPORT_SECONDS = float(os.getenv("BIG_BROTHER_FRESH_BROWSER_EXPORT_SECONDS", "3"))
DEFAULT_SIMULATION_HOLD_SECONDS = float(os.getenv("BIG_BROTHER_SIMULATION_HOLD_SECONDS", "12"))


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def ensure_output_dirs():
    for path in MODE_TO_SOURCE_DIR.values():
        path.mkdir(parents=True, exist_ok=True)
    (SOURCES_DIR / "browser").mkdir(parents=True, exist_ok=True)
    TAB_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def parse_data_url(data_url: str) -> str:
    if not str(data_url or "").startswith("data:image/"):
        raise ValueError("Expected a base64 image data URL.")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Expected a base64 image data URL.")
    base64.b64decode(encoded, validate=True)
    return data_url


def build_vision_prompt(analysis_mode: str, user_prompt: str) -> str:
    if analysis_mode == "webcam":
        base_prompt = (
            "Summarize the webcam frame for an agent. Focus on posture, visible actions, attention, "
            "objects in hand, and whether the person looks present or absent. Separate observations from uncertainty."
        )
    else:
        base_prompt = (
            "Summarize the screen capture for an agent. Focus on visible apps, readable text, layout, "
            "and the likely task. Separate observations from uncertainty."
        )
    if user_prompt:
        return f"{base_prompt}\n\nUser focus: {user_prompt}"
    return base_prompt


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


def shorten_text(value: str, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def evidence_fingerprint(goal: str, prompt_text: str) -> str:
    stable_lines = [
        line
        for line in str(prompt_text or "").splitlines()
        if not line.strip().lower().startswith(VOLATILE_LINE_PREFIXES)
    ]
    return hashlib.sha1((f"{goal}\n" + "\n".join(stable_lines)).encode("utf-8")).hexdigest()


class DashboardState:
    def __init__(self):
        self.lock = threading.RLock()
        self.goal = "I am studying calculus"
        self.interval_seconds = env_int("BIG_BROTHER_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        self.session_duration_seconds = env_int("BIG_BROTHER_SESSION_DURATION_SECONDS", DEFAULT_SESSION_SECONDS)
        self.session_deadline_at = 0.0
        self.running = False
        self.status = "Ready."
        self.last_error = ""
        self.last_turn_at = ""
        self.last_turn_reason = ""
        self.resource_revision = 0
        self.resources = {
            "webcam": "No webcam summary yet.",
            "screenshare": "No screenshare summary yet.",
            "browser": "No browser export yet.",
        }
        self.agent_output = {
            "summary": "No agent decision yet.",
            "focus_state": "unknown",
            "evidence": [],
            "response_required": False,
            "requested_resources": [],
            "notes": [],
            "actor_mode": "idle",
        }
        self.planner_output = {
            "summary": "No follow-up actions yet.",
            "requested_actions": [],
            "notes": [],
            "actor_mode": "idle",
        }
        self.personality_output = {
            "triggered": False,
            "should_speak": False,
            "spoken_text": "No spoken response yet.",
            "delivery_notes": "Idle.",
            "actor_mode": "idle",
            "audio_generated": False,
            "audio_url": "",
            "audio_error": "",
            "event_id": "",
        }
        self.speech = {
            "in_progress": False,
            "event_id": "",
            "text": "",
            "started_at": "",
            "client_started": False,
            "grace_until_unix": 0.0,
        }
        self.capture_status = "No capture source active."
        self.last_export = {"path": "", "count": 0}
        self.last_analysis = {"analysisMode": "", "summary": "", "writtenFiles": {}}
        self.browser_name = os.getenv("BIG_BROTHER_DEMO_BROWSER", "Edge")
        self.browser_url = os.getenv("BIG_BROTHER_DEMO_URL", "https://en.wikipedia.org/wiki/Calculus")
        self.debug_events = []
        self.event_sequence = 0
        self.vision_model = VISION_MODEL

    def snapshot(self, agent, personality, resource_loader):
        with self.lock:
            remaining = 0
            if self.running and self.session_deadline_at:
                remaining = max(0, int(self.session_deadline_at - time.time()))
            return {
                "goal": self.goal,
                "interval_seconds": self.interval_seconds,
                "session_duration_seconds": self.session_duration_seconds,
                "session_remaining_seconds": remaining,
                "running": self.running,
                "status": self.status,
                "last_error": self.last_error,
                "last_turn_at": self.last_turn_at,
                "last_turn_reason": self.last_turn_reason,
                "resource_revision": self.resource_revision,
                "resources": dict(self.resources),
                "agent_output": dict(self.agent_output),
                "planner_output": dict(self.planner_output),
                "personality_output": dict(self.personality_output),
                "speech": dict(self.speech),
                "capture_status": self.capture_status,
                "last_export": dict(self.last_export),
                "last_analysis": dict(self.last_analysis),
                "browser_name": self.browser_name,
                "browser_url": self.browser_url,
                "available_browsers": list(BROWSERS.keys()),
                "paths": resource_loader.describe_paths(),
                "debug_events": list(self.debug_events),
                "debug_log_path": str(DEBUG_LOG_PATH.relative_to(APP_DIR)),
                "vision_model": self.vision_model,
                "agent_enabled": agent.enabled,
                "agent_model": agent.model,
                "personality_enabled": personality.enabled,
                "personality_model": personality.model or "fallback",
                "snapshot_at": now_iso(),
            }


class BigBrotherApp:
    def __init__(self, start_orchestrator=True):
        ensure_output_dirs()
        self._runtime_started_perf = time.perf_counter()
        self._last_event_perf = self._runtime_started_perf
        self._browser_refresh_lock = threading.RLock()
        self._simulation_lock = threading.RLock()
        self.state = DashboardState()
        self.resource_loader = ResourceLoader()
        self.ledger = TokenLedger()
        self.memory = AgentMemory()
        self.status_file = StatusFile()
        self.todos = TodoList()
        self.context_files = ContextFiles()
        self.client_actions = ClientActionQueue()
        self.bus = StimulusBus()
        self.agent = AgentActor(ledger=self.ledger)
        self.personality = PersonalityActor(ledger=self.ledger)
        self.tab_reader = BrowserLiveReader(BROWSERS[self.state.browser_name])
        self.browser_event_monitor = None
        self.orchestrator = AgentOrchestrator(self)
        self._capture_hashes = {}
        self._capture_cache = {}
        self._last_decision_fingerprint = ""
        self._last_browser_export_fingerprint = ""
        self._browser_simulation = {
            "active": False,
            "tabs": [],
            "note": "",
            "source": "simulation",
            "expires_at_unix": 0.0,
            "applied_at_unix": 0.0,
            "needs_live_resync": False,
        }
        if start_orchestrator:
            self.orchestrator.start()
        self._ensure_browser_event_monitor()
        self._log_event(
            "system",
            "startup",
            "Minimal agent runtime initialized.",
            {
                "agent_model": self.agent.model,
                "personality_model": self.personality.model or "fallback",
                "vision_model": VISION_MODEL,
            },
        )

    def snapshot(self):
        snap = self.state.snapshot(self.agent, self.personality, self.resource_loader)
        snap["agent"] = {
            "status": self.status_file.get(),
            "todos": self.todos.list_all(),
            "memory_recent": self.memory.recent(12),
            "context_current": self.context_files.get_current(),
            "context_history": self.context_files.recent_history(8),
            "pending_actions": self.client_actions.pending(),
            "last_stimulus": dict(self.bus.last_stimulus),
            "stimulus_history": self.bus.history(limit=40),
            "token_ledger": self.ledger.snapshot(),
            "heartbeat_seconds": self.orchestrator.heartbeat_seconds,
        }
        snap["simulation"] = self._simulation_snapshot()
        return snap

    def _simulation_snapshot(self) -> dict:
        with self._simulation_lock:
            active = bool(self._browser_simulation.get("active"))
            tabs = list(self._browser_simulation.get("tabs", []) or [])
            note = str(self._browser_simulation.get("note", "")).strip()
            source = str(self._browser_simulation.get("source", "simulation")).strip() or "simulation"
            expires_at_unix = float(self._browser_simulation.get("expires_at_unix", 0.0) or 0.0)
            applied_at_unix = float(self._browser_simulation.get("applied_at_unix", 0.0) or 0.0)
            needs_live_resync = bool(self._browser_simulation.get("needs_live_resync"))
        now = time.time()
        if active and expires_at_unix and expires_at_unix <= now:
            active = False
        return {
            "active": active,
            "note": note,
            "source": source,
            "tab_count": len(tabs),
            "tabs_preview": tabs[:5],
            "expires_at_unix": expires_at_unix,
            "applied_at_unix": applied_at_unix,
            "remaining_seconds": max(0.0, round(expires_at_unix - now, 3)) if active and expires_at_unix else 0.0,
            "needs_live_resync": needs_live_resync,
        }

    def _debug_state_summary(self) -> dict:
        with self.state.lock:
            return {
                "running": bool(self.state.running),
                "status": str(self.state.status),
                "last_turn_at": str(self.state.last_turn_at),
                "last_turn_reason": str(self.state.last_turn_reason),
                "resource_revision": int(self.state.resource_revision),
                "capture_status": str(self.state.capture_status),
                "browser_name": str(self.state.browser_name),
                "browser_url": str(self.state.browser_url),
                "session_duration_seconds": int(self.state.session_duration_seconds),
                "speech": {
                    "in_progress": bool(self.state.speech.get("in_progress")),
                    "client_started": bool(self.state.speech.get("client_started")),
                    "event_id": str(self.state.speech.get("event_id", "")),
                    "grace_until_unix": float(self.state.speech.get("grace_until_unix", 0.0) or 0.0),
                },
                "agent_output_summary": str((self.state.agent_output or {}).get("summary", ""))[:200],
                "planner_requested_actions": len((self.state.planner_output or {}).get("requested_actions", []) or []),
                "personality_pending": bool((self.state.personality_output or {}).get("should_speak")),
            }

    def _debug_resource_summary(self, resources) -> dict:
        metadata = dict(getattr(resources, "metadata", {}) or {})
        return {
            "missing_sources": list(getattr(resources, "missing_sources", []) or []),
            "sources": {
                "webcam": {
                    "chars": len(str(getattr(resources, "webcam_text", "") or "")),
                    "metadata": metadata.get("webcam", {}),
                },
                "screenshare": {
                    "chars": len(str(getattr(resources, "screenshare_text", "") or "")),
                    "metadata": metadata.get("screenshare", {}),
                },
                "browser": {
                    "chars": len(str(getattr(resources, "browser_text", "") or "")),
                    "metadata": metadata.get("browser", {}),
                },
            },
        }

    def _browser_tabs_fingerprint(self, tabs: list[dict]) -> str:
        normalized = []
        for tab in list(tabs or []):
            tab = dict(tab or {})
            normalized.append(
                {
                    "id": str(tab.get("id", "")).strip(),
                    "title": str(tab.get("title", "")).strip(),
                    "url": str(tab.get("url", "")).strip(),
                    "domain": str(tab.get("domain", "")).strip(),
                }
            )
        normalized.sort(key=lambda item: (item["id"], item["url"], item["title"], item["domain"]))
        return hashlib.sha1(
            json.dumps(normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _normalize_browser_tabs(self, tabs) -> list[dict]:
        normalized = []
        for raw in list(tabs or []):
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url", "")).strip()
            title = str(raw.get("title", "")).strip()
            domain = str(raw.get("domain", "")).strip() or urlparse(url).netloc
            tab_id = str(raw.get("id", "") or raw.get("tab_id", "")).strip()
            if not tab_id:
                basis = f"{title}|{url}|{domain}"
                tab_id = hashlib.sha1(basis.encode("utf-8")).hexdigest().upper()[:32]
            normalized.append(
                {
                    "id": tab_id,
                    "title": title,
                    "url": url,
                    "domain": domain,
                }
            )
        return normalized

    def _simulation_override_tabs(self) -> list[dict] | None:
        with self._simulation_lock:
            active = bool(self._browser_simulation.get("active"))
            expires_at_unix = float(self._browser_simulation.get("expires_at_unix", 0.0) or 0.0)
            if not active:
                return None
            if expires_at_unix and expires_at_unix <= time.time():
                self._browser_simulation = {
                    "active": False,
                    "tabs": [],
                    "note": "",
                    "source": "simulation",
                    "expires_at_unix": 0.0,
                    "applied_at_unix": 0.0,
                    "needs_live_resync": True,
                }
                return None
            return self._normalize_browser_tabs(self._browser_simulation.get("tabs", []))

    def _simulation_active(self) -> bool:
        return self._simulation_override_tabs() is not None

    def _consume_simulation_live_resync_flag(self) -> bool:
        with self._simulation_lock:
            needs_live_resync = bool(self._browser_simulation.get("needs_live_resync"))
            if needs_live_resync:
                self._browser_simulation["needs_live_resync"] = False
        return needs_live_resync

    def _has_fresh_browser_export(self, max_age_seconds: float = FRESH_BROWSER_EXPORT_SECONDS) -> bool:
        with self.state.lock:
            exported_at_unix = float((self.state.last_export or {}).get("exported_at_unix", 0.0) or 0.0)
        if exported_at_unix <= 0:
            return False
        return (time.time() - exported_at_unix) <= max(0.0, float(max_age_seconds))

    def _ensure_browser_event_monitor(self, force_restart: bool = False):
        config = BROWSERS[self.state.browser_name]
        if (
            not force_restart
            and
            self.browser_event_monitor is not None
            and self.browser_event_monitor.is_alive()
            and self.browser_event_monitor.config.name == config.name
        ):
            return
        self._stop_browser_event_monitor(log_event=False)
        self.browser_event_monitor = BrowserEventMonitor(config, self._handle_browser_debug_event, logger=self._log_event)
        self.browser_event_monitor.start()
        self._log_event("browser", "event_monitor_start", "Browser event monitor started.", {"browser": config.name})

    def _stop_browser_event_monitor(self, log_event=True):
        monitor = self.browser_event_monitor
        self.browser_event_monitor = None
        if monitor is None:
            return
        try:
            monitor.stop()
        finally:
            if log_event:
                self._log_event("browser", "event_monitor_stop", "Browser event monitor stopped.", {"browser": monitor.config.name})

    def _handle_browser_debug_event(self, event: dict):
        started_perf = time.perf_counter()
        stimulus_type = str(event.get("stimulus_type", "")).strip()
        payload = dict(event.get("payload") or {})
        source_event_at_unix = float(payload.get("source_event_at_unix", 0.0) or 0.0)
        callback_received_at_unix = time.time()
        callback_latency_ms = round((callback_received_at_unix - source_event_at_unix) * 1000, 1) if source_event_at_unix else None
        self._log_event(
            "browser",
            "event_received",
            "Browser event handed to Big Brother.",
            {
                "stimulus_type": stimulus_type,
                "payload": payload,
                "source_event_at_unix": source_event_at_unix,
                "callback_received_at_unix": callback_received_at_unix,
                "callback_latency_ms": callback_latency_ms,
            },
        )
        if self._simulation_active():
            self._log_event(
                "browser",
                "event_ignored_simulation",
                "Browser event ignored because a simulation override is active.",
                {
                    "stimulus_type": stimulus_type,
                    "payload": payload,
                    "source_event_at_unix": source_event_at_unix,
                    "callback_received_at_unix": callback_received_at_unix,
                    "callback_latency_ms": callback_latency_ms,
                },
            )
            return
        with self.state.lock:
            running = bool(self.state.running)
        if not running or not stimulus_type:
            self._log_event(
                "browser",
                "event_ignored",
                "Browser event ignored because the session is not running or the event was empty.",
                {
                    "stimulus_type": stimulus_type,
                    "payload": payload,
                    "running": running,
                    "source_event_at_unix": source_event_at_unix,
                    "callback_received_at_unix": callback_received_at_unix,
                    "callback_latency_ms": callback_latency_ms,
                },
            )
            return

        if stimulus_type in {"tab_opened", "tab_refreshed", "tab_closed"}:
            self._cancel_pending_speech_if_not_started(
                reason="New browser evidence arrived before narration began.",
                replacement_stimulus=stimulus_type,
            )
            self._clear_speech_grace_if_active(
                reason="New browser evidence arrived after narration finished.",
                replacement_stimulus=stimulus_type,
            )

        tabs = self.refresh_browser_export(retries=1, delay_seconds=0.05, log_event=False)
        self._log_event(
            "browser",
            "event_refresh_complete",
            "Browser export refreshed immediately after a browser event.",
            {
                "stimulus_type": stimulus_type,
                "duration_ms": round((time.perf_counter() - started_perf) * 1000, 1),
                "source_event_at_unix": source_event_at_unix,
                "callback_received_at_unix": callback_received_at_unix,
                "callback_latency_ms": callback_latency_ms,
                "count": len(tabs),
                "tabs_preview": [
                    {
                        "id": str(tab.get("id", "")),
                        "title": str(tab.get("title", ""))[:120],
                        "url": str(tab.get("url", ""))[:180],
                    }
                    for tab in tabs[:5]
                ],
            },
        )
        self.orchestrator.sync_tab_signature(tabs, source=f"browser_event:{stimulus_type}")
        accepted = self.bus.emit(stimulus_type, payload)
        self._log_event(
            "browser",
            "event_stimulus_emitted",
            "Browser event emitted an agent stimulus immediately.",
            {
                "stimulus_type": stimulus_type,
                "accepted": accepted,
                "payload": payload,
                "source_event_at_unix": source_event_at_unix,
                "callback_received_at_unix": callback_received_at_unix,
                "callback_latency_ms": callback_latency_ms,
                "total_duration_ms": round((time.perf_counter() - started_perf) * 1000, 1),
            },
        )

    def _cancel_pending_speech_if_not_started(self, reason: str, replacement_stimulus: str = "") -> bool:
        with self.state.lock:
            speech = dict(self.state.speech)
            if not speech.get("in_progress") or speech.get("client_started"):
                return False
            canceled_event_id = str(speech.get("event_id", "")).strip()
            current_output = dict(self.state.personality_output or {})
            current_output["should_speak"] = False
            current_output["spoken_text"] = ""
            current_output["delivery_notes"] = "Canceled before narration because newer browser evidence arrived."
            self.state.personality_output = current_output
            self.state.speech = {
                "in_progress": False,
                "event_id": "",
                "text": "",
                "started_at": "",
                "client_started": False,
                "grace_until_unix": 0.0,
            }
            self.state.status = "A stale speech signal was canceled before narration so newer browser evidence could be assessed."
        self._log_event(
            "speech",
            "canceled",
            "Pending speech signal canceled before narration started.",
            {"event_id": canceled_event_id, "reason": reason, "replacement_stimulus": replacement_stimulus},
        )
        return True

    def _clear_speech_grace_if_active(self, reason: str, replacement_stimulus: str = "") -> bool:
        with self.state.lock:
            speech = dict(self.state.speech)
            if speech.get("in_progress"):
                return False
            grace_until = float(speech.get("grace_until_unix", 0.0) or 0.0)
            if grace_until <= time.time():
                return False
            self.state.speech = {
                "in_progress": False,
                "event_id": "",
                "text": "",
                "started_at": "",
                "client_started": False,
                "grace_until_unix": 0.0,
            }
            self.state.status = "Post-speech grace cleared because fresher browser evidence arrived."
        self._log_event(
            "speech",
            "grace_cleared",
            "Post-speech grace window cleared so fresher browser evidence can be assessed immediately.",
            {"reason": reason, "replacement_stimulus": replacement_stimulus, "state": self._debug_state_summary()},
        )
        return True

    def _log_event(self, component: str, phase: str, message: str, payload=None):
        ensure_output_dirs()
        now_perf = time.perf_counter()
        event = {
            "timestamp": now_iso(),
            "unix_ms": int(time.time() * 1000),
            "runtime_ms": round((now_perf - self._runtime_started_perf) * 1000, 1),
            "delta_ms": round((now_perf - self._last_event_perf) * 1000, 1),
            "component": component,
            "phase": phase,
            "message": message,
            "thread": threading.current_thread().name,
        }
        self._last_event_perf = now_perf
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

    def expire_session_if_needed(self):
        with self.state.lock:
            running = self.state.running
            deadline_at = self.state.session_deadline_at
        if running and deadline_at and time.time() >= deadline_at:
            self.stop(timed_out=True)
            return True
        return False

    def speech_in_progress(self) -> bool:
        with self.state.lock:
            return bool(self.state.speech.get("in_progress"))

    def assessment_paused(self) -> bool:
        with self.state.lock:
            speech = dict(self.state.speech)
        if speech.get("in_progress"):
            return True
        grace_until = float(speech.get("grace_until_unix", 0.0) or 0.0)
        return grace_until > time.time()

    def _turn_is_obsolete(self, turn_resource_revision: int) -> tuple[bool, dict]:
        with self.state.lock:
            latest_revision = int(self.state.resource_revision)
            running = bool(self.state.running)
        return (not running or latest_revision != turn_resource_revision), {
            "turn_resource_revision": int(turn_resource_revision),
            "latest_resource_revision": latest_revision,
            "running": running,
        }

    def _discard_obsolete_turn(self, reason: str, phase_label: str, stale_payload: dict):
        if stale_payload.get("running"):
            message = f"Agent turn discarded because newer evidence arrived during {phase_label}."
        else:
            message = f"Agent turn discarded because the session stopped during {phase_label}."
        self._log_event("turn", "stale", message, {"reason": reason, **dict(stale_payload or {})})
        if not stale_payload.get("running"):
            with self.state.lock:
                if not self.state.running:
                    self.state.status = "Session stopped."

    def _resource_max_age_seconds(self):
        with self.state.lock:
            return max(10, int(self.state.interval_seconds) * 2 + 5)

    def _refresh_resource_debug(self):
        resources = self.resource_loader.load(max_age_seconds=self._resource_max_age_seconds())
        with self.state.lock:
            self.state.resources = {
                "webcam": resources.describe_source("webcam") or "No webcam summary yet.",
                "screenshare": resources.describe_source("screenshare") or "No screenshare summary yet.",
                "browser": resources.describe_source("browser") or "No browser export yet.",
            }
        self._log_event(
            "resources",
            "loaded",
            "Resource loader refreshed runtime resource summaries.",
            self._debug_resource_summary(resources),
        )
        return resources

    def _write_browser_outputs_from_tabs(self, tabs: list[dict], retries=2, delay_seconds=0.25):
        self.tab_reader = BrowserLiveReader(BROWSERS[self.state.browser_name])
        path, count = self.tab_reader.export_tabs(retries=retries, delay_seconds=delay_seconds, tabs=tabs)
        self.tab_reader.write_index(retries=retries, delay_seconds=delay_seconds, tabs=tabs)
        self.tab_reader.write_summary(retries=retries, delay_seconds=delay_seconds, tabs=tabs)
        return path, count

    def _apply_browser_simulation(self, tabs, hold_seconds=None, note: str = "", source: str = "simulation"):
        normalized_tabs = self._normalize_browser_tabs(tabs)
        hold_value = DEFAULT_SIMULATION_HOLD_SECONDS if hold_seconds is None else float(hold_seconds)
        hold_value = max(1.0, min(600.0, hold_value))
        now = time.time()
        with self._simulation_lock:
            self._browser_simulation = {
                "active": True,
                "tabs": normalized_tabs,
                "note": str(note or "").strip(),
                "source": str(source or "simulation").strip() or "simulation",
                "expires_at_unix": now + hold_value,
                "applied_at_unix": now,
                "needs_live_resync": False,
            }
        self.orchestrator.sync_tab_signature(normalized_tabs, source=f"{source}:override")
        self._log_event(
            "simulation",
            "override_applied",
            "Browser simulation override applied.",
            {
                "tab_count": len(normalized_tabs),
                "hold_seconds": hold_value,
                "note": str(note or "").strip(),
                "tabs_preview": normalized_tabs[:5],
            },
        )
        return {"tabs": normalized_tabs, "hold_seconds": hold_value, "applied_at_unix": now}

    def clear_browser_simulation(self):
        had_active = self._simulation_active()
        with self._simulation_lock:
            self._browser_simulation = {
                "active": False,
                "tabs": [],
                "note": "",
                "source": "simulation",
                "expires_at_unix": 0.0,
                "applied_at_unix": 0.0,
                "needs_live_resync": False,
            }
        tabs = self.refresh_browser_export(retries=1, delay_seconds=0.05, log_event=False)
        self.orchestrator.sync_tab_signature(tabs, source="simulation:cleared")
        self._log_event(
            "simulation",
            "override_cleared",
            "Browser simulation override cleared.",
            {"had_active": had_active, "live_tab_count": len(tabs), "tabs_preview": tabs[:5]},
        )
        return {"cleared": True, "had_active": had_active, "live_tab_count": len(tabs)}

    def _default_stimulus_payload_from_tabs(self, stimulus_type: str, tabs: list[dict]) -> dict:
        normalized_tabs = self._normalize_browser_tabs(tabs)
        if stimulus_type in {"tab_opened", "tab_refreshed"}:
            return {"count": len(normalized_tabs), "tabs": normalized_tabs[:5]}
        if stimulus_type == "tab_closed":
            return {
                "count": len(normalized_tabs),
                "tabs": normalized_tabs[:5],
                "tab_ids": [str(tab.get("id", "")).strip() for tab in normalized_tabs[:5] if str(tab.get("id", "")).strip()],
            }
        return {}

    def refresh_browser_export(self, retries=2, delay_seconds=0.25, log_event=True):
        started_perf = time.perf_counter()
        with self.state.lock:
            previous_revision = int(self.state.resource_revision)
            browser_name = self.state.browser_name
        simulation_tabs = self._simulation_override_tabs()
        source = "simulation" if simulation_tabs is not None else "live"
        should_resync_live_baseline = simulation_tabs is None and self._consume_simulation_live_resync_flag()
        with self._browser_refresh_lock:
            try:
                if simulation_tabs is not None:
                    tabs = simulation_tabs
                    path, count = self._write_browser_outputs_from_tabs(tabs, retries=retries, delay_seconds=delay_seconds)
                else:
                    self.tab_reader = BrowserLiveReader(BROWSERS[browser_name])
                    tabs = self.tab_reader.read_tabs(retries=retries, delay_seconds=delay_seconds)
                    path, count = self._write_browser_outputs_from_tabs(tabs, retries=retries, delay_seconds=delay_seconds)
            except Exception as exc:
                self._log_event("browser", "error", "Browser export failed.", {"error": str(exc)})
                return []
        exported_at_unix = time.time()
        fingerprint = self._browser_tabs_fingerprint(tabs)
        with self.state.lock:
            self.state.last_export = {
                "path": str(path.relative_to(APP_DIR)),
                "count": count,
                "exported_at_unix": exported_at_unix,
                "source": source,
            }
            changed = fingerprint != self._last_browser_export_fingerprint
            if changed:
                self.state.resource_revision += 1
                self._last_browser_export_fingerprint = fingerprint
            latest_revision = int(self.state.resource_revision)
        if changed:
            self._refresh_resource_debug()
        if should_resync_live_baseline:
            self.orchestrator.sync_tab_signature(tabs, source="simulation:expired")
            self._log_event(
                "simulation",
                "override_expired",
                "Simulation override expired and the browser baseline was resynced to live tabs.",
                {"live_tab_count": len(tabs), "tabs_preview": tabs[:5]},
            )
        if log_event:
            self._log_event(
                "browser",
                "refresh",
                "Browser export refreshed.",
                {
                    "count": count,
                    "path": str(path),
                    "retries": int(retries),
                    "delay_seconds": float(delay_seconds),
                    "source": source,
                    "changed": bool(changed),
                    "resource_revision_before": previous_revision,
                    "resource_revision_after": latest_revision,
                    "duration_ms": round((time.perf_counter() - started_perf) * 1000, 1),
                    "tabs_preview": [
                        {
                            "id": str(tab.get("id", "")),
                            "title": str(tab.get("title", ""))[:120],
                            "url": str(tab.get("url", ""))[:180],
                        }
                        for tab in tabs[:5]
                    ],
                },
            )
        return tabs

    def launch_browser(self, browser_name: str, browser_url: str):
        browser_name = browser_name if browser_name in BROWSERS else "Edge"
        with self.state.lock:
            self.state.browser_name = browser_name
            self.state.browser_url = browser_url or self.state.browser_url
        self.tab_reader = BrowserLiveReader(BROWSERS[browser_name])
        self.tab_reader.launch(browser_url or "about:blank")
        self._ensure_browser_event_monitor(force_restart=True)
        self._log_event("browser", "launch", "Tracked browser launched.", {"browser": browser_name, "url": browser_url})

    def start(self, goal=None, interval_seconds=None, duration_seconds=None, browser_name=None, browser_url=None):
        with self.state.lock:
            if goal:
                self.state.goal = str(goal)
            if interval_seconds:
                self.state.interval_seconds = max(1, int(interval_seconds))
            if duration_seconds:
                self.state.session_duration_seconds = max(60, int(duration_seconds))
            if browser_name in BROWSERS:
                self.state.browser_name = browser_name
            if browser_url:
                self.state.browser_url = browser_url
            self.state.running = True
            self.state.session_deadline_at = time.time() + self.state.session_duration_seconds
            self.state.status = "Agent session running."
            self.state.last_error = ""
            start_snapshot = self._debug_state_summary()
        self._ensure_browser_event_monitor(force_restart=True)
        self.refresh_browser_export()
        self._log_event("session", "start", "Agent session started.", start_snapshot)
        self.orchestrator.note_session_started()

    def stop(self, timed_out=False):
        with self.state.lock:
            self.state.running = False
            self.state.session_deadline_at = 0.0
            self.state.status = "Timed session complete." if timed_out else "Session stopped."
            stop_snapshot = self._debug_state_summary()
        self._log_event("session", "stop", "Agent session stopped.", {"timed_out": bool(timed_out), "state": stop_snapshot})
        self.orchestrator.note_session_stopped()

    def reset_stats(self):
        with self.state.lock:
            goal = self.state.goal
            interval = self.state.interval_seconds
            duration = self.state.session_duration_seconds
            browser_name = self.state.browser_name
            browser_url = self.state.browser_url
        self.stop()
        self.state = DashboardState()
        with self.state.lock:
            self.state.goal = goal
            self.state.interval_seconds = interval
            self.state.session_duration_seconds = duration
            self.state.browser_name = browser_name
            self.state.browser_url = browser_url
        self.memory = AgentMemory()
        self.status_file = StatusFile()
        self.todos = TodoList()
        self.context_files = ContextFiles()
        self.client_actions = ClientActionQueue()
        self.bus = StimulusBus()
        self.ledger = TokenLedger()
        self.agent = AgentActor(ledger=self.ledger)
        self.personality = PersonalityActor(ledger=self.ledger)
        self.tab_reader = BrowserLiveReader(BROWSERS[self.state.browser_name])
        self.orchestrator.bus = self.bus
        self.orchestrator.memory = self.memory
        self.orchestrator.status = self.status_file
        self.orchestrator.todos = self.todos
        self._capture_hashes = {}
        self._capture_cache = {}
        self._last_decision_fingerprint = ""
        self._last_browser_export_fingerprint = ""
        with self._simulation_lock:
            self._browser_simulation = {
                "active": False,
                "tabs": [],
                "note": "",
                "source": "simulation",
                "expires_at_unix": 0.0,
                "applied_at_unix": 0.0,
            }
        self._ensure_browser_event_monitor(force_restart=True)
        self.status_file.reset()
        self.context_files.reset()
        clear_targets = [
            DEBUG_LOG_PATH,
            PERSONALITY_JSON_PATH,
            self.memory.path,
            self.todos.path,
            self.client_actions.path,
        ]
        for path in clear_targets:
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
        try:
            self.status_file.path.write_text(json.dumps(self.status_file.data, indent=2), encoding="utf-8")
            self.context_files.current_path.write_text(json.dumps(self.context_files.current, indent=2), encoding="utf-8")
            self.todos.path.write_text("[]", encoding="utf-8")
            self.client_actions.path.write_text("[]", encoding="utf-8")
            self.ledger.path.write_text(json.dumps(self.ledger.snapshot(), indent=2), encoding="utf-8")
        except OSError:
            pass
        self._clear_resource_artifacts()
        self._log_event("system", "reset", "Agent state reset.", {"state": self._debug_state_summary()})

    def _clear_resource_artifacts(self):
        files_to_clear = [
            SOURCES_DIR / "webcam" / "latest.txt",
            SOURCES_DIR / "webcam" / "latest.json",
            SOURCES_DIR / "video" / "latest.txt",
            SOURCES_DIR / "video" / "latest.json",
            SUMMARIES_DIR / "webcam_summary.json",
            SUMMARIES_DIR / "screen_summary.json",
            SUMMARIES_DIR / "screenshare_summary.json",
            SOURCES_DIR / "browser" / "tabs.txt",
            SOURCES_DIR / "browser" / "index.json",
            SUMMARIES_DIR / "browser_summary.json",
        ]
        for path in files_to_clear:
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
        try:
            for path in TAB_RECORDS_DIR.glob("*"):
                if path.is_file():
                    path.unlink()
        except OSError:
            pass

    def run_once(self, goal=None, interval_seconds=None, duration_seconds=None, reason="manual_run"):
        with self.state.lock:
            if goal:
                self.state.goal = str(goal)
            if interval_seconds:
                self.state.interval_seconds = max(1, int(interval_seconds))
            if duration_seconds:
                self.state.session_duration_seconds = max(60, int(duration_seconds))
        self.run_turn(reason=reason)

    def run_turn(self, reason="manual_run"):
        turn_started_perf = time.perf_counter()
        self._log_event("turn", "requested", "Agent turn requested.", {"reason": reason, "state": self._debug_state_summary()})
        if self.assessment_paused():
            pause_payload = {"reason": reason, "speech_in_progress": self.speech_in_progress()}
            self._log_event("turn", "paused", "Turn blocked while speech/grace pause is active.", pause_payload)
            with self.state.lock:
                grace_until = float(self.state.speech.get("grace_until_unix", 0.0) or 0.0)
                if self.state.speech.get("in_progress"):
                    self.state.status = "Speech narration in progress. Big Brother is waiting before reassessing."
                elif grace_until > time.time():
                    remaining = max(0, int(grace_until - time.time()))
                    self.state.status = f"Post-speech grace active ({remaining}s). Big Brother is waiting before reassessing."
            return
        refresh_started_perf = time.perf_counter()
        if self._has_fresh_browser_export():
            self._log_event(
                "turn",
                "browser_sync_reused",
                "Turn reused a fresh browser export instead of forcing another export.",
                {"reason": reason, "max_age_seconds": FRESH_BROWSER_EXPORT_SECONDS},
            )
        else:
            self.refresh_browser_export(log_event=False)
            self._log_event(
                "turn",
                "browser_sync_complete",
                "Browser export sync finished for turn.",
                {"reason": reason, "duration_ms": round((time.perf_counter() - refresh_started_perf) * 1000, 1)},
            )
        resources = self._refresh_resource_debug()
        with self.state.lock:
            turn_resource_revision = int(self.state.resource_revision)
        with self.state.lock:
            goal = self.state.goal
            self.state.status = f"Running agent turn ({reason})..."
            self.state.last_error = ""

        prompt_text = resources.as_prompt_text()
        fingerprint = evidence_fingerprint(goal, prompt_text)
        if reason == "stimulus:heartbeat" and fingerprint == self._last_decision_fingerprint:
            self.ledger.record_skip("agent", estimate_text_tokens(prompt_text) + 200)
            with self.state.lock:
                self.state.status = "Heartbeat checked in; evidence is unchanged."
                self.state.last_turn_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.state.last_turn_reason = reason
            self._log_event("agent", "cached", "Heartbeat turn skipped because evidence was unchanged.", {"reason": reason})
            return

        current_context = self.context_files.get_current()
        history = self.context_files.recent_history(8)
        stimulus_payload = {}
        last_stimulus = dict(getattr(self.bus, "last_stimulus", {}) or {})
        if str(last_stimulus.get("type", "")).strip() and f"stimulus:{last_stimulus.get('type')}" == reason:
            stimulus_payload = dict(last_stimulus.get("payload", {}) or {})
        self._log_event(
            "agent",
            "reading",
            "Agent received fresh resources.",
            {
                "reason": reason,
                "prompt": prompt_text,
                "turn_resource_revision": turn_resource_revision,
                "resource_summary": self._debug_resource_summary(resources),
                "current_context": current_context,
                "history_count": len(history),
                "last_stimulus": last_stimulus,
                "stimulus_payload_used": stimulus_payload,
            },
        )
        actor_started_perf = time.perf_counter()
        decision = self.agent.evaluate(
            goal,
            resources,
            stimulus_type=reason,
            stimulus_payload=stimulus_payload,
            current_context=current_context,
            historic_context=history,
        )
        self._log_event(
            "agent",
            "evaluate_complete",
            "Agent evaluation finished.",
            {
                "reason": reason,
                "duration_ms": round((time.perf_counter() - actor_started_perf) * 1000, 1),
                "sufficient": decision.sufficient,
                "focus_state": decision.focus_state,
                "summary": decision.summary,
                "evidence": list(decision.evidence),
                "response_required": decision.response_required,
                "response_text": decision.response_text,
                "todo_writes": list(decision.todo_writes),
                "notes": list(decision.notes),
                "requested_resources": list(decision.requested_resources),
            },
        )
        obsolete, stale_payload = self._turn_is_obsolete(turn_resource_revision)
        if obsolete:
            self._discard_obsolete_turn(reason, "evaluation", stale_payload)
            return

        for _ in range(2):
            immediate_browser_requests = [
                item
                for item in decision.requested_resources
                if str(item.get("type", "")).strip().lower() in {"browser", "browser_scan", "browser_rag"}
            ]
            blocking_visual_requests = [
                item
                for item in decision.requested_resources
                if str(item.get("type", "")).strip().lower() in {"screen", "screen_scan", "webcam", "webcam_scan"}
            ]
            if not immediate_browser_requests or blocking_visual_requests or decision.sufficient:
                break
            loop_started_perf = time.perf_counter()
            for item in immediate_browser_requests:
                self._queue_requested_resource(item)
            resources = self._refresh_resource_debug()
            prompt_text = resources.as_prompt_text()
            self._log_event(
                "agent",
                "reading",
                "Agent re-evaluating after procedural browser refresh.",
                {
                    "reason": reason,
                    "prompt": prompt_text,
                    "resource_summary": self._debug_resource_summary(resources),
                    "immediate_browser_requests": immediate_browser_requests,
                },
            )
            decision = self.agent.evaluate(
                goal,
                resources,
                stimulus_type=reason,
                stimulus_payload=stimulus_payload,
                current_context=current_context,
                historic_context=history,
            )
            self._log_event(
                "agent",
                "reevaluate_complete",
                "Agent re-evaluation finished after browser refresh.",
                {
                    "reason": reason,
                    "duration_ms": round((time.perf_counter() - loop_started_perf) * 1000, 1),
                    "sufficient": decision.sufficient,
                    "focus_state": decision.focus_state,
                    "summary": decision.summary,
                    "evidence": list(decision.evidence),
                    "response_required": decision.response_required,
                    "response_text": decision.response_text,
                    "todo_writes": list(decision.todo_writes),
                    "notes": list(decision.notes),
                    "requested_resources": list(decision.requested_resources),
                },
            )
            obsolete, stale_payload = self._turn_is_obsolete(turn_resource_revision)
            if obsolete:
                self._discard_obsolete_turn(reason, "browser re-evaluation", stale_payload)
                return

        self._last_decision_fingerprint = fingerprint

        now_label = time.strftime("%Y-%m-%d %H:%M:%S")
        should_speak = decision.response_required and self._should_emit_response(decision)
        personality_started_perf = time.perf_counter()
        if should_speak:
            personality_result = self.personality.evaluate(goal, decision)
        else:
            personality_result = self.personality.evaluate(
                goal,
                type(
                    "SilentDecision",
                    (),
                    {
                        "response_required": False,
                        "response_text": "",
                        "summary": decision.summary,
                        "evidence": list(decision.evidence),
                    },
                )(),
            )
        obsolete, stale_payload = self._turn_is_obsolete(turn_resource_revision)
        if obsolete:
            self._discard_obsolete_turn(reason, "response wording", stale_payload)
            return
        self._log_event(
            "response",
            "personality_complete",
            "Response wording actor finished.",
            {
                "reason": reason,
                "duration_ms": round((time.perf_counter() - personality_started_perf) * 1000, 1),
                "should_speak": personality_result.should_speak,
                "actor_mode": personality_result.actor_mode,
                "triggered": personality_result.triggered,
                "spoken_text": personality_result.spoken_text,
                "delivery_notes": personality_result.delivery_notes,
            },
        )
        if personality_result.should_speak:
            self._arm_speech_lock(now_label, personality_result.spoken_text)
        personality_output = {
            "triggered": personality_result.triggered,
            "should_speak": personality_result.should_speak,
            "spoken_text": personality_result.spoken_text,
            "delivery_notes": personality_result.delivery_notes,
            "actor_mode": personality_result.actor_mode,
            "audio_generated": False,
            "audio_url": "",
            "audio_error": "",
            "event_id": now_label,
        }

        requested_actions = []
        for item in decision.requested_resources:
            queued = self._queue_requested_resource(item)
            if queued:
                requested_actions.append(queued)
        for todo in decision.todo_writes:
            note = str(todo.get("note", "")).strip()
            if note:
                created = self.todos.add(
                    note,
                    due_in_seconds=float(todo.get("due_in_seconds", 0) or 0),
                    kind=str(todo.get("kind", "scheduled")),
                )
                requested_actions.append({"todo": created})
        self._log_event(
            "planner",
            "actions_resolved",
            "Decision resources and todos were converted into runtime actions.",
            {
                "reason": reason,
                "requested_resources": list(decision.requested_resources),
                "resolved_actions": requested_actions,
                "todo_writes": list(decision.todo_writes),
            },
        )

        self.context_files.write_snapshot(
            decision.summary,
            focus_state=decision.focus_state,
            notes=decision.notes,
            meta={
                "last_stimulus": reason,
                "last_turn_reason": reason,
                "resource_summary": {
                    "missing_sources": list(resources.missing_sources),
                },
            },
        )
        if decision.response_required and not should_speak:
            decision.notes.append("A speech narration is already in progress, so this turn did not emit a new spoken response.")
        self.status_file.update(
            focus_state=decision.focus_state,
            last_turn_at=now_label,
            last_turn_reason=reason,
            notes=decision.summary,
        )
        self.memory.append(
            "agent_turn",
            decision.summary,
            meta={
                "stimulus": reason,
                "focus_state": decision.focus_state,
                "evidence": list(decision.evidence),
                "requested_resources": list(decision.requested_resources),
                "response_required": decision.response_required,
                "response_spoken": personality_result.should_speak,
            },
        )
        with self.state.lock:
            self.state.agent_output = {
                "summary": decision.summary,
                "focus_state": decision.focus_state,
                "evidence": list(decision.evidence),
                "response_required": decision.response_required,
                "requested_resources": list(decision.requested_resources),
                "notes": list(decision.notes),
                "actor_mode": decision.actor_mode,
            }
            self.state.planner_output = {
                "summary": "Requested follow-up resources." if requested_actions else "No follow-up actions were needed.",
                "requested_actions": requested_actions,
                "notes": list(decision.notes),
                "actor_mode": decision.actor_mode,
            }
            self.state.personality_output = personality_output
            self.state.status = decision.summary
            self.state.last_turn_at = now_label
            self.state.last_turn_reason = reason
            committed_agent_output = dict(self.state.agent_output)
            committed_planner_output = dict(self.state.planner_output)
            committed_personality_output = dict(self.state.personality_output)
        self._log_event(
            "turn",
            "state_commit",
            "Turn outputs committed to dashboard state and context files.",
            {
                "reason": reason,
                "last_turn_at": now_label,
                "agent_output": committed_agent_output,
                "planner_output": committed_planner_output,
                "personality_output": committed_personality_output,
                "state": self._debug_state_summary(),
            },
        )
        self._log_event(
            "agent",
            "decision",
            "Agent decision completed.",
            {
                "reason": reason,
                "duration_ms": round((time.perf_counter() - turn_started_perf) * 1000, 1),
                "decision": {
                    "sufficient": decision.sufficient,
                    "focus_state": decision.focus_state,
                    "summary": decision.summary,
                    "response_required": decision.response_required,
                    "response_spoken": personality_result.should_speak,
                    "requested_resources": decision.requested_resources,
                },
            },
        )

    def _should_emit_response(self, decision) -> bool:
        if not decision.response_required:
            return False
        with self.state.lock:
            return not bool(self.state.speech.get("in_progress"))

    def note_speech_started(self, event_id: str, text: str = ""):
        event_id = str(event_id or "").strip()
        with self.state.lock:
            active_event_id = str(self.state.speech.get("event_id", "")).strip()
            if active_event_id and event_id and active_event_id != event_id:
                return {"ok": False, "reason": "event_id_mismatch", "active_event_id": active_event_id}
            current_output = dict(self.state.personality_output or {})
            current_output["should_speak"] = False
            self.state.personality_output = current_output
            self.state.speech = {
                "in_progress": True,
                "event_id": event_id or active_event_id,
                "text": str(text or self.state.speech.get("text", "")),
                "started_at": now_iso(),
                "client_started": True,
                "grace_until_unix": 0.0,
            }
            self.state.status = "Speech narration in progress. Big Brother is waiting before reassessing."
        self.status_file.update(last_intervention_at=now_iso())
        self._log_event(
            "speech",
            "started",
            "Speech narration started.",
            {"event_id": event_id, "text": str(text or "")[:240], "state": self._debug_state_summary()},
        )
        return {"ok": True}

    def note_speech_finished(self, event_id: str = ""):
        event_id = str(event_id or "").strip()
        with self.state.lock:
            active_event_id = str(self.state.speech.get("event_id", "")).strip()
            if event_id and active_event_id and event_id != active_event_id:
                return {"ok": False, "reason": "event_id_mismatch", "active_event_id": active_event_id}
            grace_seconds = max(0, int(POST_SPEECH_GRACE_SECONDS))
            grace_until = time.time() + grace_seconds if grace_seconds > 0 else 0.0
            current_output = dict(self.state.personality_output or {})
            current_output["should_speak"] = False
            self.state.personality_output = current_output
            self.state.speech = {
                "in_progress": False,
                "event_id": "",
                "text": "",
                "started_at": "",
                "client_started": False,
                "grace_until_unix": grace_until,
            }
            self.state.status = (
                f"Speech finished. Waiting {grace_seconds}s before reassessing."
                if grace_seconds > 0
                else "Speech finished. Reassessment may resume immediately."
            )
        self._log_event(
            "speech",
            "finished",
            "Speech narration finished. Grace period started." if grace_seconds > 0 else "Speech narration finished. No post-speech grace window is active.",
            {
                "event_id": event_id or active_event_id,
                "grace_seconds": grace_seconds,
                "state": self._debug_state_summary(),
            },
        )
        return {"ok": True, "grace_seconds": grace_seconds}

    def _arm_speech_lock(self, event_id: str, text: str):
        with self.state.lock:
            self.state.speech = {
                "in_progress": True,
                "event_id": str(event_id or "").strip(),
                "text": str(text or ""),
                "started_at": "",
                "client_started": False,
                "grace_until_unix": 0.0,
            }
            self.state.status = "Speech signal sent. Big Brother is waiting for narration to finish."
        self.status_file.update(last_intervention_at=now_iso())
        self._log_event(
            "speech",
            "armed",
            "Speech signal sent; narration lock engaged immediately.",
            {"event_id": event_id, "text": str(text or "")[:240], "state": self._debug_state_summary()},
        )

    def _queue_requested_resource(self, item: dict):
        request_type = str(item.get("type", "")).strip().lower()
        if request_type in {"browser", "browser_scan", "browser_rag"}:
            self.refresh_browser_export(retries=2, delay_seconds=0.15)
            try:
                self.tab_reader.sync_tab_records()
            except Exception:
                pass
            self._log_event("browser", "rag", "Browser records refreshed from an agent request.", {"reason": item.get("reason", "")})
            return {"type": "browser_rag", "status": "completed", "reason": item.get("reason", "")}

        if request_type in {"screen", "screen_scan"}:
            normalized_type = "screen_scan"
        elif request_type in {"webcam", "webcam_scan"}:
            normalized_type = "webcam_scan"
        else:
            return None

        dedupe_key = str(item.get("dedupe_key", "")).strip()
        if not dedupe_key:
            dedupe_key = f"{normalized_type}:{item.get('source', '')}:{item.get('reason', '')}"
        queued = self.client_actions.enqueue(normalized_type, dict(item), dedupe_key=dedupe_key)
        self._log_event(
            "client_action",
            "enqueue",
            "Client-side resource action queued or deduplicated.",
            {
                "normalized_type": normalized_type,
                "dedupe_key": dedupe_key,
                "request": dict(item),
                "result": queued,
                "pending_count": len(self.client_actions.pending()),
            },
        )
        return queued

    def ingest_stimulus(self, stimulus_type: str, payload: dict | None = None):
        stimulus_type = str(stimulus_type or "").strip()
        allowed = {
            "manual",
            "activity",
            "inactivity",
            "capture_updated",
            "tab_opened",
            "tab_closed",
            "tab_refreshed",
            "heartbeat",
            "todo_due",
        }
        if stimulus_type not in allowed:
            raise ValueError(f"Unknown stimulus type '{stimulus_type}'.")

        payload = dict(payload or {})
        accepted = self.bus.emit(stimulus_type, payload)
        self._log_event(
            "stimulus",
            "ingest",
            "Manual/API stimulus ingest attempted.",
            {
                "stimulus_type": stimulus_type,
                "accepted": accepted,
                "payload": payload,
                "last_stimulus": dict(self.bus.last_stimulus),
                "state": self._debug_state_summary(),
            },
        )
        return {"accepted": accepted, "type": stimulus_type}

    def simulate_stimulus(
        self,
        stimulus_type: str,
        *,
        browser_tabs=None,
        payload: dict | None = None,
        apply_browser_snapshot: bool = True,
        derive_payload_from_tabs: bool = True,
        hold_seconds=None,
        note: str = "",
    ):
        stimulus_type = str(stimulus_type or "").strip()
        normalized_tabs = self._normalize_browser_tabs(browser_tabs)
        simulation_payload = dict(payload or {})
        simulation_info = {
            "applied_browser_snapshot": False,
            "hold_seconds": 0.0,
            "tab_count": len(normalized_tabs),
            "note": str(note or "").strip(),
        }
        if apply_browser_snapshot:
            override_result = self._apply_browser_simulation(
                normalized_tabs,
                hold_seconds=hold_seconds,
                note=note,
                source="simulation_panel",
            )
            self.refresh_browser_export(retries=1, delay_seconds=0.01, log_event=False)
            simulation_info["applied_browser_snapshot"] = True
            simulation_info["hold_seconds"] = float(override_result.get("hold_seconds", 0.0) or 0.0)
        if derive_payload_from_tabs and not simulation_payload:
            simulation_payload = self._default_stimulus_payload_from_tabs(stimulus_type, normalized_tabs)
        result = self.ingest_stimulus(stimulus_type, simulation_payload)
        self._log_event(
            "simulation",
            "stimulus_sent",
            "Simulation panel emitted a fake stimulus.",
            {
                "stimulus_type": stimulus_type,
                "payload": simulation_payload,
                "browser_tabs": normalized_tabs,
                **simulation_info,
                "accepted": bool(result.get("accepted")),
            },
        )
        return {
            "ok": True,
            "stimulus": result,
            "simulation": simulation_info,
        }

    def complete_client_action(self, action_id: str, result: dict | None = None):
        completed = self.client_actions.complete(action_id, result or {})
        if completed:
            self._log_event("client_action", "complete", "Client action completed.", completed)
        return completed

    def _save_tab_record_image(self, data_url: str, tab_id: str):
        if not data_url or not tab_id:
            return ""
        try:
            encoded = data_url.split(",", 1)[1]
            image_bytes = base64.b64decode(encoded)
            path = TAB_RECORDS_DIR / f"{tab_id}.jpg"
            path.write_bytes(image_bytes)
            return str(path.relative_to(APP_DIR))
        except Exception:
            return ""

    def analyze_capture(self, analysis_mode: str, prompt: str, image_data_url: str, metadata: dict | None = None):
        metadata = dict(metadata or {})
        analysis_mode = str(analysis_mode or "").strip().lower()
        if analysis_mode not in MODE_TO_SOURCE_DIR:
            raise ValueError("analysisMode must be 'webcam' or 'screen'.")
        image_data_url = parse_data_url(image_data_url)
        self._log_event(
            "capture",
            "analyze_start",
            "Capture analysis requested.",
            {
                "analysis_mode": analysis_mode,
                "prompt": prompt,
                "metadata": metadata,
                "image_bytes_base64": len(image_data_url),
            },
        )

        image_hash = hashlib.sha1(image_data_url.encode("utf-8")).hexdigest()
        if self._capture_hashes.get(analysis_mode) == image_hash and self._capture_cache.get(analysis_mode):
            summary = self._capture_cache[analysis_mode]
            written_files = write_local_outputs(analysis_mode, prompt.strip(), summary)
            self.ledger.record_skip(
                f"vlm:{analysis_mode}",
                estimate_image_tokens(base64_length=len(image_data_url)) + estimate_text_tokens(prompt) + 200,
            )
            with self.state.lock:
                self.state.resource_revision += 1
                self.state.capture_status = f"{analysis_mode.title()} frame unchanged; cached summary reused."
                self.state.last_analysis = {
                    "analysisMode": analysis_mode,
                    "summary": summary,
                    "writtenFiles": written_files,
                }
                latest_revision = int(self.state.resource_revision)
            self._log_event(
                "capture",
                "analyze_cached",
                "Capture analysis reused a cached summary.",
                {
                    "analysis_mode": analysis_mode,
                    "metadata": metadata,
                    "resource_revision_after": latest_revision,
                    "written_files": written_files,
                },
            )
            return {
                "summary": summary,
                "model": VISION_MODEL,
                "analysisMode": analysis_mode,
                "writtenFiles": written_files,
                "cached": True,
            }

        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            message = f"No API key configured for {analysis_mode} analysis."
            written_files = write_local_outputs(analysis_mode, prompt.strip(), message)
            self._capture_hashes[analysis_mode] = image_hash
            self._capture_cache[analysis_mode] = message
            with self.state.lock:
                self.state.resource_revision += 1
                self.state.capture_status = f"{analysis_mode.title()} summary updated without a model."
                self.state.last_analysis = {
                    "analysisMode": analysis_mode,
                    "summary": message,
                    "writtenFiles": written_files,
                }
                latest_revision = int(self.state.resource_revision)
            self._log_event(
                "capture",
                "analyze_no_model",
                "Capture analysis completed without an external model.",
                {
                    "analysis_mode": analysis_mode,
                    "metadata": metadata,
                    "resource_revision_after": latest_revision,
                    "written_files": written_files,
                },
            )
            return {
                "summary": message,
                "model": "",
                "analysisMode": analysis_mode,
                "writtenFiles": written_files,
                "cached": False,
            }

        request_prompt = build_vision_prompt(analysis_mode, prompt)
        body = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": request_prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            "max_tokens": 300,
        }
        req = request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
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
            raise RuntimeError(f"Vision API request failed.\n\n{error_body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Unable to reach the vision API: {exc.reason}") from exc

        try:
            message = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected vision API response: {response_data}") from exc

        usage = response_data.get("usage") or {}
        self.ledger.record_call(
            f"vlm:{analysis_mode}",
            usage.get("prompt_tokens") or estimate_image_tokens(base64_length=len(image_data_url)) + estimate_text_tokens(request_prompt),
            usage.get("completion_tokens") or estimate_text_tokens(message),
        )
        self._capture_hashes[analysis_mode] = image_hash
        self._capture_cache[analysis_mode] = message
        written_files = write_local_outputs(analysis_mode, prompt.strip(), message)
        tab_record_image = ""
        if analysis_mode == "screen" and metadata.get("tab_id"):
            tab_record_image = self._save_tab_record_image(image_data_url, metadata.get("tab_id"))
        self._refresh_resource_debug()
        with self.state.lock:
            self.state.resource_revision += 1
            self.state.capture_status = f"{analysis_mode.title()} summary updated."
            self.state.last_analysis = {
                "analysisMode": analysis_mode,
                "summary": message,
                "writtenFiles": written_files,
            }
            latest_revision = int(self.state.resource_revision)
        self.memory.append("observation", f"{analysis_mode} summary updated.", meta={"summary": message[:300]})
        self._log_event(
            "capture",
            "analyze_complete",
            "Capture analysis complete.",
            {
                "analysis_mode": analysis_mode,
                "metadata": metadata,
                "summary": message,
                "tab_record_image": tab_record_image,
                "written_files": written_files,
                "resource_revision_after": latest_revision,
            },
        )
        self.bus.emit("capture_updated", {"analysis_mode": analysis_mode})
        return {
            "summary": message,
            "model": VISION_MODEL,
            "analysisMode": analysis_mode,
            "writtenFiles": written_files,
            "tabRecordImage": tab_record_image,
            "cached": False,
        }


WatcherDashboardApp = BigBrotherApp
APP = BigBrotherApp()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "BigBrother/3.0"

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self._send_json(APP.snapshot())
            return
        if parsed.path == "/":
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._send_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self._send_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/personality-latest.mp3":
            self._send_file(PERSONALITY_AUDIO_PATH, "audio/mpeg")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/run-once":
                APP.run_once(
                    goal=body.get("goal"),
                    interval_seconds=body.get("interval_seconds"),
                    duration_seconds=body.get("duration_seconds"),
                    reason=body.get("reason", "manual_run"),
                )
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/start":
                APP.start(
                    goal=body.get("goal"),
                    interval_seconds=body.get("interval_seconds"),
                    duration_seconds=body.get("duration_seconds"),
                    browser_name=body.get("browser_name"),
                    browser_url=body.get("browser_url"),
                )
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/stop":
                APP.stop()
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/reset-stats":
                APP.reset_stats()
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/stimulus":
                result = APP.ingest_stimulus(body.get("type", ""), body.get("payload") or {})
                self._send_json(result)
                return
            if parsed.path == "/api/simulate-stimulus":
                result = APP.simulate_stimulus(
                    body.get("type", ""),
                    browser_tabs=body.get("browser_tabs") or [],
                    payload=body.get("payload") or {},
                    apply_browser_snapshot=bool(body.get("apply_browser_snapshot", True)),
                    derive_payload_from_tabs=bool(body.get("derive_payload_from_tabs", True)),
                    hold_seconds=body.get("hold_seconds"),
                    note=body.get("note", ""),
                )
                self._send_json(result)
                return
            if parsed.path == "/api/clear-simulation":
                result = APP.clear_browser_simulation()
                self._send_json(result)
                return
            if parsed.path == "/api/client-action-complete":
                result = APP.complete_client_action(body.get("action_id", ""), body.get("result") or {})
                self._send_json({"ok": True, "result": result})
                return
            if parsed.path == "/api/export-tabs":
                APP.refresh_browser_export()
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/launch-browser":
                APP.launch_browser(body.get("browser_name", "Edge"), body.get("browser_url", "about:blank"))
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/analyze":
                result = APP.analyze_capture(
                    body.get("analysisMode", ""),
                    body.get("prompt", ""),
                    body.get("imageDataUrl", ""),
                    metadata=body.get("metadata") or {},
                )
                self._send_json(result)
                return
            if parsed.path == "/api/speech-started":
                result = APP.note_speech_started(body.get("event_id", ""), body.get("text", ""))
                self._send_json({"ok": True, "result": result if result else {}})
                return
            if parsed.path == "/api/speech-finished":
                result = APP.note_speech_finished(body.get("event_id", ""))
                self._send_json(result)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            APP._log_event("server", "error", "API request failed.", {"path": parsed.path, "error": str(exc)})
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


class BigBrotherHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _can_bind(host: str, port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _choose_server_port(host: str, requested_port: int, max_attempts: int = 25) -> tuple[int, list[dict]]:
    attempts = []
    for offset in range(max(1, int(max_attempts))):
        port = int(requested_port) + offset
        ok, error_text = _can_bind(host, port)
        attempts.append({"port": port, "ok": ok, "error": error_text})
        if ok:
            return port, attempts
    raise OSError(f"Unable to bind any port from {requested_port} to {requested_port + max(0, int(max_attempts) - 1)} on {host}.")


def run():
    ensure_output_dirs()
    host = os.getenv("BIG_BROTHER_HOST", "127.0.0.1")
    requested_port = env_int("BIG_BROTHER_PORT", 8000)
    port, bind_attempts = _choose_server_port(host, requested_port)
    server = BigBrotherHTTPServer((host, port), RequestHandler)
    if port != requested_port:
        APP._log_event(
            "server",
            "port_fallback",
            "Default port was unavailable, so Big Brother moved to a fallback port.",
            {"host": host, "requested_port": requested_port, "selected_port": port, "attempts": bind_attempts},
        )
        print(f"Big Brother default port {requested_port} was unavailable; using http://{host}:{port}")
    else:
        print(f"Big Brother running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
