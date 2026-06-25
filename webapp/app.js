const goalInput = document.getElementById("goal");
const intervalInput = document.getElementById("intervalSeconds");
const thresholdInput = document.getElementById("threshold");
const capturePromptInput = document.getElementById("capturePrompt");
const browserNameInput = document.getElementById("browserName");
const browserUrlInput = document.getElementById("browserUrl");
const sessionDurationSlider = document.getElementById("sessionDurationSlider");

const watcherBadge = document.getElementById("watcherBadge");
const statusBadge = document.getElementById("statusBadge");
const captureBadge = document.getElementById("captureBadge");
const runningBadge = document.getElementById("runningBadge");
const turnBadge = document.getElementById("turnBadge");
const actorModeBadge = document.getElementById("actorModeBadge");
const thresholdBadge = document.getElementById("thresholdBadge");
const cooldownBadge = document.getElementById("cooldownBadge");
const mpaBadge = document.getElementById("mpaBadge");
const personalityBadge = document.getElementById("personalityBadge");
const guidedBadge = document.getElementById("guidedBadge");

const statusText = document.getElementById("statusText");
const captureStatusText = document.getElementById("captureStatusText");
const errorText = document.getElementById("errorText");
const streakValue = document.getElementById("streakValue");
const exportValue = document.getElementById("exportValue");
const watcherSummary = document.getElementById("watcherSummary");
const mpaSummary = document.getElementById("mpaSummary");
const personalitySummary = document.getElementById("personalitySummary");
const personalityNotes = document.getElementById("personalityNotes");
const guidedStatus = document.getElementById("guidedStatus");
const sessionDurationValue = document.getElementById("sessionDurationValue");
const sessionCountdown = document.getElementById("sessionCountdown");
const thoughtFeedBadge = document.getElementById("thoughtFeedBadge");
const thoughtFeed = document.getElementById("thoughtFeed");
const evidenceList = document.getElementById("evidenceList");
const pathsText = document.getElementById("paths");
const actorStageGrid = document.getElementById("actorStageGrid");
const debugLogPath = document.getElementById("debugLogPath");
const debugEventLog = document.getElementById("debugEventLog");
const turnSnapshotPanel = document.getElementById("turnSnapshotPanel");
const turnHistoryPanel = document.getElementById("turnHistoryPanel");

const efficiencyBadge = document.getElementById("efficiencyBadge");
const ledgerCalls = document.getElementById("ledgerCalls");
const ledgerSkips = document.getElementById("ledgerSkips");
const ledgerUsed = document.getElementById("ledgerUsed");
const ledgerSaved = document.getElementById("ledgerSaved");
const agentLedger = document.getElementById("agentLedger");
const agentStimulusBadge = document.getElementById("agentStimulusBadge");
const agentStatusText = document.getElementById("agentStatusText");
const agentTodos = document.getElementById("agentTodos");
const agentMemory = document.getElementById("agentMemory");

const webcamOutput = document.getElementById("webcamOutput");
const screenshareOutput = document.getElementById("screenshareOutput");
const browserOutput = document.getElementById("browserOutput");
const watcherOutput = document.getElementById("watcherOutput");
const mpaOutput = document.getElementById("mpaOutput");
const personalityOutput = document.getElementById("personalityOutput");
const personalityAudio = document.getElementById("personalityAudio");

const shareButton = document.getElementById("shareButton");
const webcamButton = document.getElementById("webcamButton");
const captureButton = document.getElementById("captureButton");
const autoCaptureButton = document.getElementById("autoCaptureButton");
const stopCaptureButton = document.getElementById("stopCaptureButton");
const runOnceButton = document.getElementById("runOnceButton");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const resetStatsButton = document.getElementById("resetStatsButton");
const exportTabsButton = document.getElementById("exportTabsButton");
const launchBrowserButton = document.getElementById("launchBrowserButton");
const guidedStartButton = document.getElementById("guidedStartButton");

const FRAME_DIFF_THRESHOLD = 5; // mean absolute grayscale delta (0-255)
const STRONG_MOTION_THRESHOLD = FRAME_DIFF_THRESHOLD * 3;
const WEBCAM_MIN_PERIOD_MS = 30000;
const INACTIVITY_AFTER_MS = 60000;
const REACTIVITY_CHANGED_TICKS = 2;
const MAX_UPLOAD_WIDTH = { webcam: 640, screen: 1024 };
const UPLOAD_JPEG_QUALITY = 0.7;

const captureSources = {
  webcam: {
    key: "webcam",
    label: "Webcam",
    analysisMode: "webcam",
    videoEl: document.getElementById("webcamVideo"),
    canvasEl: document.getElementById("webcamCanvas"),
    snapshotEl: document.getElementById("webcamSnapshot"),
    liveStatusEl: document.getElementById("webcamLiveStatus"),
    stream: null,
    lastSnipAt: "",
    prevSignature: null,
    lastSentAt: 0,
    sentCount: 0,
    skippedCount: 0,
  },
  screen: {
    key: "screen",
    label: "Screenshare",
    analysisMode: "screen",
    videoEl: document.getElementById("screenVideo"),
    canvasEl: document.getElementById("screenCanvas"),
    snapshotEl: document.getElementById("screenSnapshot"),
    liveStatusEl: document.getElementById("screenLiveStatus"),
    stream: null,
    lastSnipAt: "",
    prevSignature: null,
    lastSentAt: 0,
    sentCount: 0,
    skippedCount: 0,
  },
};

const signatureCanvas = document.createElement("canvas");
signatureCanvas.width = 64;
signatureCanvas.height = 36;

let inactivityReported = false;
let unchangedSinceMs = 0;
let consecutiveChangedTicks = 0;

let pollHandle = null;
let autoCaptureHandle = null;
let captureInFlight = false;
let lastSpokenPersonalityEventId = "";
let availableSpeechVoices = [];
let audioContext = null;
const lastActorStageVersions = {};
let latestState = null;
let speechInFlight = false;
let speechPauseUntilMs = 0;
const completedClientActionIds = new Set();

function refreshSpeechVoices() {
  if (!("speechSynthesis" in window)) {
    availableSpeechVoices = [];
    return;
  }
  availableSpeechVoices = window.speechSynthesis.getVoices();
}

function ensureAudioContext() {
  if (audioContext) {
    if (audioContext.state === "suspended") {
      audioContext.resume().catch(() => {});
    }
    return audioContext;
  }
  const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextCtor) {
    return null;
  }
  audioContext = new AudioContextCtor();
  return audioContext;
}

function playStatusCue(status) {
  const context = ensureAudioContext();
  if (!context) {
    return;
  }
  const frequencies = {
    reading: 420,
    processing: 560,
    writing: 740,
    idle: 320,
    cooldown: 500,
  };
  const frequency = frequencies[status] || 360;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  const now = context.currentTime;
  oscillator.type = status === "writing" ? "triangle" : "sine";
  oscillator.frequency.setValueAtTime(frequency, now);
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.04, now + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(now);
  oscillator.stop(now + 0.2);
}

refreshSpeechVoices();
if ("speechSynthesis" in window) {
  window.speechSynthesis.addEventListener("voiceschanged", refreshSpeechVoices);
}

function payloadFromControls() {
  return {
    goal: goalInput.value,
    interval_seconds: Number(intervalInput.value || 3),
    threshold: Number(thresholdInput.value || 1),
    duration_seconds: Number(sessionDurationSlider.value || 15) * 60,
    browser_name: browserNameInput.value,
    browser_url: browserUrlInput.value,
  };
}

function formatDurationLabel(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds || 0));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes > 0 && remainder > 0) {
    return `${minutes}m ${remainder}s`;
  }
  if (minutes > 0) {
    return `${minutes} min`;
  }
  return `${remainder}s`;
}

function syncSessionDurationLabel() {
  sessionDurationValue.textContent = `${Number(sessionDurationSlider.value || 15)} min`;
}

function speechPauseActive() {
  return speechInFlight || Date.now() < speechPauseUntilMs;
}

function clearSnipPreviews() {
  for (const source of Object.values(captureSources)) {
    source.snapshotEl.removeAttribute("src");
    source.lastSnipAt = "";
  }
}

function activeSourceKeys() {
  return Object.keys(captureSources).filter((key) => Boolean(captureSources[key].stream));
}

function formatSourceList(keys) {
  if (keys.length === 0) {
    return "No live sources";
  }
  return keys.map((key) => captureSources[key].label).join(" + ");
}

function syncCaptureBadges() {
  const activeKeys = activeSourceKeys();
  captureBadge.textContent = formatSourceList(activeKeys);

  const hasActiveSources = activeKeys.length > 0;
  captureButton.disabled = !hasActiveSources || captureInFlight;
  autoCaptureButton.disabled = !hasActiveSources || captureInFlight;
  stopCaptureButton.disabled = !hasActiveSources;

  shareButton.textContent = captureSources.screen.stream ? "Re-share screen" : "Share screen";
  webcamButton.textContent = captureSources.webcam.stream ? "Restart webcam" : "Use webcam";
}

function stopAutoCapture() {
  if (autoCaptureHandle) {
    window.clearInterval(autoCaptureHandle);
    autoCaptureHandle = null;
  }
  autoCaptureButton.textContent = "Start auto capture";
}

function stopSource(sourceKey, endedMessage = "") {
  const source = captureSources[sourceKey];
  if (source.stream) {
    source.stream.getTracks().forEach((track) => track.stop());
    source.stream = null;
  }
  source.videoEl.srcObject = null;
  if (endedMessage) {
    source.liveStatusEl.textContent = endedMessage;
  }
  if (activeSourceKeys().length === 0) {
    stopAutoCapture();
  }
  syncCaptureBadges();
}

async function startSource(sourceKey) {
  const source = captureSources[sourceKey];
  stopSource(sourceKey);

  if (sourceKey === "webcam") {
    source.stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "user",
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
  } else {
    source.stream = await navigator.mediaDevices.getDisplayMedia({
      video: {
        frameRate: { ideal: 8, max: 12 },
      },
      audio: false,
    });
  }

  const [track] = source.stream.getVideoTracks();
  track.addEventListener("ended", () => {
    stopSource(
      sourceKey,
      sourceKey === "webcam" ? "Webcam permission ended." : "Screen-share permission ended."
    );
  });

  source.videoEl.srcObject = source.stream;
  await source.videoEl.play();
  source.liveStatusEl.textContent = `${source.label} live feed connected.`;
  syncCaptureBadges();

  window.setTimeout(() => {
    summarizeSources("auto", [sourceKey]);
  }, 250);
}

function captureSourceFrame(sourceKey) {
  const source = captureSources[sourceKey];
  const nativeWidth = source.videoEl.videoWidth;
  const nativeHeight = source.videoEl.videoHeight;
  if (!nativeWidth || !nativeHeight) {
    throw new Error(`${source.label} stream is not ready yet.`);
  }

  // Downscale before upload: image tokens scale with area.
  const maxWidth = MAX_UPLOAD_WIDTH[sourceKey] || 1024;
  const scale = Math.min(1, maxWidth / nativeWidth);
  const width = Math.round(nativeWidth * scale);
  const height = Math.round(nativeHeight * scale);

  source.canvasEl.width = width;
  source.canvasEl.height = height;
  const context = source.canvasEl.getContext("2d");
  context.drawImage(source.videoEl, 0, 0, width, height);
  const imageDataUrl = source.canvasEl.toDataURL("image/jpeg", UPLOAD_JPEG_QUALITY);
  source.snapshotEl.src = imageDataUrl;
  source.lastSnipAt = new Date().toLocaleTimeString();
  return { imageDataUrl, width, height };
}

function computeFrameSignature(sourceKey) {
  const source = captureSources[sourceKey];
  if (!source.videoEl.videoWidth || !source.videoEl.videoHeight) {
    return null;
  }
  const context = signatureCanvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(source.videoEl, 0, 0, signatureCanvas.width, signatureCanvas.height);
  const pixels = context.getImageData(0, 0, signatureCanvas.width, signatureCanvas.height).data;
  const gray = new Uint8Array(pixels.length / 4);
  for (let i = 0; i < gray.length; i += 1) {
    const offset = i * 4;
    gray[i] = (pixels[offset] + pixels[offset + 1] + pixels[offset + 2]) / 3;
  }
  return gray;
}

function signatureDiff(a, b) {
  if (!a || !b || a.length !== b.length) {
    return Infinity;
  }
  let total = 0;
  for (let i = 0; i < a.length; i += 1) {
    total += Math.abs(a[i] - b[i]);
  }
  return total / a.length;
}

function postStimulus(type, payload = {}) {
  return postJson("/api/stimulus", { type, payload }).catch(() => {});
}

function updateActivityTracking(changed) {
  const now = Date.now();
  if (changed) {
    consecutiveChangedTicks += 1;
    unchangedSinceMs = 0;
    if (inactivityReported && consecutiveChangedTicks >= REACTIVITY_CHANGED_TICKS) {
      inactivityReported = false;
      postStimulus("activity");
    }
    return;
  }
  consecutiveChangedTicks = 0;
  if (!unchangedSinceMs) {
    unchangedSinceMs = now;
  }
  if (!inactivityReported && now - unchangedSinceMs >= INACTIVITY_AFTER_MS) {
    inactivityReported = true;
    postStimulus("inactivity", { idle_ms: now - unchangedSinceMs });
  }
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

async function completeClientAction(actionId, result = {}) {
  if (!actionId) {
    return;
  }
  completedClientActionIds.add(actionId);
  await postJson("/api/client-action-complete", {
    action_id: actionId,
    result,
  }).catch(() => {});
}

async function loadState() {
  const response = await fetch("/api/state");
  if (!response.ok) {
    throw new Error(`State fetch failed: ${response.status}`);
  }
  const state = await response.json();
  renderState(state);
}

function renderEvidence(items) {
  evidenceList.innerHTML = "";
  if (!items || items.length === 0) {
    const item = document.createElement("li");
    item.textContent = "No relevant agent evidence for the latest turn.";
    evidenceList.appendChild(item);
    return;
  }

  for (const entry of items) {
    const item = document.createElement("li");
    item.textContent = entry;
    evidenceList.appendChild(item);
  }
}

function renderThreshold(state) {
  const rawProgress =
    state.threshold_progress !== undefined && state.threshold_progress !== null
      ? state.threshold_progress
      : state.off_task_streak;
  const streak = Number(rawProgress || 0);
  const threshold = Number(state.threshold || 1);
  const remaining = Math.max(0, threshold - streak);
  thresholdBadge.textContent =
    remaining === 0
      ? `Threshold met: ${streak}/${threshold}`
      : `Decision queue: ${streak}/${threshold}`;
  thresholdBadge.className = `badge ${remaining === 0 ? "ready" : "subtle"}`;
}

function renderPlanner(planner) {
  if (planner.triggered && planner.should_intervene) {
    mpaBadge.textContent = "Agenda ready";
    mpaBadge.className = "badge ready";
    mpaSummary.textContent = planner.agenda || "Planner triggered.";
    return;
  }

  if (planner.triggered && !planner.should_intervene) {
    mpaBadge.textContent = "No intervention";
    mpaBadge.className = "badge subtle";
    mpaSummary.textContent = planner.rationale || "Planner reviewed the evidence and declined intervention.";
    return;
  }

  mpaBadge.textContent = "Waiting";
  mpaBadge.className = "badge subtle";
  mpaSummary.textContent = planner.rationale || "Planner is waiting for the next agent turn.";
}

function renderPersonality(personality) {
  if (personality.triggered && personality.should_speak) {
    personalityBadge.textContent = personality.audio_generated ? "Voice ready" : "Line ready";
    personalityBadge.className = `badge ${personality.audio_generated ? "ready" : "subtle"}`;
    personalitySummary.textContent = personality.spoken_text || "Personality actor produced a spoken line.";
    personalityNotes.textContent =
      personality.audio_error || personality.delivery_notes || "Final voice output prepared.";
  } else {
    personalityBadge.textContent = "Waiting";
    personalityBadge.className = "badge subtle";
    personalitySummary.textContent =
      personality.spoken_text || "Response actor is waiting for a response-worthy plan.";
    personalityNotes.textContent =
      personality.audio_error || personality.delivery_notes || "Voice delivery notes will appear here.";
  }

  if (personality.audio_url) {
    if (personalityAudio.getAttribute("src") !== personality.audio_url) {
      personalityAudio.src = personality.audio_url;
    }
    personalityAudio.hidden = false;
  } else {
    personalityAudio.pause();
    personalityAudio.removeAttribute("src");
    personalityAudio.load();
    personalityAudio.hidden = true;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function formatAgeFromUnix(unixSeconds, snapshotAtIso = "") {
  if (!unixSeconds) {
    return "No updates yet";
  }
  const nowMillis = snapshotAtIso ? Date.parse(snapshotAtIso) : Date.now();
  if (!Number.isFinite(nowMillis)) {
    return "Updated just now";
  }
  const seconds = Math.max(0, Math.round((nowMillis - unixSeconds * 1000) / 1000));
  return `Updated ${formatDurationLabel(seconds)} ago`;
}

function renderActorStages(actorStages, snapshotAtIso = "") {
  const stageEntries = Object.values(actorStages || {});
  if (stageEntries.length === 0) {
    actorStageGrid.innerHTML = `
      <article class="actor-stage-card stage-idle">
        <div class="actor-stage-head">
          <h3>No actors</h3>
          <span class="badge subtle">idle</span>
        </div>
        <p class="status-text">No actor stage data is available yet.</p>
      </article>
    `;
    return;
  }

  actorStageGrid.innerHTML = stageEntries
    .map((stage) => {
      const status = String(stage.status || "idle").toLowerCase();
      const badgeClass =
        status === "reading"
          ? "ready"
          : status === "processing"
            ? "running"
            : status === "writing"
              ? "warm"
              : status === "cooldown"
                ? "warm"
                : "subtle";
      const detail = escapeHtml(stage.detail || "Waiting for work.");
      const outputPreview = escapeHtml(stage.last_output_preview || "No output yet.");
      const model = escapeHtml(stage.model || "No model");
      const updatedLabel = escapeHtml(formatAgeFromUnix(Number(stage.updated_at_unix || 0), snapshotAtIso));
      const lastOutputAt = escapeHtml(stage.last_output_at || "No output yet");

      return `
        <article class="actor-stage-card stage-${status}">
          <div class="actor-stage-head">
            <h3>${escapeHtml(stage.label || stage.key || "Actor")}</h3>
            <span class="badge ${badgeClass}">${escapeHtml(status)}</span>
          </div>
          <p class="status-text">${detail}</p>
          <p class="actor-stage-meta"><strong>Model:</strong> ${model}</p>
          <p class="actor-stage-meta"><strong>Timer:</strong> ${updatedLabel}</p>
          <p class="actor-stage-meta"><strong>Last output:</strong> ${lastOutputAt}</p>
          <pre class="actor-stage-output">${outputPreview}</pre>
        </article>
      `;
    })
    .join("");
}

function formatDebugEvents(events) {
  if (!events || events.length === 0) {
    return "Debug events will appear here after a run.";
  }

  const latestEvents = [...events].slice(-40).reverse();
  return latestEvents
    .map((event) => {
      const header = `${event.timestamp || ""}  [${event.component || "?"}/${event.phase || "?"}]  ${event.message || ""}`;
      if (event.payload === undefined) {
        return header;
      }
      return `${header}\n${JSON.stringify(event.payload, null, 2)}`;
    })
    .join("\n\n");
}

function parseEventTime(value) {
  const millis = Date.parse(value || "");
  return Number.isFinite(millis) ? millis : null;
}

function formatRelativeTimestamp(timestamp, baseline) {
  const timeMs = parseEventTime(timestamp);
  if (timeMs === null || baseline === null) {
    return "--:--";
  }
  const seconds = Math.max(0, Math.floor((timeMs - baseline) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function summarizeThoughtEvent(event) {
  const component = String(event.component || "");
  const phase = String(event.phase || "");
  const payload = event.payload || {};

  if (component === "agent" && phase === "stimulus") {
    const stimulusType = String((payload && payload.type) || "").trim();
    const rawMessage = String(event.message || "").replace(/^Stimulus:\s*/i, "").trim();
    const label = stimulusType || rawMessage || "a new signal";
    return {
      line: `I noticed ${label.replaceAll("_", " ")}.`,
      meta: rawMessage && rawMessage !== label ? rawMessage : "",
      tone: "signal",
    };
  }

  if (component === "turn" && phase === "collecting") {
    return {
      line: "I started collecting a fresh deliberate snapshot of the world state.",
      meta: "",
      tone: "step",
    };
  }

  if (component === "turn" && phase === "snapshot_frozen") {
    const reason = String(payload.reason || "").replace(/^stimulus:/, "").replaceAll("_", " ").trim();
    return {
      line: "I froze the evidence for a deliberate turn so I could reason on a stable snapshot.",
      meta: reason ? `Trigger: ${reason}.` : "",
      tone: "step",
    };
  }

  if (component === "agent" && phase === "processing") {
    return {
      line: `I started evaluating the latest evidence with ${payload.model || "the agent model"}.`,
      meta: "",
      tone: "thinking",
    };
  }

  if (component === "agent" && phase === "writing") {
    const output = payload.output || {};
    const summary = trimPreview(output.summary || "I finished my assessment.", 180);
    const responseRequired = output.response_required ? "Response requested." : "No response requested.";
    return {
      line: `I finished my assessment: ${summary}`,
      meta: responseRequired,
      tone: "decision",
    };
  }

  if (component === "agent" && phase === "cached") {
    return {
      line: "I skipped a model call because the evidence had not meaningfully changed.",
      meta: "",
      tone: "efficiency",
    };
  }

  if (component === "planner" && phase === "writing") {
    const plannerOutput = payload.planner_output || {};
    const agenda = trimPreview(plannerOutput.agenda || "Planner updated the next action.", 180);
    const intervene = plannerOutput.should_intervene ? "Intervention is warranted." : "No intervention needed.";
    return {
      line: `I updated the plan: ${agenda}`,
      meta: intervene,
      tone: "decision",
    };
  }

  if (component === "personality" && phase === "writing") {
    const output = payload.output || {};
    const spoken = trimPreview(output.spoken_text || "I prepared a spoken response.", 200);
    return {
      line: `I prepared a spoken line for the user: "${spoken}"`,
      meta: output.should_speak ? "Speech is ready." : "Speech was withheld.",
      tone: "voice",
    };
  }

  if (component === "personality" && phase === "idle") {
    return {
      line: "I chose not to speak on this turn.",
      meta: trimPreview(event.message || "", 140),
      tone: "quiet",
    };
  }

  if (component === "audio" && phase === "processing") {
    return {
      line: "I started generating audio for the spoken response.",
      meta: "",
      tone: "voice",
    };
  }

  if (component === "audio" && phase === "writing") {
    const generated = Boolean(payload.audio_generated);
    return {
      line: generated
        ? "I delivered an audio message to the user."
        : "I tried to generate audio, but it failed.",
      meta: payload.audio_error ? trimPreview(payload.audio_error, 180) : "",
      tone: generated ? "voice" : "error",
    };
  }

  if (component === "browser" && phase === "launch_complete") {
    const url = trimPreview((payload && payload.url) || "", 120);
    return {
      line: "I launched the tracked browser for the session.",
      meta: url ? `URL: ${url}` : "",
      tone: "step",
    };
  }

  if (component === "capture" && phase === "analyze_complete") {
    const mode = payload.analysis_mode || "capture";
    return {
      line: `I updated the ${mode} summary after scanning a fresh frame.`,
      meta: "",
      tone: "step",
    };
  }

  if (component === "turn" && phase === "error") {
    return {
      line: "I hit an error while working through the turn.",
      meta: trimPreview(payload.error || event.message || "Unknown error.", 180),
      tone: "error",
    };
  }

  if (component === "turn" && phase === "complete") {
    return {
      line: "I finished the current turn.",
      meta: payload.focus_state ? `Focus state: ${payload.focus_state}.` : "",
      tone: "step",
    };
  }

  if (component === "session" && phase === "start") {
    const duration = Number(payload.duration_seconds || 0);
    return {
      line: "I started a focus session.",
      meta: duration ? `Session length: ${formatDurationLabel(duration)}.` : "",
      tone: "step",
    };
  }

  if (component === "session" && phase === "stop") {
    return {
      line: "I stopped the focus session.",
      meta: "",
      tone: "step",
    };
  }

  return null;
}

function renderThoughtFeed(events) {
  const filtered = (events || [])
    .map((event) => {
      const summary = summarizeThoughtEvent(event);
      if (!summary) {
        return null;
      }
      return { event, summary };
    })
    .filter(Boolean);

  if (filtered.length === 0) {
    thoughtFeedBadge.textContent = "Waiting for events";
    thoughtFeedBadge.className = "badge subtle";
    thoughtFeed.innerHTML = `
      <article class="thought-entry">
        <div class="thought-time">0:00</div>
        <div class="thought-body">
          <p class="thought-line">Agent thought updates will appear here after the session starts doing work.</p>
        </div>
      </article>
    `;
    return;
  }

  const visible = filtered.slice(-18);
  const baseline = parseEventTime(visible[0].event.timestamp);
  thoughtFeedBadge.textContent = `${visible.length} thought${visible.length === 1 ? "" : "s"} visible`;
  thoughtFeedBadge.className = "badge ready";
  thoughtFeed.innerHTML = visible
    .map(({ event, summary }) => `
      <article class="thought-entry">
        <div class="thought-time">${escapeHtml(formatRelativeTimestamp(event.timestamp, baseline))}</div>
        <div class="thought-body">
          <p class="thought-line">${escapeHtml(summary.line)}</p>
          ${summary.meta ? `<p class="thought-meta">${escapeHtml(summary.meta)}</p>` : ""}
        </div>
      </article>
    `)
    .join("");
}

function formatTurnReason(reason) {
  return String(reason || "unspecified")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function trimPreview(text, maxLength = 220) {
  const normalized = String(text || "").trim();
  if (!normalized) {
    return "No text captured.";
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1)}…`;
}

function formatSourceBadges(sourceNames = [], className = "subtle") {
  if (!sourceNames.length) {
    return `<span class="badge ${className}">None</span>`;
  }
  return sourceNames
    .map((name) => `<span class="badge ${className}">${escapeHtml(name)}</span>`)
    .join("");
}

function renderTurnSnapshot(snapshot) {
  if (!snapshot || !snapshot.turn_id) {
    turnSnapshotPanel.innerHTML =
      '<p class="muted-text">The latest deliberate turn snapshot will appear here after a cycle runs.</p>';
    return;
  }

  const resources = snapshot.resources || {};
  const browserExport = snapshot.browser_export || {};
  const lastAnalysis = snapshot.last_analysis || {};
  const availableSources = Array.isArray(snapshot.available_sources) ? snapshot.available_sources : [];
  const missingSources = Array.isArray(snapshot.missing_sources) ? snapshot.missing_sources : [];
  const createdAt = snapshot.created_at || "Unknown time";
  const analysisSources = Object.keys(lastAnalysis);

  turnSnapshotPanel.innerHTML = `
    <div class="turn-summary-card">
      <div class="turn-summary-head">
        <div>
          <h3>Turn ${escapeHtml(snapshot.turn_id)}</h3>
          <p class="turn-summary-meta">${escapeHtml(createdAt)} · ${escapeHtml(formatTurnReason(snapshot.reason))}</p>
        </div>
        <span class="badge running">Revision ${escapeHtml(snapshot.resource_revision ?? 0)}</span>
      </div>
      <p class="status-text">${escapeHtml(snapshot.goal || "No study goal provided.")}</p>
      <div class="turn-badge-row">
        ${formatSourceBadges(availableSources, "running")}
      </div>
      <div class="turn-badge-row">
        ${missingSources.length ? formatSourceBadges(missingSources, "alert") : '<span class="badge subtle">No missing sources</span>'}
      </div>
    </div>

    <div class="turn-resource-grid">
      <article class="turn-resource-card">
        <div class="resource-head">
          <h3>Frozen agent prompt</h3>
          <span class="badge subtle">Exact turn input</span>
        </div>
        <pre class="turn-pre">${escapeHtml(snapshot.prompt_text || "No prompt text was captured.")}</pre>
      </article>

      <article class="turn-resource-card">
        <div class="resource-head">
          <h3>Browser export snapshot</h3>
          <span class="badge subtle">${escapeHtml(browserExport.browser || "browser")}</span>
        </div>
        <p class="turn-card-meta">Tabs exported: ${escapeHtml(browserExport.count ?? 0)}</p>
        <p class="turn-card-meta">Exported at: ${escapeHtml(browserExport.exported_at || "Unknown")}</p>
        <pre class="turn-pre">${escapeHtml(resources.browser || "No browser resource text found.")}</pre>
      </article>

      <article class="turn-resource-card">
        <div class="resource-head">
          <h3>Webcam VLM snapshot</h3>
          <span class="badge subtle">Vision input</span>
        </div>
        <pre class="turn-pre">${escapeHtml(resources.webcam || "No webcam resource text found.")}</pre>
      </article>

      <article class="turn-resource-card">
        <div class="resource-head">
          <h3>Screenshare VLM snapshot</h3>
          <span class="badge subtle">Vision input</span>
        </div>
        <pre class="turn-pre">${escapeHtml(resources.screenshare || "No screenshare resource text found.")}</pre>
      </article>

      <article class="turn-resource-card">
        <div class="resource-head">
          <h3>Latest analysis metadata</h3>
          <span class="badge subtle">${escapeHtml(analysisSources.length)} source(s)</span>
        </div>
        <pre class="turn-pre">${escapeHtml(
          analysisSources.length ? JSON.stringify(lastAnalysis, null, 2) : "No analysis metadata recorded."
        )}</pre>
      </article>
    </div>
  `;
}

function renderTurnHistory(history) {
  if (!history || history.length === 0) {
    turnHistoryPanel.innerHTML =
      '<p class="muted-text">Recent turn history will appear here after a cycle runs.</p>';
    return;
  }

  const latestTurns = [...history].slice().reverse();
  turnHistoryPanel.innerHTML = latestTurns
    .map((turn) => {
      const availableSources = Array.isArray(turn.available_sources) ? turn.available_sources : [];
      const missingSources = Array.isArray(turn.missing_sources) ? turn.missing_sources : [];
      return `
        <article class="turn-history-card">
          <div class="turn-history-head">
            <div>
              <h3>Turn ${escapeHtml(turn.turn_id || "?")}</h3>
              <p class="turn-summary-meta">${escapeHtml(turn.created_at || "Unknown time")} · ${escapeHtml(
                formatTurnReason(turn.reason)
              )}</p>
            </div>
            <span class="badge subtle">Revision ${escapeHtml(turn.resource_revision ?? 0)}</span>
          </div>
          <p class="turn-card-meta"><strong>Goal:</strong> ${escapeHtml(trimPreview(turn.goal || "", 120))}</p>
          <p class="turn-card-meta"><strong>Available:</strong> ${escapeHtml(
            availableSources.length ? availableSources.join(", ") : "None"
          )}</p>
          <p class="turn-card-meta"><strong>Missing:</strong> ${escapeHtml(
            missingSources.length ? missingSources.join(", ") : "None"
          )}</p>
          <p class="turn-card-meta"><strong>Frozen prompt:</strong> ${escapeHtml(
            trimPreview(turn.prompt_text || "", 260)
          )}</p>
        </article>
      `;
    })
    .join("");
}

function playActorStageUpdates(actorStages) {
  for (const [actorKey, stage] of Object.entries(actorStages || {})) {
    const version = Number(stage.version || 0);
    if (!version) {
      continue;
    }
    const previousVersion = Number(lastActorStageVersions[actorKey] || 0);
    if (version !== previousVersion) {
      if (previousVersion !== 0) {
        playStatusCue(String(stage.status || "idle").toLowerCase());
      }
      lastActorStageVersions[actorKey] = version;
    }
  }
}

function speakPersonalityIfNeeded(personality, turnLabel = "") {
  if (!("speechSynthesis" in window)) {
    return;
  }
  if (!personality.triggered || !personality.should_speak || !personality.spoken_text) {
    return;
  }
  if (speechPauseActive()) {
    return;
  }

  const eventId = personality.event_id || `${turnLabel}::${personality.spoken_text}`;
  if (!eventId || eventId === lastSpokenPersonalityEventId) {
    return;
  }

  const utterance = new SpeechSynthesisUtterance(personality.spoken_text);
  const preferredVoice =
    availableSpeechVoices.find((voice) => voice.lang === "en-US") ||
    availableSpeechVoices.find((voice) => voice.lang && voice.lang.startsWith("en")) ||
    null;
  if (preferredVoice) {
    utterance.voice = preferredVoice;
  }
  utterance.pitch = 1.05;
  utterance.rate = 0.92;
  utterance.volume = 1;
  utterance.onstart = () => {
    speechInFlight = true;
  };
  utterance.onend = () => {
    speechInFlight = false;
    clearSnipPreviews();
    const pauseSeconds = Number((latestState && latestState.post_speech_pause_seconds) || 5);
    speechPauseUntilMs = Date.now() + pauseSeconds * 1000;
    postJson("/api/speech-finished", { pause_seconds: pauseSeconds }).catch(() => {});
  };
  utterance.onerror = () => {
    speechInFlight = false;
    const pauseSeconds = Number((latestState && latestState.post_speech_pause_seconds) || 5);
    speechPauseUntilMs = Date.now() + pauseSeconds * 1000;
  };

  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
  lastSpokenPersonalityEventId = eventId;
}

function renderAgent(agent) {
  if (!agent) {
    return;
  }
  const ledger = agent.token_ledger || {};
  ledgerCalls.textContent = String(ledger.total_calls || 0);
  ledgerSkips.textContent = String(ledger.total_skipped_calls || 0);
  ledgerUsed.textContent = String(ledger.total_tokens_used || 0);
  ledgerSaved.textContent = String(ledger.total_estimated_tokens_saved || 0);
  const multiplier = Number(ledger.efficiency_multiplier || 0);
  if (multiplier > 0) {
    efficiencyBadge.textContent = `~${multiplier}x efficiency`;
    efficiencyBadge.className = `badge ${multiplier >= 5 ? "ready" : "subtle"}`;
  } else {
    efficiencyBadge.textContent = "No calls yet";
    efficiencyBadge.className = "badge subtle";
  }
  agentLedger.textContent = JSON.stringify(ledger.components || {}, null, 2);

  const status = agent.status || {};
  agentStatusText.textContent = `Focus: ${status.focus_state || "unknown"} · Last turn: ${
    status.last_turn_reason || "none"
  } · ${status.notes || ""}`;
  const lastStimulus = agent.last_stimulus || {};
  agentStimulusBadge.textContent = lastStimulus.type
    ? `Last stimulus: ${lastStimulus.type}`
    : "No stimulus yet";

  const todos = agent.todos || [];
  agentTodos.textContent = todos.length
    ? todos.map((item) => `[${item.kind}] ${item.note}`).join("\n")
    : "No pending agent alarms.";
  const memoryEntries = agent.memory_recent || [];
  agentMemory.textContent = memoryEntries.length
    ? memoryEntries
        .map((entry) => `${entry.timestamp || ""}  [${entry.kind || "?"}]  ${entry.text || ""}`)
        .join("\n")
    : "Agent memory tail will appear here.";

  if (Array.isArray(agent.pending_actions) && agent.pending_actions.length) {
    agentTodos.textContent += `\n\nPending client actions:\n${agent.pending_actions
      .map((item) => `[${item.type}] ${((item.payload && item.payload.reason) || "No reason provided.")}`)
      .join("\n")}`;
  }
}

async function processPendingAgentActions(actions) {
  if (!Array.isArray(actions) || actions.length === 0 || captureInFlight) {
    return;
  }

  for (const action of actions) {
    if (!action || !action.id || completedClientActionIds.has(action.id)) {
      continue;
    }
    if (action.type === "browser_rag") {
      await completeClientAction(action.id, {
        ok: true,
        status: "server_side",
        message: "Browser context is handled by the server tab export.",
      });
      continue;
    }
    if (action.type === "screen_scan") {
      await summarizeSources("agent_action", ["screen"], action);
      continue;
    }
    if (action.type === "webcam_scan") {
      await summarizeSources("agent_action", ["webcam"], action);
      continue;
    }
    await completeClientAction(action.id, {
      ok: false,
      status: "unsupported",
      message: `Unsupported client action type: ${action.type}`,
    });
  }
}

function hasPendingActionType(actionType) {
  const pending = (((latestState || {}).agent || {}).pending_actions || []);
  return pending.some((action) => action && action.type === actionType && action.id && !completedClientActionIds.has(action.id));
}

function shouldAutoAnalyzeSource(sourceKey, changed, diff, now) {
  if (sourceKey === "webcam") {
    const source = captureSources[sourceKey];
    const webcamDue = now - source.lastSentAt >= WEBCAM_MIN_PERIOD_MS;
    return !source.lastSentAt || diff >= STRONG_MOTION_THRESHOLD || (changed && webcamDue);
  }

  if (sourceKey === "screen") {
    if (hasPendingActionType("screen_scan")) {
      return changed;
    }
    return false;
  }

  return changed;
}

function renderState(state) {
  latestState = state;
  goalInput.value = state.goal;
  intervalInput.value = state.interval_seconds;
  thresholdInput.value = state.threshold;
  browserUrlInput.value = state.browser_url || "";

  const availableBrowsers = state.available_browsers || [];
  if (browserNameInput.options.length !== availableBrowsers.length) {
    browserNameInput.innerHTML = "";
    for (const browser of availableBrowsers) {
      const option = document.createElement("option");
      option.value = browser;
      option.textContent = browser;
      browserNameInput.appendChild(option);
    }
  }
  browserNameInput.value = state.browser_name || availableBrowsers[0] || "";

  watcherBadge.textContent = state.agent_enabled
      ? `Agent online · ${state.agent_model}`
      : "Agent fallback mode";
  const agentOutput = state.agent_output || state.watcher_output || {};
  actorModeBadge.textContent = agentOutput.actor_mode || "unknown";

  runningBadge.textContent = state.running ? "Agent running" : "Agent stopped";
  runningBadge.className = `badge ${state.running ? "running" : "subtle"}`;
  guidedBadge.textContent = state.running ? "Session active" : "Ready to launch";
  guidedBadge.className = `badge ${state.running ? "running" : "subtle"}`;

  const cooldownRemaining = Number(state.cooldown_remaining_seconds || 0);
  statusBadge.textContent =
    cooldownRemaining > 0
      ? "Response cooldown active"
      : agentOutput.off_task
        ? "Agent says off task"
        : "Agent says on task";
  statusBadge.className = `badge ${
    cooldownRemaining > 0 ? "warm" : agentOutput.off_task ? "alert" : "idle"
  }`;
  cooldownBadge.textContent =
    cooldownRemaining > 0
      ? `Cooldown: ${formatDurationLabel(cooldownRemaining)}`
      : "Cooldown idle";
  cooldownBadge.className = `badge ${cooldownRemaining > 0 ? "warm" : "subtle"}`;

  turnBadge.textContent = state.last_turn_at ? `Last turn: ${state.last_turn_at}` : "No turns yet";
  statusText.textContent = state.status || "Ready.";
  captureStatusText.textContent = `${state.capture_status} Vision model: ${state.vision_model}`;
  guidedStatus.textContent = state.running
    ? state.cycle_status || "Big Brother is live. Waiting for the next deliberate cycle."
    : "Choose a session length, then launch Big Brother.";
  if (Number(state.speech_grace_remaining_seconds || 0) > 0) {
    guidedStatus.textContent = `Voice cooldown active. Reassessment resumes in ${formatDurationLabel(state.speech_grace_remaining_seconds)}.`;
  }
  errorText.textContent = state.last_error || "";
  streakValue.textContent = String(state.off_task_streak);
  exportValue.textContent = String(state.last_export.count || 0);
  const remainingSeconds = Number(state.session_remaining_seconds || 0);
  sessionCountdown.textContent = state.running
    ? `Time left: ${formatDurationLabel(remainingSeconds)}`
    : "Countdown idle";
  sessionCountdown.className = `badge ${state.running ? "running" : "subtle"}`;
  if (state.session_duration_seconds) {
    sessionDurationSlider.value = String(Math.max(1, Math.round(state.session_duration_seconds / 60)));
  }
  syncSessionDurationLabel();
  if (!state.running && state.status === "Timed session complete.") {
    stopAutoCapture();
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    guidedStatus.textContent = "Session timer finished. Big Brother is no longer active.";
  }

  renderThreshold(state);
  watcherSummary.textContent = agentOutput.summary || "No agent output yet.";
  renderEvidence(agentOutput.evidence || agentOutput.relevant_evidence || []);

  const mpa = state.planner_output || state.mpa_output || {};
  renderPlanner(mpa);
  const personality = state.personality_output || {};
  renderPersonality(personality);
  speakPersonalityIfNeeded(personality, state.last_turn_at || "");
  renderAgent(state.agent || null);
  processPendingAgentActions((state.agent && state.agent.pending_actions) || []).catch((error) => {
    errorText.textContent = error.message || "Agent action failed.";
  });
  renderActorStages(state.actor_stages || {}, state.snapshot_at || "");
  playActorStageUpdates(state.actor_stages || {});
  renderThoughtFeed(state.debug_events || []);
  debugLogPath.textContent = `Debug log file: ${state.debug_log_path || "state/debug_events.jsonl"} | Live events in memory: ${
    (state.debug_events || []).length
  }`;
  debugEventLog.textContent = formatDebugEvents(state.debug_events || []);
  renderTurnSnapshot(state.last_turn_snapshot || {});
  renderTurnHistory(state.turn_history || []);

  const paths = state.paths || {};
  pathsText.textContent = `Files in use: webcam ${paths.webcam || ""} | screenshare ${paths.screenshare || ""} | browser ${paths.browser || ""}`;

  webcamOutput.textContent = state.resources.webcam;
  screenshareOutput.textContent = state.resources.screenshare;
  browserOutput.textContent = state.resources.browser;
  watcherOutput.textContent = JSON.stringify(agentOutput, null, 2);
  mpaOutput.textContent = JSON.stringify(mpa, null, 2);
  personalityOutput.textContent = JSON.stringify(personality, null, 2);

  syncCaptureBadges();
}

async function summarizeSources(reason = "manual", requestedKeys = null, actionRequest = null) {
  const sourceKeys = (requestedKeys || activeSourceKeys()).filter((key) => Boolean(captureSources[key].stream));
  if (sourceKeys.length === 0 || captureInFlight) {
    if (actionRequest && actionRequest.id) {
      await completeClientAction(actionRequest.id, {
        ok: false,
        status: "unavailable",
        reason: "Requested capture source is not active.",
      });
    }
    return;
  }
  if (reason === "auto" && speechPauseActive()) {
    captureStatusText.textContent = "Voice intervention pause active. Reassessment resumes in a few seconds.";
    return;
  }

  captureInFlight = true;
  syncCaptureBadges();
  errorText.textContent = "";
  let analyzedCount = 0;
  let skippedCount = 0;

  try {
    await Promise.all(sourceKeys.map(async (sourceKey) => {
      const source = captureSources[sourceKey];
      const now = Date.now();

      const signature = computeFrameSignature(sourceKey);
      const diff = source.prevSignature ? signatureDiff(signature, source.prevSignature) : Infinity;
      if (signature) {
        source.prevSignature = signature;
      }
      const changed = diff >= FRAME_DIFF_THRESHOLD;

      if (sourceKey === "screen" && reason === "auto") {
        updateActivityTracking(changed);
      }

      // Send policy:
      // - manual / targeted agent actions always go through
      // - browser/tab changes are handled browser-first on the server
      // - screen VLM only runs when the agent explicitly requested it
      // - webcam keeps its slower cadence unless there is strong motion
      let shouldSend = true;
      if (reason === "auto") {
        shouldSend = shouldAutoAnalyzeSource(sourceKey, changed, diff, now);
      }

      if (!shouldSend) {
        skippedCount += 1;
        source.skippedCount += 1;
        source.liveStatusEl.textContent = `${source.label} unchanged — VLM skipped (${source.skippedCount} saved).`;
        postStimulus("frame_unchanged", {
          mode: source.analysisMode,
          width: source.videoEl.videoWidth,
          height: source.videoEl.videoHeight,
        });
        return;
      }

      source.liveStatusEl.textContent =
        reason === "auto"
          ? `${source.label} changed — analyzing fresh frame...`
          : `${source.label} capturing a fresh frame...`;

      const frame = captureSourceFrame(sourceKey);
      await postJson("/api/analyze", {
        analysisMode: source.analysisMode,
        prompt: capturePromptInput.value.trim(),
        imageDataUrl: frame.imageDataUrl,
        metadata: actionRequest
          ? {
              action_id: actionRequest.id,
              action_type: actionRequest.type,
              tab_id: actionRequest.payload && actionRequest.payload.tab_id,
              tab_url: actionRequest.payload && actionRequest.payload.tab_url,
              reason,
            }
          : {},
      });
      source.lastSentAt = Date.now();
      source.sentCount += 1;
      analyzedCount += 1;
      source.liveStatusEl.textContent = `${source.label} updated at ${source.lastSnipAt}.`;
    }));

    // Auto turns are stimulus-driven by the server orchestrator; only manual
    // captures force a turn from the client.
    if (reason === "manual" && latestState && latestState.running && analyzedCount > 0) {
      await postJson("/api/run-once", {
        ...payloadFromControls(),
        reason: "manual_capture_cycle",
      });
    }

    await loadState();
    if (actionRequest && actionRequest.id) {
      await completeClientAction(actionRequest.id, {
        ok: true,
        status: "captured",
        analyzed_count: analyzedCount,
        skipped_count: skippedCount,
      });
    }
    captureStatusText.textContent =
      analyzedCount > 0
        ? `Analyzed ${analyzedCount} source(s), skipped ${skippedCount} unchanged on this tick.`
        : `No visual change — skipped ${skippedCount} VLM call(s) on this tick.`;
  } catch (error) {
    errorText.textContent = error.message || "Capture failed.";
    if (actionRequest && actionRequest.id) {
      await completeClientAction(actionRequest.id, {
        ok: false,
        status: "error",
        message: error.message || "Capture failed.",
      });
    }
    stopAutoCapture();
  } finally {
    captureInFlight = false;
    syncCaptureBadges();
  }
}

async function runOnce() {
  runOnceButton.disabled = true;
  try {
    await postJson("/api/run-once", {
      ...payloadFromControls(),
      reason: "manual_run",
    });
    await loadState();
  } finally {
    runOnceButton.disabled = false;
  }
}

async function startSession() {
  startButton.disabled = true;
  try {
    await postJson("/api/start", payloadFromControls());
    await loadState();
  } finally {
    startButton.disabled = false;
  }
}

async function startGuidedSession() {
  guidedStartButton.disabled = true;
  errorText.textContent = "";
  guidedStatus.textContent = "Resetting state and preparing the session...";
  try {
    await resetStats();
    guidedStatus.textContent = "Launching tracked browser...";
    await postJson("/api/launch-browser", {
      browser_name: browserNameInput.value,
      browser_url: browserUrlInput.value,
    });

    const setupErrors = [];

    guidedStatus.textContent = "Requesting webcam access...";
    try {
      await startSource("webcam");
    } catch (error) {
      setupErrors.push(`Webcam: ${error.message || "permission denied"}`);
    }

    guidedStatus.textContent = "Requesting screenshare access...";
    try {
      await startSource("screen");
    } catch (error) {
      setupErrors.push(`Screenshare: ${error.message || "permission denied"}`);
    }

    guidedStatus.textContent = "Starting agent loop...";
    await postJson("/api/start", payloadFromControls());

    if (activeSourceKeys().length > 0) {
      await summarizeSources("auto");
      if (!autoCaptureHandle) {
        autoCaptureHandle = window.setInterval(
          () => summarizeSources("auto"),
          Number(intervalInput.value || 3) * 1000
        );
        autoCaptureButton.textContent = "Pause auto capture";
      }
    }

    await loadState();
    guidedStatus.textContent = setupErrors.length
      ? `Started with partial setup. ${setupErrors.join(" | ")}`
      : "Big Brother launched successfully.";
  } catch (error) {
    errorText.textContent = error.message || "Unable to launch the guided session.";
    guidedStatus.textContent = "Guided launch hit a problem. You can still use the manual controls below.";
  } finally {
    guidedStartButton.disabled = false;
  }
}

async function stopSession() {
  stopButton.disabled = true;
  try {
    await postJson("/api/stop");
    await loadState();
  } finally {
    stopButton.disabled = false;
  }
}

async function resetStats() {
  resetStatsButton.disabled = true;
  try {
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    lastSpokenPersonalityEventId = "";
    clearSnipPreviews();
    personalityAudio.pause();
    personalityAudio.removeAttribute("src");
    personalityAudio.load();
    personalityAudio.hidden = true;
    await postJson("/api/reset-stats");
    await loadState();
  } finally {
    resetStatsButton.disabled = false;
  }
}

async function exportTabs() {
  exportTabsButton.disabled = true;
  try {
    await postJson("/api/export-tabs", {
      browser_name: browserNameInput.value,
      browser_url: browserUrlInput.value,
    });
    await loadState();
  } finally {
    exportTabsButton.disabled = false;
  }
}

async function launchBrowser() {
  launchBrowserButton.disabled = true;
  try {
    await postJson("/api/launch-browser", {
      browser_name: browserNameInput.value,
      browser_url: browserUrlInput.value,
    });
    await loadState();
  } finally {
    launchBrowserButton.disabled = false;
  }
}

function startPolling() {
  if (pollHandle) {
    clearInterval(pollHandle);
  }
  pollHandle = setInterval(() => {
    loadState().catch((error) => {
      errorText.textContent = error.message;
    });
  }, 1000);
}

shareButton.addEventListener("click", async () => {
  try {
    captureSources.screen.liveStatusEl.textContent = "Waiting for screen-share permission...";
    await startSource("screen");
  } catch (error) {
    errorText.textContent = error.message || "Unable to start screen share.";
  }
});

webcamButton.addEventListener("click", async () => {
  try {
    captureSources.webcam.liveStatusEl.textContent = "Waiting for webcam permission...";
    await startSource("webcam");
  } catch (error) {
    errorText.textContent = error.message || "Unable to start webcam.";
  }
});

captureButton.addEventListener("click", () => summarizeSources("manual"));

autoCaptureButton.addEventListener("click", async () => {
  if (autoCaptureHandle) {
    stopAutoCapture();
    captureStatusText.textContent = "Auto capture stopped.";
    return;
  }

  const seconds = Number(intervalInput.value || 3);
  if (!Number.isFinite(seconds) || seconds < 3) {
    errorText.textContent = "Choose an interval of at least 3 seconds.";
    return;
  }

  await summarizeSources("auto");
  autoCaptureHandle = window.setInterval(() => summarizeSources("auto"), seconds * 1000);
  autoCaptureButton.textContent = "Pause auto capture";
});

stopCaptureButton.addEventListener("click", () => {
  stopAutoCapture();
  stopSource("webcam", "Webcam stopped.");
  stopSource("screen", "Screenshare stopped.");
  captureStatusText.textContent = "All capture sources stopped.";
});

runOnceButton.addEventListener("click", runOnce);
startButton.addEventListener("click", startSession);
stopButton.addEventListener("click", stopSession);
resetStatsButton.addEventListener("click", resetStats);
exportTabsButton.addEventListener("click", exportTabs);
launchBrowserButton.addEventListener("click", launchBrowser);
guidedStartButton.addEventListener("click", startGuidedSession);
sessionDurationSlider.addEventListener("input", syncSessionDurationLabel);
document.addEventListener(
  "pointerdown",
  () => {
    ensureAudioContext();
  },
  { once: true }
);

captureSources.webcam.liveStatusEl.textContent = "Webcam not connected.";
captureSources.screen.liveStatusEl.textContent = "Screenshare not connected.";
syncCaptureBadges();
syncSessionDurationLabel();
loadState().catch((error) => {
  errorText.textContent = error.message;
});
startPolling();
