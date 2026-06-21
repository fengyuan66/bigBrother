"""Event-driven agent orchestrator replacing the fixed-tick pipeline loop.

The orchestrator owns the agent loop: it reacts to stimuli (browser tab
events it detects itself via CDP, client-reported inactivity/activity,
capture updates, todo alarms) and only then runs a deliberate turn. A
heartbeat stimulus keeps the system live when nothing happens, at a far
slower cadence than the old per-tick loop.
"""

import threading
import time

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
        self.pending_turn_reason = ""
        self._tab_signature = None

    def stop(self):
        self.stop_event.set()

    def _running(self) -> bool:
        with self.app.state.lock:
            return bool(self.app.state.running)

    def _min_turn_spacing(self) -> float:
        with self.app.state.lock:
            return float(max(3, self.app.state.interval_seconds))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.app._log_event("agent", "error", "Orchestrator loop error.", {
                    "error": str(exc),
                })
                time.sleep(1.0)

    def _tick(self):
        now = time.time()

        for item in self.todos.pop_due(now):
            self.bus.emit("todo_due", {"todo": item})

        if self._running() and now - self.last_tab_poll >= self.tab_poll_seconds:
            self.last_tab_poll = now
            self._poll_tabs()

        if (
            self._running()
            and self.last_turn_started
            and now - self.last_turn_started >= self.heartbeat_seconds
        ):
            self.bus.emit("heartbeat", {"idle_seconds": round(now - self.last_turn_started, 1)})

        stimulus = self.bus.get(timeout=1.0)
        if stimulus is not None:
            self._handle_stimulus(stimulus)

        self._run_pending_turn_if_allowed()

    def _poll_tabs(self):
        """Cheap local CDP poll; diff tab signatures into stimuli. No tokens."""
        try:
            tabs = self.app.tab_reader.read_tabs(retries=1, delay_seconds=0.1)
        except Exception:
            return
        signature = {}
        for tab in tabs:
            tab_id = tab.get("id") or tab.get("url", "")
            signature[tab_id] = tab.get("url", "")

        previous = self._tab_signature
        self._tab_signature = signature
        if previous is None:
            return

        opened = [tab for tab in tabs if (tab.get("id") or tab.get("url", "")) not in previous]
        closed = [tab_id for tab_id in previous if tab_id not in signature]
        navigated = [
            tab
            for tab in tabs
            if (tab.get("id") or tab.get("url", "")) in previous
            and signature[(tab.get("id") or tab.get("url", ""))] != previous[(tab.get("id") or tab.get("url", ""))]
        ]
        if opened:
            self.bus.emit("tab_opened", {
                "count": len(opened),
                "urls": [tab.get("url", "") for tab in opened][:5],
                "tabs": [
                    {"id": str(tab.get("id", "")), "url": tab.get("url", ""), "title": tab.get("title", "")}
                    for tab in opened[:5]
                ],
            })
        if closed:
            self.bus.emit("tab_closed", {"count": len(closed)})
        if navigated:
            self.bus.emit("tab_refreshed", {
                "count": len(navigated),
                "urls": [tab.get("url", "") for tab in navigated][:5],
                "tabs": [
                    {"id": str(tab.get("id", "")), "url": tab.get("url", ""), "title": tab.get("title", "")}
                    for tab in navigated[:5]
                ],
            })

    def _handle_stimulus(self, stimulus: dict):
        stimulus_type = stimulus.get("type", "")
        payload = stimulus.get("payload", {})

        self.memory.append("stimulus", f"Stimulus received: {stimulus_type}", meta=payload)
        self.status.update(
            last_stimulus=stimulus_type,
            last_stimulus_at=stimulus.get("emitted_at", ""),
            last_stimulus_payload=payload,
        )
        self.app._log_event("agent", "stimulus", f"Stimulus: {stimulus_type}", payload)

        if stimulus_type == "inactivity":
            self.status.update(focus_state="inactive", notes="No screen change for the inactivity window.")
        elif stimulus_type == "activity":
            self.status.update(focus_state="active", last_activity_at=stimulus.get("emitted_at", ""))

        if stimulus_type == "frame_unchanged":
            # Freshness ping only — the server refreshes cached resource
            # timestamps in the API handler; no turn is needed.
            return

        if stimulus_type in TURN_STIMULI and self._running():
            self.pending_turn_reason = f"stimulus:{stimulus_type}"

    def _run_pending_turn_if_allowed(self):
        if not self.pending_turn_reason or not self._running():
            return
        now = time.time()
        if now - self.last_turn_started < self._min_turn_spacing():
            return
        reason = self.pending_turn_reason
        self.pending_turn_reason = ""
        self.last_turn_started = now
        try:
            self.app.run_turn(reason=reason)
            self.status.update(last_turn_at=self.app.state.last_turn_at, last_turn_reason=reason)
        except Exception as exc:
            self.app._log_event("agent", "error", "Agent turn failed.", {
                "reason": reason,
                "error": str(exc),
            })

    def note_session_started(self):
        self.last_turn_started = time.time()
        self._tab_signature = None
        self.pending_turn_reason = "stimulus:manual"
        self.status.update(focus_state="active", notes="Session started.")
        self.memory.append("session", "Monitoring session started.")

    def note_session_stopped(self):
        self.pending_turn_reason = ""
        self.status.update(focus_state="unknown", notes="Session stopped.")
        self.memory.append("session", "Monitoring session stopped.")
