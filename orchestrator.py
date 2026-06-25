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

    def stop(self):
        self.stop_event.set()

    def _running(self) -> bool:
        with self.app.state.lock:
            return bool(self.app.state.running)

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

        for item in self.todos.pop_due(now):
            self.bus.emit("todo_due", {"todo": item})

        if self._running() and now - self.last_tab_poll >= self.tab_poll_seconds:
            self.last_tab_poll = now
            self._poll_tabs()

        self._maybe_emit_heartbeat(now)

        stimulus = self.bus.get(timeout=0.5)
        if stimulus is not None:
            self._handle_stimulus(stimulus)

        if self.app.assessment_paused():
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
                return
            ticket = StimulusTicket(
                type=stimulus_type,
                payload=payload,
                emitted_at=stimulus.get("emitted_at", ""),
                priority=STIMULUS_PRIORITY.get(stimulus_type, 0),
            )
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
                    },
                )

    def _run_pending_turn_if_allowed(self):
        if self.pending_ticket is None or not self._running():
            return

        spacing = 0.0 if self.pending_ticket.priority >= 90 else self._min_turn_spacing()
        now = time.time()
        if now - self.last_turn_started < spacing:
            return

        ticket = self.pending_ticket
        self.pending_ticket = None
        self.last_turn_started = now
        self.last_heartbeat_emitted_at = 0.0
        reason = f"stimulus:{ticket.type}"
        self.app._log_event(
            "turn",
            "start",
            "Agent turn starting from queued ticket.",
            {"reason": reason, "payload": dict(ticket.payload), "priority": ticket.priority},
        )
        try:
            self.app.run_turn(reason=reason)
            self.status.update(last_turn_at=self.app.state.last_turn_at, last_turn_reason=reason)
        except Exception as exc:
            self.app._log_event("turn", "error", "Agent turn failed.", {"reason": reason, "error": str(exc)})

    def note_session_started(self):
        self.last_turn_started = 0.0
        self.last_tab_poll = 0.0
        self.last_heartbeat_emitted_at = 0.0
        self.pending_ticket = StimulusTicket(type="manual", payload={}, emitted_at="", priority=100)
        self._seed_tab_signature()
        self._warmup_until = time.time() + 0.5
        self.status.update(focus_state="active", notes="Session started.")
        self.memory.append("session", "Monitoring session started.")

    def note_session_stopped(self):
        self.pending_ticket = None
        self.last_heartbeat_emitted_at = 0.0
        self._tab_signature = None
        self.status.update(focus_state="unknown", notes="Session stopped.")
        self.memory.append("session", "Monitoring session stopped.")
