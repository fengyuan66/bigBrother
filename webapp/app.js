const goalInput = document.getElementById("goal");
const intervalInput = document.getElementById("intervalSeconds");
const sessionDurationSlider = document.getElementById("sessionDurationSlider");
const sessionDurationValue = document.getElementById("sessionDurationValue");
const sessionCountdown = document.getElementById("sessionCountdown");
const browserNameInput = document.getElementById("browserName");
const browserUrlInput = document.getElementById("browserUrl");
const capturePromptInput = document.getElementById("capturePrompt");

const runningBadge = document.getElementById("runningBadge");
const stimulusBadge = document.getElementById("stimulusBadge");
const agentModeBadge = document.getElementById("agentModeBadge");
const actionBadge = document.getElementById("actionBadge");
const responseBadge = document.getElementById("responseBadge");
const captureBadge = document.getElementById("captureBadge");
const efficiencyBadge = document.getElementById("efficiencyBadge");

const statusText = document.getElementById("statusText");
const errorText = document.getElementById("errorText");
const agentSummary = document.getElementById("agentSummary");
const actionSummary = document.getElementById("actionSummary");
const responseSummary = document.getElementById("responseSummary");
const responseNotes = document.getElementById("responseNotes");
const captureStatusText = document.getElementById("captureStatusText");
const agentStatusText = document.getElementById("agentStatusText");
const pathsText = document.getElementById("paths");
const debugLogPath = document.getElementById("debugLogPath");

const evidenceList = document.getElementById("evidenceList");
const actionList = document.getElementById("actionList");
const agentTodos = document.getElementById("agentTodos");
const agentMemory = document.getElementById("agentMemory");
const debugEventLog = document.getElementById("debugEventLog");
const agentJson = document.getElementById("agentJson");
const responseJson = document.getElementById("responseJson");

const browserOutput = document.getElementById("browserOutput");
const webcamOutput = document.getElementById("webcamOutput");
const screenshareOutput = document.getElementById("screenshareOutput");
const personalityAudio = document.getElementById("personalityAudio");

const shareButton = document.getElementById("shareButton");
const webcamButton = document.getElementById("webcamButton");
const captureButton = document.getElementById("captureButton");
const autoCaptureButton = document.getElementById("autoCaptureButton");
const stopCaptureButton = document.getElementById("stopCaptureButton");
const launchBrowserButton = document.getElementById("launchBrowserButton");
const exportTabsButton = document.getElementById("exportTabsButton");
const freshStartButton = document.getElementById("freshStartButton");
const runOnceButton = document.getElementById("runOnceButton");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const resetStatsButton = document.getElementById("resetStatsButton");

const FRAME_DIFF_THRESHOLD = 5;
const STRONG_MOTION_THRESHOLD = 15;
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
    prevSignature: null,
    lastSentAt: 0,
  },
  screen: {
    key: "screen",
    label: "Screen",
    analysisMode: "screen",
    videoEl: document.getElementById("screenVideo"),
    canvasEl: document.getElementById("screenCanvas"),
    snapshotEl: document.getElementById("screenSnapshot"),
    liveStatusEl: document.getElementById("screenLiveStatus"),
    stream: null,
    prevSignature: null,
    lastSentAt: 0,
  },
};

const signatureCanvas = document.createElement("canvas");
signatureCanvas.width = 64;
signatureCanvas.height = 36;

let pollHandle = null;
let autoCaptureHandle = null;
let captureInFlight = false;
let latestState = null;
let inactivityReported = false;
let unchangedSinceMs = 0;
let consecutiveChangedTicks = 0;
let lastSpokenResponseEventId = "";
const completedClientActionIds = new Set();

function payloadFromControls() {
  return {
    goal: goalInput.value,
    interval_seconds: Number(intervalInput.value || 5),
    duration_seconds: Number(sessionDurationSlider.value || 15) * 60,
    browser_name: browserNameInput.value,
    browser_url: browserUrlInput.value,
  };
}

function formatDuration(totalSeconds) {
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

function activeSourceKeys() {
  return Object.keys(captureSources).filter((key) => Boolean(captureSources[key].stream));
}

function syncCaptureButtons() {
  const activeKeys = activeSourceKeys();
  captureBadge.textContent = activeKeys.length ? activeKeys.join(" + ") : "No source";
  captureButton.disabled = activeKeys.length === 0 || captureInFlight;
  autoCaptureButton.disabled = activeKeys.length === 0 || captureInFlight;
  stopCaptureButton.disabled = activeKeys.length === 0;
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

function postStimulus(type, payload = {}) {
  return postJson("/api/stimulus", { type, payload }).catch(() => {});
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
  const entries = Array.isArray(items) ? items : [];
  if (!entries.length) {
    const item = document.createElement("li");
    item.textContent = "No evidence listed.";
    evidenceList.appendChild(item);
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("li");
    item.textContent = entry;
    evidenceList.appendChild(item);
  }
}

function renderPendingActions(actions) {
  const entries = Array.isArray(actions) ? actions : [];
  actionList.textContent = JSON.stringify(entries, null, 2);
  if (!entries.length) {
    actionBadge.textContent = "None";
    actionBadge.className = "badge subtle";
    return;
  }
  actionBadge.textContent = `${entries.length} queued`;
  actionBadge.className = "badge warm";
}

function speakResponseIfNeeded(response) {
  if (!("speechSynthesis" in window)) {
    return;
  }
  if (!response || !response.should_speak || !response.spoken_text) {
    return;
  }

  const dedupeKey = String(response.spoken_text || "").trim().toLowerCase();
  if (!dedupeKey || dedupeKey === lastSpokenResponseEventId) {
    return;
  }

  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(String(response.spoken_text));
  utterance.rate = 1;
  utterance.pitch = 1;
  utterance.volume = 1;
  utterance.onstart = () => {
    lastSpokenResponseEventId = dedupeKey;
  };
  window.speechSynthesis.speak(utterance);
}

function renderState(state) {
  latestState = state;

  goalInput.value = state.goal || "";
  intervalInput.value = state.interval_seconds || 5;
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

  if (state.session_duration_seconds) {
    sessionDurationSlider.value = String(Math.max(1, Math.round(state.session_duration_seconds / 60)));
  }
  syncSessionDurationLabel();
  sessionCountdown.textContent = state.running
    ? `Time left: ${formatDuration(state.session_remaining_seconds || 0)}`
    : "Countdown idle";
  sessionCountdown.className = `badge ${state.running ? "running" : "subtle"}`;

  runningBadge.textContent = state.running ? "Running" : "Stopped";
  runningBadge.className = `badge ${state.running ? "running" : "subtle"}`;
  statusText.textContent = state.status || "Ready.";
  errorText.textContent = state.last_error || "";
  captureStatusText.textContent = `${state.capture_status || "Idle"} Vision model: ${state.vision_model || "none"}`;

  const agentOutput = state.agent_output || {};
  agentModeBadge.textContent = agentOutput.actor_mode || "heuristic";
  agentSummary.textContent = agentOutput.summary || "No agent decision yet.";
  renderEvidence(agentOutput.evidence || []);

  const planner = state.planner_output || {};
  actionSummary.textContent = planner.summary || "No follow-up actions yet.";
  renderPendingActions(planner.requested_actions || []);

  const response = state.personality_output || {};
  responseSummary.textContent = response.spoken_text || "No spoken response yet.";
  responseNotes.textContent = response.delivery_notes || "Idle.";
  if (response.should_speak) {
    responseBadge.textContent = "Ready";
    responseBadge.className = "badge ready";
  } else {
    responseBadge.textContent = "Idle";
    responseBadge.className = "badge subtle";
  }
  if (response.audio_url) {
    if (personalityAudio.getAttribute("src") !== response.audio_url) {
      personalityAudio.src = response.audio_url;
    }
    personalityAudio.hidden = false;
  } else {
    personalityAudio.pause();
    personalityAudio.removeAttribute("src");
    personalityAudio.load();
    personalityAudio.hidden = true;
  }
  speakResponseIfNeeded(response);

  const agent = state.agent || {};
  const ledger = agent.token_ledger || {};
  const multiplier = Number(ledger.efficiency_multiplier || 0);
  efficiencyBadge.textContent = multiplier > 0 ? `~${multiplier}x efficiency` : "No calls yet";
  efficiencyBadge.className = `badge ${multiplier >= 2 ? "ready" : "subtle"}`;

  const lastStimulus = agent.last_stimulus || {};
  stimulusBadge.textContent = lastStimulus.type ? lastStimulus.type : "No stimulus yet";
  agentStatusText.textContent = `Focus: ${(agent.status && agent.status.focus_state) || "unknown"} · Last turn: ${
    (agent.status && agent.status.last_turn_reason) || "none"
  } · ${((agent.status && agent.status.notes) || "").trim()}`;
  agentTodos.textContent = JSON.stringify(agent.pending_actions || [], null, 2);
  agentMemory.textContent = (agent.memory_recent || [])
    .map((entry) => `${entry.timestamp || ""}  [${entry.kind || "?"}]  ${entry.text || ""}`)
    .join("\n") || "No memory entries yet.";

  debugLogPath.textContent = `Debug log: ${state.debug_log_path || "state/debug_events.jsonl"}`;
  debugEventLog.textContent = JSON.stringify(state.debug_events || [], null, 2);
  agentJson.textContent = JSON.stringify(agentOutput, null, 2);
  responseJson.textContent = JSON.stringify(response, null, 2);

  browserOutput.textContent = (state.resources && state.resources.browser) || "No browser export yet.";
  webcamOutput.textContent = (state.resources && state.resources.webcam) || "No webcam summary yet.";
  screenshareOutput.textContent = (state.resources && state.resources.screenshare) || "No screenshare summary yet.";

  const paths = state.paths || {};
  pathsText.textContent = `Files: browser ${paths.browser || ""} | webcam ${paths.webcam || ""} | screenshare ${paths.screenshare || ""}`;

  processPendingAgentActions((agent && agent.pending_actions) || []).catch((err) => {
    errorText.textContent = err.message || "Client action failed.";
  });
  syncCaptureButtons();
}

function stopAutoCapture() {
  if (autoCaptureHandle) {
    clearInterval(autoCaptureHandle);
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
  syncCaptureButtons();
}

async function startSource(sourceKey) {
  const source = captureSources[sourceKey];
  stopSource(sourceKey);
  if (sourceKey === "webcam") {
    source.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
  } else {
    source.stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 8, max: 12 } },
      audio: false,
    });
  }
  const [track] = source.stream.getVideoTracks();
  track.addEventListener("ended", () => {
    stopSource(sourceKey, `${source.label} permission ended.`);
  });
  source.videoEl.srcObject = source.stream;
  await source.videoEl.play();
  source.liveStatusEl.textContent = `${source.label} connected.`;
  syncCaptureButtons();
}

function captureSourceFrame(sourceKey) {
  const source = captureSources[sourceKey];
  const nativeWidth = source.videoEl.videoWidth;
  const nativeHeight = source.videoEl.videoHeight;
  if (!nativeWidth || !nativeHeight) {
    throw new Error(`${source.label} stream is not ready yet.`);
  }
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

async function completeClientAction(actionId, result = {}) {
  if (!actionId) {
    return;
  }
  completedClientActionIds.add(actionId);
  await postJson("/api/client-action-complete", { action_id: actionId, result }).catch(() => {});
}

function hasPendingActionType(actionType) {
  const pending = (((latestState || {}).agent || {}).pending_actions || []);
  return pending.some((action) => action && action.type === actionType && action.id && !completedClientActionIds.has(action.id));
}

async function processPendingAgentActions(actions) {
  if (!Array.isArray(actions) || !actions.length || captureInFlight) {
    return;
  }
  for (const action of actions) {
    if (!action || !action.id || completedClientActionIds.has(action.id)) {
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
    if (action.type === "browser_rag") {
      await completeClientAction(action.id, { ok: true, status: "server_side" });
      continue;
    }
    await completeClientAction(action.id, { ok: false, status: "unsupported" });
  }
}

function shouldAutoAnalyzeSource(sourceKey, changed, diff, now) {
  if (sourceKey === "screen") {
    updateActivityTracking(changed);
    return hasPendingActionType("screen_scan") && changed;
  }
  if (sourceKey === "webcam") {
    return hasPendingActionType("webcam_scan") || diff >= STRONG_MOTION_THRESHOLD || now - captureSources.webcam.lastSentAt >= WEBCAM_MIN_PERIOD_MS;
  }
  return false;
}

async function summarizeSources(reason = "manual", requestedKeys = null, actionRequest = null) {
  const sourceKeys = (requestedKeys || activeSourceKeys()).filter((key) => Boolean(captureSources[key].stream));
  if (!sourceKeys.length || captureInFlight) {
    if (actionRequest && actionRequest.id) {
      await completeClientAction(actionRequest.id, { ok: false, status: "unavailable" });
    }
    return;
  }

  captureInFlight = true;
  syncCaptureButtons();
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
      let shouldSend = reason !== "auto";
      if (reason === "auto") {
        shouldSend = shouldAutoAnalyzeSource(sourceKey, changed, diff, now);
      }

      if (!shouldSend) {
        skippedCount += 1;
        postStimulus("frame_unchanged", {
          mode: source.analysisMode,
          width: source.videoEl.videoWidth,
          height: source.videoEl.videoHeight,
        });
        return;
      }

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
      analyzedCount += 1;
    }));

    if (reason === "manual" && latestState && latestState.running && analyzedCount > 0) {
      await postJson("/api/run-once", { ...payloadFromControls(), reason: "manual_capture_cycle" });
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
        ? `Analyzed ${analyzedCount} source(s); skipped ${skippedCount}.`
        : `No visual upload was needed; skipped ${skippedCount}.`;
  } catch (err) {
    errorText.textContent = err.message || "Capture failed.";
    if (actionRequest && actionRequest.id) {
      await completeClientAction(actionRequest.id, { ok: false, status: "error", message: err.message || "Capture failed." });
    }
    stopAutoCapture();
  } finally {
    captureInFlight = false;
    syncCaptureButtons();
  }
}

async function runOnce() {
  runOnceButton.disabled = true;
  try {
    await postJson("/api/run-once", { ...payloadFromControls(), reason: "manual_run" });
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

async function freshStart() {
  freshStartButton.disabled = true;
  errorText.textContent = "";
  statusText.textContent = "Starting a fresh test session...";

  try {
    completedClientActionIds.clear();
    stopAutoCapture();
    stopSource("webcam");
    stopSource("screen");

    await postJson("/api/reset-stats");
    await postJson("/api/launch-browser", {
      browser_name: browserNameInput.value,
      browser_url: browserUrlInput.value,
    });
    await postJson("/api/start", payloadFromControls());

    const setupNotes = [];

    try {
      await startSource("webcam");
      setupNotes.push("webcam connected");
    } catch (err) {
      setupNotes.push(`webcam skipped: ${err.message || "permission denied"}`);
    }

    try {
      await startSource("screen");
      setupNotes.push("screen connected");
    } catch (err) {
      setupNotes.push(`screen skipped: ${err.message || "permission denied"}`);
    }

    if (activeSourceKeys().length > 0) {
      await summarizeSources("auto");
      autoCaptureHandle = setInterval(
        () => summarizeSources("auto"),
        Math.max(1000, Number(intervalInput.value || 5) * 1000),
      );
      autoCaptureButton.textContent = "Stop auto capture";
    }

    await loadState();
    statusText.textContent = setupNotes.length
      ? `Fresh session started: ${setupNotes.join(" · ")}.`
      : "Fresh session started.";
  } catch (err) {
    errorText.textContent = err.message || "Fresh start failed.";
    await loadState().catch(() => {});
  } finally {
    freshStartButton.disabled = false;
  }
}

async function stopSession() {
  stopButton.disabled = true;
  try {
    await postJson("/api/stop");
    stopAutoCapture();
    await loadState();
  } finally {
    stopButton.disabled = false;
  }
}

async function resetStats() {
  resetStatsButton.disabled = true;
  try {
    completedClientActionIds.clear();
    lastSpokenResponseEventId = "";
    stopAutoCapture();
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    await postJson("/api/reset-stats");
    await loadState();
  } finally {
    resetStatsButton.disabled = false;
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

async function exportTabs() {
  exportTabsButton.disabled = true;
  try {
    await postJson("/api/export-tabs");
    await loadState();
  } finally {
    exportTabsButton.disabled = false;
  }
}

function startPolling() {
  if (pollHandle) {
    clearInterval(pollHandle);
  }
  pollHandle = setInterval(() => {
    loadState().catch((err) => {
      errorText.textContent = err.message || "Unable to load state.";
    });
  }, 1000);
}

shareButton.addEventListener("click", async () => {
  try {
    await startSource("screen");
  } catch (err) {
    errorText.textContent = err.message || "Unable to start screen share.";
  }
});

webcamButton.addEventListener("click", async () => {
  try {
    await startSource("webcam");
  } catch (err) {
    errorText.textContent = err.message || "Unable to start webcam.";
  }
});

captureButton.addEventListener("click", () => summarizeSources("manual"));

autoCaptureButton.addEventListener("click", async () => {
  if (autoCaptureHandle) {
    stopAutoCapture();
    return;
  }
  await summarizeSources("auto");
  autoCaptureHandle = setInterval(() => summarizeSources("auto"), Math.max(1000, Number(intervalInput.value || 5) * 1000));
  autoCaptureButton.textContent = "Stop auto capture";
});

stopCaptureButton.addEventListener("click", () => {
  stopAutoCapture();
  stopSource("webcam", "Webcam stopped.");
  stopSource("screen", "Screen share stopped.");
  captureStatusText.textContent = "All capture sources stopped.";
});

runOnceButton.addEventListener("click", runOnce);
freshStartButton.addEventListener("click", freshStart);
startButton.addEventListener("click", startSession);
stopButton.addEventListener("click", stopSession);
resetStatsButton.addEventListener("click", resetStats);
launchBrowserButton.addEventListener("click", launchBrowser);
exportTabsButton.addEventListener("click", exportTabs);
sessionDurationSlider.addEventListener("input", syncSessionDurationLabel);

captureSources.webcam.liveStatusEl.textContent = "Webcam not connected.";
captureSources.screen.liveStatusEl.textContent = "Screen not connected.";
syncSessionDurationLabel();
syncCaptureButtons();
loadState().catch((err) => {
  errorText.textContent = err.message || "Unable to load state.";
});
startPolling();
