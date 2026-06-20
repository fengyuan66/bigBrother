# BigBrother Agent Framework Spec (v2)

## Problem with the v1 pipeline

At the default 3-second tick, the v1 system performed, unconditionally:

- 2 full-resolution VLM image calls per tick (webcam 1280x720 + screen at native
  resolution, JPEG quality 0.92) — roughly 40 VLM calls/minute, ~1,000+ image
  tokens each, even when the frames are pixel-identical.
- 1 Watcher LLM call per tick with the full resource text, even when nothing
  changed since the previous tick, and with no `max_tokens` cap on any actor.
- A fixed-interval loop: no stimuli, no scheduling, no memory, no agent state.

## Target architecture

Event-driven agent loop with cheap signals first, model calls last.

### 1. `agent_core.py` — agent primitives

- **TokenLedger** — per-component call/skip counters and token accounting
  (real `usage` from the API when present, estimates otherwise). Computes
  estimated tokens saved by skipped calls and an efficiency multiplier
  (`(used + saved) / used`). Persisted to `state/token_ledger.json`.
- **AgentMemory** — append-only timestamped JSONL (`state/memory.jsonl`) of
  user movements/stimuli/decisions, with keyword-overlap `recall(query)`.
- **StatusFile** — `state/status.json`: the agent's current model of the user
  (focus state, last activity, last stimulus, last intervention). Updated by
  the orchestrator each loop; readable by every actor.
- **TodoList** — `state/todos.json`: agent-settable alarms
  `{id, due_at, note, kind}`. Due items become `todo_due` stimuli.
- **StimulusBus** — thread-safe queue with per-type debounce windows (2 s for
  tab open/close/refresh mass-event buffering, per the design notes).

### 2. `orchestrator.py` — AgentOrchestrator (replaces the fixed-tick loop)

A daemon thread that:

- Blocks on the StimulusBus and reacts to: `tab_opened`, `tab_closed`,
  `tab_refreshed`, `capture_updated`, `inactivity`, `activity`, `todo_due`,
  `heartbeat`, `manual`.
- Polls the tracked browser's CDP tab list (cheap local HTTP, no tokens) every
  ~2 s while running, diffing tab signatures itself to *generate* the tab
  stimuli server-side.
- Emits a `heartbeat` stimulus if no turn has run for `heartbeat_seconds`
  (default 30 s) so the system stays live without per-tick model calls.
- Coalesces stimuli: turns are rate-limited to one per `min_turn_spacing`
  (default = interval setting); bursts collapse into one pending turn.
- Writes every stimulus and turn outcome to AgentMemory and StatusFile.

### 3. Model-call gating (the token savings)

- **VLM dedupe (server)** — `analyze_capture` hashes the incoming image; an
  identical hash per mode returns the cached summary, refreshes resource-file
  timestamps, and records a skip in the ledger. Zero tokens.
- **VLM change detection (client)** — each captured frame is downscaled to a
  64x36 grayscale signature and compared with the previous frame; below the
  motion threshold the frame is *not uploaded at all*. A free
  `/api/stimulus {type: "frame_unchanged"}` ping keeps server freshness.
- **Image downscaling (client)** — webcam frames capped at 640 px wide, screen
  at 1024 px, JPEG quality 0.7 (image tokens scale with area: ~3-4x fewer
  tokens per call that does happen). VLM `max_tokens` 500 → 300.
- **Adaptive webcam cadence (client)** — webcam scanned every ~30 s (per the
  design notes) instead of every tick, or immediately when motion is detected.
- **Watcher fingerprint gating (server)** — a turn whose (goal + frozen
  resource text) fingerprint matches the previous evaluated turn is skipped
  entirely: no new evidence means no Watcher call, streak state untouched.
- **Output caps & retries (server)** — all actor calls get `max_tokens`
  (watcher 220, MPA 280, personality 240), a 60 s timeout, retry with
  exponential backoff, and resource text truncation (4,000 chars default).

### 4. Inactivity / re-activity (client)

- No screen change for 60 s ⇒ `inactivity` stimulus (browser tab scan is the
  cheap response; the user may be watching a video).
- Re-activity is debounced: two consecutive *changed* ticks are required
  before an `activity` stimulus fires (robust against a single stray event).

### 5. New API surface

- `POST /api/stimulus` — `{type, payload}` from the client (inactivity,
  activity, frame_unchanged, manual).
- `/api/state` gains `agent`: status file, todos, recent memory, last
  stimulus, and the token ledger snapshot (calls, skips, tokens used/saved,
  efficiency multiplier).

### 6. UI

- New "Agent & Efficiency" panel: ledger numbers (VLM/watcher calls made vs
  skipped, estimated tokens saved, efficiency multiplier), agent status,
  pending todos, and recent memory tail.

## Expected efficiency (default settings, mostly-static study screen)

| Source of saving | Factor |
| --- | --- |
| Frame change detection + VLM dedupe (static screen/webcam) | ~5-20x fewer VLM calls |
| Webcam 3 s -> 30 s cadence | 10x fewer webcam calls |
| Downscale + quality drop on calls that do happen | ~3x fewer image tokens |
| Watcher fingerprint gating | skips watcher when nothing changed |
| Event-driven turns vs fixed tick | turns only on stimuli/heartbeat |

Combined, steady-state token usage drops well past the 5-10x goal while
*increasing* responsiveness to real changes (tab events trigger turns within
~2 s instead of waiting for the next tick).
