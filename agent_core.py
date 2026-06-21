"""Agent primitives for the BigBrother agent framework.

TokenLedger, AgentMemory, StatusFile, TodoList, StimulusBus — see AGENT_SPEC.md.
"""

import json
import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR / "state"

LEDGER_PATH = STATE_DIR / "token_ledger.json"
MEMORY_PATH = STATE_DIR / "memory.jsonl"
STATUS_PATH = STATE_DIR / "status.json"
TODOS_PATH = STATE_DIR / "todos.json"
CURRENT_CONTEXT_PATH = STATE_DIR / "context_current.json"
HISTORIC_CONTEXT_PATH = STATE_DIR / "context_history.jsonl"
CLIENT_ACTIONS_PATH = STATE_DIR / "client_actions.json"


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def estimate_text_tokens(text) -> int:
    return max(1, len(str(text or "")) // 4)


def estimate_image_tokens(width: int = 0, height: int = 0, base64_length: int = 0) -> int:
    # Qwen-VL style: one token per 28x28 patch. Fall back to a size heuristic
    # (~200 KB JPEG at 1280x720 corresponds to ~1,170 tokens).
    if width and height:
        return max(64, (int(width) // 28) * (int(height) // 28))
    if base64_length:
        return max(64, int(base64_length * 0.75) // 170)
    return 1000


class TokenLedger:
    """Per-component call/skip counters with token accounting."""

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.components = {}
        self.started_at = now_iso()
        self._last_flush = 0.0

    def _component(self, name: str) -> dict:
        return self.components.setdefault(
            name,
            {
                "calls": 0,
                "skipped_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "estimated_tokens_saved": 0,
            },
        )

    def record_call(self, component: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        with self.lock:
            entry = self._component(component)
            entry["calls"] += 1
            entry["prompt_tokens"] += max(0, int(prompt_tokens or 0))
            entry["completion_tokens"] += max(0, int(completion_tokens or 0))
            self._flush_locked()

    def record_skip(self, component: str, estimated_tokens_saved: int = 0):
        with self.lock:
            entry = self._component(component)
            entry["skipped_calls"] += 1
            entry["estimated_tokens_saved"] += max(0, int(estimated_tokens_saved or 0))
            self._flush_locked()

    def snapshot(self) -> dict:
        with self.lock:
            total_used = sum(
                entry["prompt_tokens"] + entry["completion_tokens"]
                for entry in self.components.values()
            )
            total_saved = sum(
                entry["estimated_tokens_saved"] for entry in self.components.values()
            )
            total_calls = sum(entry["calls"] for entry in self.components.values())
            total_skipped = sum(entry["skipped_calls"] for entry in self.components.values())
            multiplier = (
                round((total_used + total_saved) / total_used, 2) if total_used else 0.0
            )
            return {
                "started_at": self.started_at,
                "components": {name: dict(entry) for name, entry in self.components.items()},
                "total_calls": total_calls,
                "total_skipped_calls": total_skipped,
                "total_tokens_used": total_used,
                "total_estimated_tokens_saved": total_saved,
                "efficiency_multiplier": multiplier,
            }

    def _flush_locked(self, min_interval_seconds: float = 2.0):
        now = time.time()
        if now - self._last_flush < min_interval_seconds:
            return
        self._last_flush = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.snapshot_unlocked(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def snapshot_unlocked(self) -> dict:
        # Caller must hold the lock (RLock makes the public snapshot safe too).
        return self.snapshot()


class AgentMemory:
    """Append-only timestamped memory with simple keyword recall."""

    def __init__(self, path: Path = MEMORY_PATH, max_in_memory: int = 500):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.max_in_memory = max_in_memory
        self.entries = self._load_tail()

    def _load_tail(self):
        if not self.path.exists():
            return []
        entries = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return entries[-self.max_in_memory :]

    def append(self, kind: str, text: str, meta: dict | None = None) -> dict:
        entry = {"timestamp": now_iso(), "kind": str(kind), "text": str(text)}
        if meta:
            entry["meta"] = meta
        with self.lock:
            self.entries.append(entry)
            self.entries = self.entries[-self.max_in_memory :]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return entry

    def recent(self, limit: int = 12):
        with self.lock:
            return list(self.entries[-max(1, int(limit)) :])

    def recall(self, query: str, limit: int = 6):
        terms = {term for term in str(query or "").lower().split() if len(term) > 2}
        if not terms:
            return self.recent(limit)
        scored = []
        with self.lock:
            for entry in self.entries:
                haystack = f"{entry.get('kind', '')} {entry.get('text', '')}".lower()
                score = sum(1 for term in terms if term in haystack)
                if score:
                    scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[: max(1, int(limit))]]


class StatusFile:
    """The agent's current model of the user and session state."""

    def __init__(self, path: Path = STATUS_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {
            "updated_at": now_iso(),
            "focus_state": "unknown",
            "last_activity_at": "",
            "last_stimulus": "",
            "last_stimulus_at": "",
            "last_stimulus_payload": {},
            "last_turn_at": "",
            "last_turn_reason": "",
            "last_intervention_at": "",
            "notes": "",
        }
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.data.update(stored)
        except (OSError, json.JSONDecodeError):
            pass

    def get(self) -> dict:
        with self.lock:
            return dict(self.data)

    def update(self, **fields) -> dict:
        with self.lock:
            self.data.update(fields)
            self.data["updated_at"] = now_iso()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            except OSError:
                pass
            return dict(self.data)


class TodoList:
    """Agent-settable alarms: due items become `todo_due` stimuli."""

    def __init__(self, path: Path = TODOS_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, list):
                return [item for item in stored if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, indent=2), encoding="utf-8")
        except OSError:
            pass

    def add(self, note: str, due_in_seconds: float, kind: str = "scheduled") -> dict:
        item = {
            "id": uuid.uuid4().hex[:10],
            "note": str(note),
            "kind": str(kind),
            "created_at": now_iso(),
            "due_at_unix": time.time() + max(0.0, float(due_in_seconds)),
        }
        with self.lock:
            self.items.append(item)
            self._save_locked()
        return item

    def pop_due(self, now_unix: float | None = None):
        now_unix = time.time() if now_unix is None else float(now_unix)
        with self.lock:
            due = [item for item in self.items if item.get("due_at_unix", 0) <= now_unix]
            if due:
                self.items = [
                    item for item in self.items if item.get("due_at_unix", 0) > now_unix
                ]
                self._save_locked()
            return due

    def list_all(self):
        with self.lock:
            return [dict(item) for item in self.items]


class ContextFiles:
    """Current + historic context snapshots for the main agent."""

    def __init__(
        self,
        current_path: Path = CURRENT_CONTEXT_PATH,
        historic_path: Path = HISTORIC_CONTEXT_PATH,
        max_history: int = 200,
    ):
        self.current_path = Path(current_path)
        self.historic_path = Path(historic_path)
        self.max_history = max_history
        self.lock = threading.RLock()
        self.current = self._load_current()
        self.history = self._load_history()

    def _default_current(self):
        return {
            "updated_at": now_iso(),
            "focus_state": "unknown",
            "summary": "",
            "active_notes": [],
            "last_stimulus": "",
            "last_turn_reason": "",
            "resource_summary": {},
            "open_tab_ids": [],
        }

    def _load_current(self):
        if not self.current_path.exists():
            return self._default_current()
        try:
            data = json.loads(self.current_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                baseline = self._default_current()
                baseline.update(data)
                return baseline
        except (OSError, json.JSONDecodeError):
            pass
        return self._default_current()

    def _load_history(self):
        if not self.historic_path.exists():
            return []
        entries = []
        try:
            for line in self.historic_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return entries[-self.max_history :]

    def get_current(self):
        with self.lock:
            return dict(self.current)

    def recent_history(self, limit: int = 8):
        with self.lock:
            return list(self.history[-max(1, int(limit)) :])

    def write_snapshot(self, summary: str, *, focus_state: str = "", notes=None, meta=None):
        notes = [str(item).strip() for item in (notes or []) if str(item).strip()]
        meta = dict(meta or {})
        entry = {
            "timestamp": now_iso(),
            "summary": str(summary or ""),
            "focus_state": str(focus_state or self.current.get("focus_state", "unknown")),
            "notes": notes,
            "meta": meta,
        }
        with self.lock:
            self.current.update(
                {
                    "updated_at": entry["timestamp"],
                    "summary": entry["summary"],
                    "focus_state": entry["focus_state"],
                    "active_notes": notes,
                }
            )
            for key, value in meta.items():
                self.current[key] = value
            self.history.append(entry)
            self.history = self.history[-self.max_history :]
            self._flush_locked(entry)
        return entry

    def _flush_locked(self, entry):
        try:
            self.current_path.parent.mkdir(parents=True, exist_ok=True)
            self.current_path.write_text(json.dumps(self.current, indent=2), encoding="utf-8")
            with self.historic_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


class ClientActionQueue:
    """Queue of client-side capture/resource actions requested by the agent."""

    def __init__(self, path: Path = CLIENT_ACTIONS_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, list):
                return [item for item in stored if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, indent=2), encoding="utf-8")
        except OSError:
            pass

    def enqueue(self, action_type: str, payload: dict | None = None, *, dedupe_key: str = ""):
        payload = dict(payload or {})
        action_type = str(action_type or "").strip()
        dedupe_key = str(dedupe_key or "").strip()
        with self.lock:
            for item in self.items:
                if item.get("completed_at"):
                    continue
                if dedupe_key and item.get("dedupe_key") == dedupe_key:
                    return dict(item)
            item = {
                "id": uuid.uuid4().hex[:10],
                "type": action_type,
                "payload": payload,
                "dedupe_key": dedupe_key,
                "created_at": now_iso(),
                "completed_at": "",
                "result": {},
            }
            self.items.append(item)
            self._save_locked()
            return dict(item)

    def pending(self):
        with self.lock:
            return [dict(item) for item in self.items if not item.get("completed_at")]

    def complete(self, action_id: str, result: dict | None = None):
        action_id = str(action_id or "").strip()
        with self.lock:
            for item in self.items:
                if item.get("id") == action_id and not item.get("completed_at"):
                    item["completed_at"] = now_iso()
                    item["result"] = dict(result or {})
                    self._save_locked()
                    return dict(item)
        return {}


class StimulusBus:
    """Thread-safe stimulus queue with per-type debounce windows."""

    DEFAULT_DEBOUNCE_SECONDS = {
        "tab_opened": 2.0,
        "tab_closed": 2.0,
        "tab_refreshed": 2.0,
        "frame_unchanged": 0.0,
        "inactivity": 10.0,
        "activity": 2.0,
    }

    def __init__(self, debounce_overrides: dict | None = None):
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.debounce = dict(self.DEFAULT_DEBOUNCE_SECONDS)
        if debounce_overrides:
            self.debounce.update(debounce_overrides)
        self.last_emitted_at = {}
        self.last_stimulus = {}

    def emit(self, stimulus_type: str, payload: dict | None = None) -> bool:
        stimulus_type = str(stimulus_type)
        now = time.time()
        with self.lock:
            window = float(self.debounce.get(stimulus_type, 0.0))
            last = self.last_emitted_at.get(stimulus_type, 0.0)
            if window > 0 and now - last < window:
                return False
            self.last_emitted_at[stimulus_type] = now
            stimulus = {
                "type": stimulus_type,
                "payload": payload or {},
                "emitted_at": now_iso(),
                "emitted_at_unix": now,
            }
            self.last_stimulus = stimulus
        self.queue.put(stimulus)
        return True

    def get(self, timeout: float = 1.0):
        try:
            return self.queue.get(timeout=max(0.05, float(timeout)))
        except queue.Empty:
            return None
