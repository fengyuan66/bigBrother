import threading
import time
from dataclasses import dataclass
import threading

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
        self.tab_poll_seconds = env_int("BIG_BROTHER_TAB_POLL_SECONDS", 2)
        self.last_turn_started = 0.0
        self.last_tab_poll = 0.0
        self.pending_ticket = None
        self._tab_signature = None
        self._warmup_until = 0.0

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

        if self._running() and self.last_turn_started and now - self.last_turn_started >= self.heartbeat_seconds:
            self.bus.emit("heartbeat", {"idle_seconds": round(now - self.last_turn_started, 1)})

        stimulus = self.bus.get(timeout=0.5)
        if stimulus is not None:
            self._handle_stimulus(stimulus)

        if self.app.assessment_paused():
            return

        self._run_pending_turn_if_allowed()

    def _poll_tabs(self):
        tabs = self.app.refresh_browser_export(retries=1, delay_seconds=0.1, log_event=False)
        signature = {}
        for tab in tabs:
            tab_id = str(tab.get("id") or tab.get("url") or "")
            signature[tab_id] = {
                "url": str(tab.get("url", "")),
                "title": str(tab.get("title", "")),
            }

        previous = self._tab_signature
        self._tab_signature = signature
        if previous is None:
            return
        if time.time() < self._warmup_until:
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

        if stimulus_type == "frame_unchanged":
            return

        if stimulus_type == "inactivity":
            self.status.update(focus_state="inactive", notes="Inactivity signal received.")
        elif stimulus_type == "activity":
            self.status.update(focus_state="active", last_activity_at=stimulus.get("emitted_at", ""))

        if stimulus_type in TURN_STIMULI and self._running():
            ticket = StimulusTicket(
                type=stimulus_type,
                payload=payload,
                emitted_at=stimulus.get("emitted_at", ""),
                priority=STIMULUS_PRIORITY.get(stimulus_type, 0),
            )
            if self.pending_ticket is None or ticket.priority >= self.pending_ticket.priority:
                self.pending_ticket = ticket

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
        reason = f"stimulus:{ticket.type}"
        try:
            self.app.run_turn(reason=reason)
            self.status.update(last_turn_at=self.app.state.last_turn_at, last_turn_reason=reason)
        except Exception as exc:
            self.app._log_event("turn", "error", "Agent turn failed.", {"reason": reason, "error": str(exc)})

    def note_session_started(self):
        self.last_turn_started = 0.0
        self.last_tab_poll = 0.0
        self.pending_ticket = StimulusTicket(type="manual", payload={}, emitted_at="", priority=100)
        self._tab_signature = None
        self._warmup_until = time.time() + 2.5
        self.status.update(focus_state="active", notes="Session started.")
        self.memory.append("session", "Monitoring session started.")

    def note_session_stopped(self):
        self.pending_ticket = None
        self.status.update(focus_state="unknown", notes="Session stopped.")
        self.memory.append("session", "Monitoring session stopped.")
