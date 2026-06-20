"""Verification workflow for the BigBrother agent framework (AGENT_SPEC.md).

Runs without API keys: the VLM endpoint and watcher model are stubbed.
Each check maps to a spec section and prints PASS/FAIL.
"""

import json
import os
import threading
import time
import urllib.request

os.environ.setdefault("BIG_BROTHER_API_KEY", "test-key-not-real")

import agent_core
from agent_core import (
    AgentMemory,
    StatusFile,
    StimulusBus,
    TodoList,
    TokenLedger,
    estimate_image_tokens,
)

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


# --- Spec 1: agent primitives -------------------------------------------------

def test_primitives(tmp_state):
    ledger = TokenLedger(path=tmp_state / "ledger.json")
    ledger.record_call("watcher", 500, 100)
    ledger.record_skip("watcher", 600)
    ledger.record_skip("vlm:screen", 1200)
    snap = ledger.snapshot()
    check("TokenLedger totals", snap["total_tokens_used"] == 600 and snap["total_estimated_tokens_saved"] == 1800)
    check("TokenLedger multiplier", snap["efficiency_multiplier"] == 4.0, str(snap["efficiency_multiplier"]))

    memory = AgentMemory(path=tmp_state / "memory.jsonl")
    memory.append("observation", "User opened a YouTube tab.")
    memory.append("stimulus", "Inactivity for 60 seconds.")
    hits = memory.recall("youtube tab")
    check("AgentMemory recall", hits and "YouTube" in hits[0]["text"])
    memory2 = AgentMemory(path=tmp_state / "memory.jsonl")
    check("AgentMemory persistence", len(memory2.recent(10)) == 2)

    status = StatusFile(path=tmp_state / "status.json")
    status.update(focus_state="active", notes="testing")
    status2 = StatusFile(path=tmp_state / "status.json")
    check("StatusFile round-trip", status2.get()["focus_state"] == "active")

    todos = TodoList(path=tmp_state / "todos.json")
    todos.add("Re-scan webcam", due_in_seconds=0.0)
    todos.add("Far future", due_in_seconds=9999)
    due = todos.pop_due()
    check("TodoList due alarm", len(due) == 1 and due[0]["note"] == "Re-scan webcam")
    check("TodoList keeps future items", len(todos.list_all()) == 1)

    bus = StimulusBus()
    first = bus.emit("tab_opened", {"count": 1})
    second = bus.emit("tab_opened", {"count": 1})  # inside 2 s debounce window
    third = bus.emit("heartbeat")
    check("StimulusBus debounce", first and not second and third)
    got = bus.get(timeout=0.2)
    check("StimulusBus queue", got and got["type"] == "tab_opened")

    check("Image token estimate (1280x720 ~ 1100)", 900 <= estimate_image_tokens(1280, 720) <= 1300)
    check("Image token downscale saving", estimate_image_tokens(640, 360) < estimate_image_tokens(1280, 720) / 3)


# --- Spec 3: VLM dedupe + watcher fingerprint gating --------------------------

FAKE_VLM_CALLS = {"count": 0}


class FakeVLMResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(req, timeout=0):
    FAKE_VLM_CALLS["count"] += 1
    return FakeVLMResponse({
        "choices": [{"message": {"content": f"Fake summary #{FAKE_VLM_CALLS['count']}: user at desk."}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 60},
    })


class StubWatcher:
    model = "stub-watcher"
    enabled = True

    def __init__(self):
        self.calls = 0

    def evaluate(self, goal, resources):
        self.calls += 1
        from actors import WatcherDecision
        return WatcherDecision(
            off_task=False,
            confidence=0.9,
            summary="Stub: on task.",
            relevant_evidence=[],
            actor_mode="stub",
        )


REAL_URLOPEN = urllib.request.urlopen


def test_app_gating():
    import app as app_module

    app_module.request.urlopen = fake_urlopen  # stub the upstream VLM
    app = app_module.WatcherDashboardApp(start_orchestrator=False)

    import base64 as b64

    def data_url(seed):
        return "data:image/jpeg;base64," + b64.b64encode((seed * 40).encode()).decode()

    tiny_png = data_url("frame-a")
    result1 = app.analyze_capture("webcam", "", tiny_png)
    result2 = app.analyze_capture("webcam", "", tiny_png)
    check("VLM dedupe: 1 upstream call for identical frames", FAKE_VLM_CALLS["count"] == 1, str(FAKE_VLM_CALLS["count"]))
    check("VLM dedupe: cached flag", result2.get("cached") is True and result1.get("cached") is None)
    ledger_snap = app.ledger.snapshot()
    vlm_entry = ledger_snap["components"].get("vlm:webcam", {})
    check("VLM dedupe: ledger call+skip", vlm_entry.get("calls") == 1 and vlm_entry.get("skipped_calls") == 1)

    other_frame = data_url("frame-b")
    app.analyze_capture("webcam", "", other_frame)
    check("VLM dedupe: changed frame calls upstream", FAKE_VLM_CALLS["count"] == 2)

    stub = StubWatcher()
    app.watcher = stub
    app.run_once(goal="verify gating", reason="test_run_1")
    first_calls = stub.calls
    app.run_once(reason="test_run_2")  # identical resources -> fingerprint gate
    check("Watcher fingerprint gate: second identical turn skipped", first_calls == 1 and stub.calls == 1, f"calls={stub.calls}")
    watcher_entry = app.ledger.snapshot()["components"].get("watcher", {})
    check("Watcher gate: ledger skip recorded", watcher_entry.get("skipped_calls", 0) >= 1)

    app.analyze_capture("webcam", "", data_url("frame-c"))
    app.run_once(reason="test_run_3")  # resources changed -> watcher runs again
    check("Watcher gate: changed resources re-evaluated", stub.calls == 2, f"calls={stub.calls}")

    # Spec 5: frame_unchanged stimulus refreshes resources with zero tokens
    before_skips = app.ledger.snapshot()["components"]["vlm:webcam"]["skipped_calls"]
    result = app.ingest_stimulus("frame_unchanged", {"mode": "webcam", "width": 640, "height": 360})
    after_skips = app.ledger.snapshot()["components"]["vlm:webcam"]["skipped_calls"]
    check("frame_unchanged: ledger skip + accepted", result["accepted"] and after_skips == before_skips + 1)

    try:
        app.ingest_stimulus("evil_type")
        check("Stimulus whitelist rejects unknown types", False)
    except ValueError:
        check("Stimulus whitelist rejects unknown types", True)

    return app


# --- Spec 2: orchestrator behavior --------------------------------------------

def drain_bus(app):
    while app.bus.get(timeout=0.05) is not None:
        pass


def test_orchestrator(app):
    turns = []
    app.run_turn = lambda reason="": turns.append(reason)
    with app.state.lock:
        app.state.running = True
        app.state.interval_seconds = 3

    orch = app.orchestrator
    orch.pending_turn_reason = ""
    drain_bus(app)
    orch.last_turn_started = time.time() - 100  # spacing satisfied
    app.bus.emit("tab_opened", {"count": 1})
    orch._tick()
    orch._tick()
    check("Orchestrator: tab stimulus triggers turn", turns == ["stimulus:tab_opened"], str(turns))

    # Coalescing: burst of stimuli within spacing -> at most one pending turn
    turns.clear()
    orch.last_turn_started = time.time()
    app.bus.emit("heartbeat")
    app.bus.emit("manual")
    orch._tick()
    orch._tick()
    check("Orchestrator: rate-limit coalesces burst", turns == [], str(turns))
    orch.last_turn_started = time.time() - 100
    orch._tick()
    check("Orchestrator: pending turn runs once spacing allows", len(turns) == 1, str(turns))

    # Todo alarm becomes a stimulus and a turn
    turns.clear()
    drain_bus(app)
    orch.pending_turn_reason = ""
    orch.last_turn_started = time.time() - 100
    app.todos.add("alarm now", due_in_seconds=0.0)
    orch._tick()
    orch._tick()
    check("Orchestrator: todo alarm drives a turn", turns == ["stimulus:todo_due"], str(turns))

    # Heartbeat fires after the idle window
    turns.clear()
    drain_bus(app)
    orch.pending_turn_reason = ""
    orch.last_turn_started = time.time() - (orch.heartbeat_seconds + 5)
    orch._tick()
    orch._tick()
    check("Orchestrator: heartbeat after idle window", "stimulus:heartbeat" in turns, str(turns))

    # Inactivity stimulus updates status file
    drain_bus(app)
    app.bus.emit("inactivity", {"idle_ms": 60000})
    orch._tick()
    check("Orchestrator: inactivity sets focus_state", app.status_file.get()["focus_state"] == "inactive")

    with app.state.lock:
        app.state.running = False


# --- Spec 5/6: HTTP surface ----------------------------------------------------

def test_http():
    import app as app_module

    app_module.request.urlopen = REAL_URLOPEN  # restore: gating test stubbed it
    server = app_module.ThreadingHTTPServer(("127.0.0.1", 8799), app_module.AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/api/state", timeout=5) as response:
            state = json.loads(response.read().decode("utf-8"))
        agent = state.get("agent", {})
        check(
            "/api/state exposes agent section",
            all(key in agent for key in ("status", "todos", "memory_recent", "token_ledger")),
        )
        check("/api/state ledger has multiplier", "efficiency_multiplier" in agent.get("token_ledger", {}))

        body = json.dumps({"type": "activity", "payload": {}}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8799/api/stimulus",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        check("/api/stimulus accepts client stimuli", result.get("ok") and result["stimulus"]["accepted"])
    finally:
        server.shutdown()
        server.server_close()


def main():
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_primitives(Path(tmp))
    app = test_app_gating()
    test_orchestrator(app)
    test_http()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed.")
    if failed:
        print("FAILED: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
