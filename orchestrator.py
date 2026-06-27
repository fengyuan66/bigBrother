import json
import threading
import time
from dataclasses import dataclass

from config import env_int


TURN_STIMULI = {
    "tab_opened",
    "tab_closed",
    "tab_refreshed",
    "capture_updated",
    "inactivity",
    "activity",
    "todo_due",
    "heartbeat",
    "manual",
}

STIMULUS_PRIORITY = {
    "manual": 100,
    "tab_opened": 90,
    "tab_refreshed": 90,
    "tab_closed": 90,
    "todo_due": 80,
    "inactivity": 70,
    "activity": 60,
    "capture_updated": 50,
    "heartbeat": 10,
}

CONTENT_CHANGE_STIMULI = {
    "tab_opened",
    "tab_refreshed",
    "tab_closed",
    "capture_updated",
}


@dataclass
class StimulusTicket:
    type: str
    payload: dict
    emitted_at: str
    priority: int


class AgentOrchestrator(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="agent-orchestrator")
        self.app = app
        self.bus = app.bus
        self.memory = app.memory
        self.status = app.status_file
        self.todos = app.todos
        self.stop_event = threading.Event()
        self.heartbeat_seconds = env_int("BIG_BROTHER_HEARTBEAT_SECONDS", 30)
        self.tab_poll_seconds = max(2, env_int("BIG_BROTHER_TAB_POLL_SECONDS", 5))
        self.last_turn_started = 0.0
        self.last_tab_poll = 0.0
        self.last_heartbeat_emitted_at = 0.0
        self.pending_ticket = None
        self._tab_signature = None
        self._warmup_until = 0.0
        self._turns_lock = threading.RLock()
        self._active_turns = {}
        self._turn_sequence = 0
        self.max_parallel_content_turns = 2

    def _tabs_signature(self, tabs: list[dict]) -> dict:
        signature = {}
        for tab in tabs:
            tab_id = str(tab.get("id") or tab.get("url") or "")
            if not tab_id:
                continue
            signature[tab_id] = {
                "url": str(tab.get("url", "")),
                "title": str(tab.get("title", "")),
            }
        return signature

    def _signature_preview(self, signature: dict, limit: int = 4) -> list[dict]:
        preview = []
        for tab_id, payload in list((signature or {}).items())[: max(1, int(limit))]:
            preview.append(
                {
                    "id": tab_id,
                    "title": str((payload or {}).get("title", ""))[:120],
                    "url": str((payload or {}).get("url", ""))[:180],
                }
            )
        return preview

    def _seed_tab_signature(self):
        tabs = self.app.refresh_browser_export(retries=1, delay_seconds=0.05, log_event=False)
        self._tab_signature = self._tabs_signature(tabs)
        self.app._log_event(
            "browser",
            "baseline",
            "Browser tab baseline seeded for change detection.",
            {"count": len(self._tab_signature), "tabs": self._signature_preview(self._tab_signature)},
        )

    def sync_tab_signature(self, tabs: list[dict], source: str = ""):
        self._tab_signature = self._tabs_signature(tabs)
        self.app._log_event(
            "browser",
            "baseline_sync",
            "Browser tab baseline synced from a fresh browser event snapshot.",
            {
                "source": str(source or "unknown"),
                "count": len(self._tab_signature),
                "tabs": self._signature_preview(self._tab_signature),
            },
        )

    def stop(self):
        self.stop_event.set()

    def _running(self) -> bool:
        with self.app.state.lock:
            return bool(self.app.state.running)

    def _is_content_change_type(self, stimulus_type: str) -> bool:
        return str(stimulus_type or "").strip() in CONTENT_CHANGE_STIMULI

    def _normalize_content_payload(self, stimulus_type: str, payload: dict) -> dict:
        stimulus_type = str(stimulus_type or "").strip()
        payload = dict(payload or {})
        if stimulus_type in {"tab_opened", "tab_refreshed", "tab_closed"}:
            tabs = []
            for tab in list(payload.get("tabs") or []):
                tab = dict(tab or {})
                tabs.append(
                    {
                        "id": str(tab.get("id", "")).strip(),
                        "title": str(tab.get("title", "")).strip(),
                        "url": str(tab.get("url", "")).strip(),
                        "domain": str(tab.get("domain", "")).strip(),
                    }
                )
            tabs.sort(key=lambda item: (item["id"], item["url"], item["title"]))
            tab_ids = sorted(str(tab_id or "").strip() for tab_id in list(payload.get("tab_ids") or []))
            return {"tabs": tabs, "tab_ids": tab_ids}
        if stimulus_type == "capture_updated":
            return {"analysis_mode": str(payload.get("analysis_mode", "")).strip().lower()}
        return {}

    def _content_key(self, stimulus_type: str, payload: dict) -> str:
        normalized = self._normalize_content_payload(stimulus_type, payload)
        if not normalized:
            return ""
        return f"{str(stimulus_type or '').strip()}:{json.dumps(normalized, sort_keys=True, separators=(',', ':'))}"

    def _cleanup_finished_turns(self):
        finished = []
        with self._turns_lock:
            for token, meta in list(self._active_turns.items()):
                thread = meta.get("thread")
                if thread is None or thread.is_alive():
                    continue
                finished.append((token, dict(meta)))
                self._active_turns.pop(token, None)
        for token, meta in finished:
            self.app._log_event(
                "turn",
                "thread_complete",
                "Turn worker completed.",
                {
                    "token": token,
                    "reason": meta.get("reason", ""),
                    "type": meta.get("type", ""),
                    "priority": meta.get("priority", 0),
                    "content_change": bool(meta.get("content_change")),
                },
            )

    def _start_turn_worker(self, ticket: StimulusTicket, now: float):
        with self._turns_lock:
            self._turn_sequence += 1
            token = self._turn_sequence
        reason = f"stimulus:{ticket.type}"

        def _runner():
            try:
                self.app.run_turn(reason=reason)
            except Exception as exc:
                self.app._log_event("turn", "error", "Agent turn failed.", {"reason": reason, "error": str(exc)})

        thread = threading.Thread(target=_runner, daemon=True, name=f"agent-turn-{token}")
        with self._turns_lock:
            self._active_turns[token] = {
                "thread": thread,
                "reason": reason,
                "type": ticket.type,
                "priority": ticket.priority,
                "emitted_at": ticket.emitted_at,
                "payload": dict(ticket.payload),
                "content_change": self._is_content_change_type(ticket.type),
                "content_key": self._content_key(ticket.type, ticket.payload),
                "started_at_unix": now,
            }
        self.last_turn_started = now
        self.last_heartbeat_emitted_at = 0.0
        self.app._log_event(
            "turn",
            "start",
            "Agent turn starting from queued ticket.",
            {"reason": reason, "payload": dict(ticket.payload), "priority": ticket.priority, "token": token},
        )
        thread.start()

    def _min_turn_spacing(self) -> float:
        with self.app.state.lock:
            return float(max(1, self.app.state.interval_seconds))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.app._log_event("orchestrator", "error", "Orchestrator loop error.", {"error": str(exc)})
                time.sleep(1.0)

    def _tick(self):
        now = time.time()
        self.app.expire_session_if_needed()
        self._cleanup_finished_turns()

        for item in self.todos.pop_due(now):
            emitted = self.bus.emit("todo_due", {"todo": item})
            self.app._log_event(
                "orchestrator",
                "todo_due_emit",
                "Due todo converted into a stimulus.",
                {"todo": item, "accepted": emitted},
            )

        if self._running() and now - self.last_tab_poll >= self.tab_poll_seconds:
            self.last_tab_poll = now
            self._poll_tabs()

        self._maybe_emit_heartbeat(now)

        stimulus = self.bus.get(timeout=0.5)
        if stimulus is not None:
            self._handle_stimulus(stimulus)

        if self.app.assessment_paused():
            if self.pending_ticket is not None:
                self.app._log_event(
                    "orchestrator",
                    "paused_with_pending_ticket",
                    "Assessment pause is active; pending ticket remains queued.",
                    {
                        "pending_ticket": {
                            "type": self.pending_ticket.type,
                            "priority": self.pending_ticket.priority,
                            "emitted_at": self.pending_ticket.emitted_at,
                            "payload": dict(self.pending_ticket.payload),
                        }
                    },
                )
            return

        self._run_pending_turn_if_allowed()

    def _maybe_emit_heartbeat(self, now: float):
        if not self._running() or not self.last_turn_started:
            return
        due_at = self.last_turn_started + self.heartbeat_seconds
        if now < due_at:
            return
        if self.last_heartbeat_emitted_at >= due_at:
            return
        if self.pending_ticket is not None and self.pending_ticket.priority >= STIMULUS_PRIORITY["heartbeat"]:
            return
        emitted = self.bus.emit("heartbeat", {"idle_seconds": round(now - self.last_turn_started, 1)})
        if emitted:
            self.last_heartbeat_emitted_at = now
            self.app._log_event(
                "orchestrator",
                "heartbeat_emit",
                "Heartbeat stimulus emitted.",
                {"idle_seconds": round(now - self.last_turn_started, 1), "due_at": due_at},
            )

    def _poll_tabs(self):
        poll_started_perf = time.perf_counter()
        tabs = self.app.refresh_browser_export(retries=1, delay_seconds=0.1, log_event=False)
        signature = self._tabs_signature(tabs)
        previous = self._tab_signature
        self._tab_signature = signature
        if previous is None:
            self.app._log_event(
                "browser",
                "baseline",
                "Browser tab baseline created during polling.",
                {
                    "count": len(signature),
                    "tabs": self._signature_preview(signature),
                    "poll_duration_ms": round((time.perf_counter() - poll_started_perf) * 1000, 1),
                },
            )
            return
        if time.time() < self._warmup_until:
            self.app._log_event(
                "browser",
                "warmup_skip",
                "Browser diff observed during warmup; no stimuli emitted.",
                {
                    "count": len(signature),
                    "previous_count": len(previous),
                    "tabs": self._signature_preview(signature),
                    "poll_duration_ms": round((time.perf_counter() - poll_started_perf) * 1000, 1),
                },
            )
            return

        opened = [tab for tab in tabs if str(tab.get("id") or tab.get("url") or "") not in previous]
        closed = [tab_id for tab_id in previous if tab_id not in signature]
        refreshed = []
        for tab in tabs:
            tab_id = str(tab.get("id") or tab.get("url") or "")
            if tab_id not in previous:
                continue
            before = previous[tab_id]
            after = signature[tab_id]
            if before != after:
                refreshed.append(tab)

        if opened or closed or refreshed:
            self.app._clear_speech_grace_if_active(
                reason="Browser poll detected fresher browser evidence after narration finished.",
                replacement_stimulus="browser_poll_delta",
            )
            self.app._log_event(
                "browser",
                "diff",
                "Browser tab delta detected.",
                {
                    "opened": [
                        {"id": str(tab.get("id", "")), "title": tab.get("title", ""), "url": tab.get("url", "")}
                        for tab in opened[:5]
                    ],
                    "closed": closed[:5],
                    "refreshed": [
                        {"id": str(tab.get("id", "")), "title": tab.get("title", ""), "url": tab.get("url", "")}
                        for tab in refreshed[:5]
                    ],
                    "previous_tabs": self._signature_preview(previous),
                    "current_tabs": self._signature_preview(signature),
                    "poll_duration_ms": round((time.perf_counter() - poll_started_perf) * 1000, 1),
                },
            )
        else:
            self.app._log_event(
                "browser",
                "poll_no_change",
                "Browser poll completed with no tab delta.",
                {
                    "count": len(signature),
                    "tabs": self._signature_preview(signature),
                    "poll_duration_ms": round((time.perf_counter() - poll_started_perf) * 1000, 1),
                },
            )

        if opened:
            self.bus.emit(
                "tab_opened",
                {
                    "count": len(opened),
                    "tabs": [
                        {"id": str(tab.get("id", "")), "url": tab.get("url", ""), "title": tab.get("title", "")}
                        for tab in opened[:5]
                    ],
                },
            )
        if closed:
            self.bus.emit("tab_closed", {"count": len(closed), "tab_ids": closed[:5]})
        if refreshed:
            self.bus.emit(
                "tab_refreshed",
                {
                    "count": len(refreshed),
                    "tabs": [
                        {"id": str(tab.get("id", "")), "url": tab.get("url", ""), "title": tab.get("title", "")}
                        for tab in refreshed[:5]
                    ],
                },
            )

    def _handle_stimulus(self, stimulus: dict):
        stimulus_type = str(stimulus.get("type", "")).strip()
        payload = dict(stimulus.get("payload", {}) or {})

        self.memory.append("stimulus", f"Stimulus received: {stimulus_type}", meta=payload)
        self.status.update(
            last_stimulus=stimulus_type,
            last_stimulus_at=stimulus.get("emitted_at", ""),
            last_stimulus_payload=payload,
        )
        self.app._log_event("agent", "stimulus", f"Stimulus: {stimulus_type}", {"type": stimulus_type, **payload})

        if stimulus_type == "inactivity":
            self.status.update(focus_state="inactive", notes="Inactivity signal received.")
        elif stimulus_type == "activity":
            self.status.update(focus_state="active", last_activity_at=stimulus.get("emitted_at", ""))

        if stimulus_type in TURN_STIMULI and self._running():
            if stimulus_type == "heartbeat" and self.pending_ticket is not None:
                self.app._log_event(
                    "turn",
                    "queue_skip",
                    "Heartbeat stimulus ignored because a higher-value pending ticket already exists.",
                    {
                        "incoming_type": stimulus_type,
                        "pending_ticket": {
                            "type": self.pending_ticket.type,
                            "priority": self.pending_ticket.priority,
                            "emitted_at": self.pending_ticket.emitted_at,
                        },
                    },
                )
                return
            ticket = StimulusTicket(
                type=stimulus_type,
                payload=payload,
                emitted_at=stimulus.get("emitted_at", ""),
                priority=STIMULUS_PRIORITY.get(stimulus_type, 0),
            )
            incoming_content_key = self._content_key(ticket.type, ticket.payload)
            if incoming_content_key:
                pending_content_key = self._content_key(self.pending_ticket.type, self.pending_ticket.payload) if self.pending_ticket else ""
                with self._turns_lock:
                    matching_active = [
                        {
                            "token": token,
                            "type": meta.get("type", ""),
                            "reason": meta.get("reason", ""),
                            "started_at_unix": meta.get("started_at_unix", 0.0),
                        }
                        for token, meta in self._active_turns.items()
                        if meta.get("content_key") == incoming_content_key
                    ]
                if pending_content_key == incoming_content_key or matching_active:
                    self.app._log_event(
                        "turn",
                        "queue_skip_no_change",
                        "Incoming content-change stimulus matched content already pending or already being processed.",
                        {
                            "incoming_ticket": {
                                "type": ticket.type,
                                "priority": ticket.priority,
                                "emitted_at": ticket.emitted_at,
                                "payload": dict(ticket.payload),
                            },
                            "matching_pending_ticket": (
                                {
                                    "type": self.pending_ticket.type,
                                    "priority": self.pending_ticket.priority,
                                    "emitted_at": self.pending_ticket.emitted_at,
                                    "payload": dict(self.pending_ticket.payload),
                                }
                                if pending_content_key == incoming_content_key and self.pending_ticket is not None
                                else None
                            ),
                            "matching_active_turns": matching_active,
                        },
                    )
                    return
            previous_ticket = self.pending_ticket
            if (
                self.pending_ticket is None
                or ticket.priority > self.pending_ticket.priority
                or (ticket.priority == self.pending_ticket.priority and ticket.emitted_at >= self.pending_ticket.emitted_at)
            ):
                self.pending_ticket = ticket
                self.app._log_event(
                    "turn",
                    "queued",
                    "Agent turn ticket queued.",
                    {
                        "type": ticket.type,
                        "priority": ticket.priority,
                        "payload": dict(ticket.payload),
                        "replaced_ticket": (
                            {
                                "type": previous_ticket.type,
                                "priority": previous_ticket.priority,
                                "emitted_at": previous_ticket.emitted_at,
                                "payload": dict(previous_ticket.payload),
                            }
                            if previous_ticket is not None
                            else None
                        ),
                    },
                )
                if self._is_content_change_type(stimulus_type):
                    with self._turns_lock:
                        active_turns = len(self._active_turns)
                    if active_turns:
                        self.app._log_event(
                            "turn",
                            "supersede_requested",
                            "Newer content-change stimulus arrived while another turn is still running.",
                            {
                                "incoming_ticket": {
                                    "type": ticket.type,
                                    "priority": ticket.priority,
                                    "emitted_at": ticket.emitted_at,
                                    "payload": dict(ticket.payload),
                                },
                                "active_turns": active_turns,
                            },
                        )
            else:
                self.app._log_event(
                    "turn",
                    "queue_skip",
                    "Incoming stimulus did not replace the existing pending ticket.",
                    {
                        "incoming_ticket": {
                            "type": ticket.type,
                            "priority": ticket.priority,
                            "emitted_at": ticket.emitted_at,
                            "payload": dict(ticket.payload),
                        },
                        "kept_ticket": {
                            "type": self.pending_ticket.type,
                            "priority": self.pending_ticket.priority,
                            "emitted_at": self.pending_ticket.emitted_at,
                            "payload": dict(self.pending_ticket.payload),
                        },
                    },
                )

    def _run_pending_turn_if_allowed(self):
        if self.pending_ticket is None or not self._running():
            return

        pending_content_key = self._content_key(self.pending_ticket.type, self.pending_ticket.payload)
        with self._turns_lock:
            active_count = len(self._active_turns)
            duplicate_active = any(
                (meta.get("type") == self.pending_ticket.type and meta.get("emitted_at") == self.pending_ticket.emitted_at)
                or (pending_content_key and meta.get("content_key") == pending_content_key)
                for meta in self._active_turns.values()
            )
        if duplicate_active:
            return

        immediate_supersede = active_count > 0 and self._is_content_change_type(self.pending_ticket.type)
        if active_count > 0 and not immediate_supersede:
            self.app._log_event(
                "turn",
                "wait_active",
                "Pending ticket is waiting for the currently running turn worker to finish.",
                {
                    "pending_ticket": {
                        "type": self.pending_ticket.type,
                        "priority": self.pending_ticket.priority,
                        "emitted_at": self.pending_ticket.emitted_at,
                    },
                    "active_turns": active_count,
                },
            )
            return
        if immediate_supersede and active_count >= self.max_parallel_content_turns:
            self.app._log_event(
                "turn",
                "wait_parallel_limit",
                "Newer content-change ticket is queued, but the parallel turn limit is already in use.",
                {
                    "pending_ticket": {
                        "type": self.pending_ticket.type,
                        "priority": self.pending_ticket.priority,
                        "emitted_at": self.pending_ticket.emitted_at,
                    },
                    "active_turns": active_count,
                    "parallel_limit": self.max_parallel_content_turns,
                },
            )
            return

        spacing = 0.0 if (self.pending_ticket.priority >= 90 or immediate_supersede) else self._min_turn_spacing()
        now = time.time()
        if now - self.last_turn_started < spacing:
            self.app._log_event(
                "turn",
                "spacing_wait",
                "Pending ticket is waiting for minimum turn spacing.",
                {
                    "pending_ticket": {
                        "type": self.pending_ticket.type,
                        "priority": self.pending_ticket.priority,
                        "emitted_at": self.pending_ticket.emitted_at,
                    },
                    "elapsed_since_last_turn": round(now - self.last_turn_started, 3),
                    "required_spacing": spacing,
                },
            )
            return

        ticket = self.pending_ticket
        self.pending_ticket = None
        self._start_turn_worker(ticket, now)

    def note_session_started(self):
        self.last_turn_started = 0.0
        self.last_tab_poll = 0.0
        self.last_heartbeat_emitted_at = 0.0
        with self._turns_lock:
            self._active_turns = {}
            self._turn_sequence = 0
        pending_ticket = StimulusTicket(type="manual", payload={}, emitted_at="", priority=100)
        self.pending_ticket = pending_ticket
        self._seed_tab_signature()
        self._warmup_until = time.time() + 0.5
        self.status.update(focus_state="active", notes="Session started.")
        self.memory.append("session", "Monitoring session started.")
        self.app._log_event(
            "orchestrator",
            "session_started",
            "Orchestrator session bookkeeping initialized.",
            {
                "pending_ticket": {
                    "type": pending_ticket.type,
                    "priority": pending_ticket.priority,
                },
                "warmup_until_unix": self._warmup_until,
                "heartbeat_seconds": self.heartbeat_seconds,
                "tab_poll_seconds": self.tab_poll_seconds,
            },
        )

    def note_session_stopped(self):
        self.pending_ticket = None
        self.last_heartbeat_emitted_at = 0.0
        self._tab_signature = None
        with self._turns_lock:
            self._active_turns = {}
        self.status.update(focus_state="unknown", notes="Session stopped.")
        self.memory.append("session", "Monitoring session stopped.")
        self.app._log_event(
            "orchestrator",
            "session_stopped",
            "Orchestrator session bookkeeping cleared.",
            {"heartbeat_seconds": self.heartbeat_seconds, "tab_poll_seconds": self.tab_poll_seconds},
        )
