# BigBrother Agent Framework — Implementation Report

Date: 2026-06-10 · Spec: `AGENT_SPEC.md` · Verification: `verify_framework.py` (29/29 checks pass)

## What changed, at a glance

The v1 system was a fixed-cadence pipeline: every 3 s the browser uploaded
two full-resolution frames to the VLM and ran the Watcher LLM, unconditionally.
It is now an event-driven agent: cheap signals (frame diffs, CDP tab polls,
inactivity timers, todo alarms) decide *whether* a model is called at all, and
every call is capped, cached, retried, and metered.

## Implemented per spec

### 1. Agent primitives — `agent_core.py` (new)
- **TokenLedger** (`state/token_ledger.json`): per-component calls/skips, real
  API `usage` when present (estimates otherwise), estimated tokens saved, and
  a live efficiency multiplier `(used + saved) / used`.
- **AgentMemory** (`state/memory.jsonl`): append-only timestamped log of
  stimuli/observations/sessions with keyword-overlap `recall()`.
- **StatusFile** (`state/status.json`): the agent's current model of the user
  (focus state, last stimulus/turn/intervention), updated each loop.
- **TodoList** (`state/todos.json`): agent-settable alarms; due items become
  `todo_due` stimuli.
- **StimulusBus**: thread-safe queue with per-type debounce (2 s for tab
  open/close/refresh mass-event buffering, per the design notes).

### 2. Orchestrator — `orchestrator.py` (new)
Daemon thread replacing per-tick evaluation. Polls the tracked browser's CDP
tab list every 2 s (free, local) and *generates* `tab_opened` / `tab_closed` /
`tab_refreshed` stimuli itself; reacts to client stimuli (inactivity,
activity, manual); fires todo alarms; emits a `heartbeat` only after 30 s
without a turn; rate-limits turns to one per interval and coalesces stimulus
bursts into a single pending turn. Every stimulus and turn lands in memory
and the status file.

### 3. Model-call gating
- **Server VLM dedupe** (`app.py analyze_capture`): SHA-1 of the incoming
  frame; identical frame → cached summary returned, resource files
  re-timestamped, ledger skip recorded, zero tokens.
- **Client frame change detection** (`webapp/app.js`): 64×36 grayscale
  signature per source; below the motion threshold the frame is never
  uploaded — a free `frame_unchanged` stimulus keeps server freshness instead.
- **Client downscaling**: webcam capped at 640 px, screen at 1024 px, JPEG
  quality 0.92 → 0.7. VLM `max_tokens` 500 → 300.
- **Webcam cadence**: ~30 s between webcam VLM calls (was every 3 s tick)
  unless strong motion is detected.
- **Watcher fingerprint gate** (`app.py`): identical (goal + frozen resource
  text) → the whole turn is skipped with no Watcher call and untouched streak
  state. Volatile timestamp lines are stripped before hashing.
- **Actor hardening** (`actors.py`): shared `chat_json()` with `max_tokens`
  (watcher 220 / MPA 280 / personality 240), 60 s timeout, 2 retries with
  exponential backoff, 4,000-char resource truncation, ledger accounting.

### 4. Inactivity / re-activity (client)
60 s without screen change → `inactivity` stimulus; re-activity requires two
consecutive changed ticks (robust against a single stray event), then fires
`activity`.

### 5. API surface
- `POST /api/stimulus` (whitelisted types: inactivity, activity,
  frame_unchanged, manual).
- `/api/state` now carries an `agent` section: status file, todos, recent
  memory, last stimulus, heartbeat setting, and the full token ledger.

### 6. UI
New "Agent & Efficiency" panel: calls made vs skipped, tokens used vs saved,
live efficiency multiplier badge, agent status line, pending alarms, and the
memory tail. Per-source capture status now reports skipped-VLM counts.

## Efficiency math (defaults, mostly-static study screen)

| Lever | v1 | v2 |
| --- | --- | --- |
| Webcam VLM calls | 20/min | ≤2/min (30 s cadence, diff-gated) |
| Screen VLM calls | 20/min | only on visual change |
| Image tokens per call | ~1,170 (1280×720) | ~390 (downscaled) |
| Watcher LLM calls | 20/min | only on changed evidence / stimuli |
| Actor output caps | none | 220–300 max_tokens |

Steady-state on a static screen: ~40 VLM + 20 LLM calls/min → ~0–2 calls/min,
comfortably past the 5–10x target, while tab events now trigger turns within
~2 s (faster than the old tick in the worst case). The ledger reports the
realized multiplier live in the UI.

## Deviations from the spec

1. **Watcher gate skips the turn instead of reusing the cached decision.**
   Re-emitting a cached off-task decision would re-escalate toward the MPA
   without fresh evidence; skipping keeps the consecutive-positives contract
   intact. (Stricter than spec'd, intentional.)
2. **Fingerprint normalization added** (not in the original spec): the browser
   export embeds `Created:` timestamps, which defeated the gate — found by the
   verification workflow. Volatile lines are stripped before hashing.
3. **Webcam cadence is client-side**, not an orchestrator directive: the
   browser owns the media streams, so the server cannot capture frames. The
   spec's "every 30 s camera scan" stimulus lives in the capture loop instead.
4. **MPA→VLM targeted queries** (from `explanation.txt`) are scaffolded but
   not closed-loop: the capture-guidance prompt field feeds the VLM prompt,
   and the stimulus/todo plumbing exists, but the MPA does not yet write VLM
   queries itself. Noted as the natural next step.
5. `_handle` → `_handle_stimulus` rename: the spec'd name collided with
   `threading.Thread._handle` on Python 3.13 (caught by verification).

## Verification summary (`python verify_framework.py`)

29/29 checks across five suites, all passing:
- primitives (ledger math, memory recall/persistence, status round-trip,
  todo alarms, bus debounce, image-token estimates),
- VLM dedupe (single upstream call for identical frames, cached flag, ledger
  call+skip, changed-frame re-call),
- watcher gating (identical turn skipped, ledger skip, changed resources
  re-evaluated, `frame_unchanged` freshness refresh, stimulus whitelist),
- orchestrator (stimulus→turn, burst coalescing, spacing, todo alarms,
  heartbeat, inactivity → status file),
- HTTP surface (`/api/state` agent section + multiplier, `/api/stimulus`).

A live boot smoke test confirmed the dashboard serves and `/api/state`
exposes the agent section with the orchestrator running.
