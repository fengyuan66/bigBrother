const goalInput = document.getElementById("goal");
const intervalInput = document.getElementById("intervalSeconds");
const thresholdInput = document.getElementById("threshold");
const capturePromptInput = document.getElementById("capturePrompt");
const browserNameInput = document.getElementById("browserName");
const browserUrlInput = document.getElementById("browserUrl");

const watcherBadge = document.getElementById("watcherBadge");
const statusBadge = document.getElementById("statusBadge");
const captureBadge = document.getElementById("captureBadge");
const runningBadge = document.getElementById("runningBadge");
const turnBadge = document.getElementById("turnBadge");
const actorModeBadge = document.getElementById("actorModeBadge");
const thresholdBadge = document.getElementById("thresholdBadge");
const mpaBadge = document.getElementById("mpaBadge");

const statusText = document.getElementById("statusText");
const captureStatusText = document.getElementById("captureStatusText");
const errorText = document.getElementById("errorText");
const streakValue = document.getElementById("streakValue");
const exportValue = document.getElementById("exportValue");
const watcherSummary = document.getElementById("watcherSummary");
const mpaSummary = document.getElementById("mpaSummary");
const evidenceList = document.getElementById("evidenceList");
const pathsText = document.getElementById("paths");

const webcamOutput = document.getElementById("webcamOutput");
const screenshareOutput = document.getElementById("screenshareOutput");
const browserOutput = document.getElementById("browserOutput");
const watcherOutput = document.getElementById("watcherOutput");
const mpaOutput = document.getElementById("mpaOutput");

const shareButton = document.getElementById("shareButton");
const webcamButton = document.getElementById("webcamButton");
const captureButton = document.getElementById("captureButton");
const autoCaptureButton = document.getElementById("autoCaptureButton");
const stopCaptureButton = document.getElementById("stopCaptureButton");
const runOnceButton = document.getElementById("runOnceButton");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const exportTabsButton = document.getElementById("exportTabsButton");
const launchBrowserButton = document.getElementById("launchBrowserButton");

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
  },
};

let pollHandle = null;
let autoCaptureHandle = null;
let captureInFlight = false;

function payloadFromControls() {
  return {
    goal: goalInput.value,
    interval_seconds: Number(intervalInput.value || 4),
    threshold: Number(thresholdInput.value || 2),
    browser_name: browserNameInput.value,
    browser_url: browserUrlInput.value,
  };
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
  const width = source.videoEl.videoWidth;
  const height = source.videoEl.videoHeight;
  if (!width || !height) {
    throw new Error(`${source.label} stream is not ready yet.`);
  }

  source.canvasEl.width = width;
  source.canvasEl.height = height;
  const context = source.canvasEl.getContext("2d");
  context.drawImage(source.videoEl, 0, 0, width, height);
  const imageDataUrl = source.canvasEl.toDataURL("image/jpeg", 0.92);
  source.snapshotEl.src = imageDataUrl;
  source.lastSnipAt = new Date().toLocaleTimeString();
  return imageDataUrl;
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
    item.textContent = "No relevant watcher evidence for the latest turn.";
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
  const streak = Number(state.off_task_streak || 0);
  const threshold = Number(state.threshold || 1);
  const remaining = Math.max(0, threshold - streak);
  thresholdBadge.textContent =
    remaining === 0
      ? `Threshold met: ${streak}/${threshold}`
      : `Toward MPA: ${streak}/${threshold}`;
  thresholdBadge.className = `badge ${remaining === 0 ? "ready" : "subtle"}`;
}

function renderMPA(mpa) {
  if (mpa.triggered && mpa.should_intervene) {
    mpaBadge.textContent = "Agenda ready";
    mpaBadge.className = "badge ready";
    mpaSummary.textContent = mpa.agenda || "MPA triggered.";
    return;
  }

  if (mpa.triggered && !mpa.should_intervene) {
    mpaBadge.textContent = "No intervention";
    mpaBadge.className = "badge subtle";
    mpaSummary.textContent = mpa.rationale || "MPA reviewed the evidence and declined intervention.";
    return;
  }

  mpaBadge.textContent = "Waiting";
  mpaBadge.className = "badge subtle";
  mpaSummary.textContent = mpa.rationale || "MPA is waiting for enough consecutive watcher positives.";
}

function renderState(state) {
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

  watcherBadge.textContent = state.watcher_enabled
    ? `Watcher online · ${state.watcher_model}`
    : "Watcher fallback mode";
  actorModeBadge.textContent = state.watcher_output.actor_mode || "unknown";

  runningBadge.textContent = state.running ? "Watcher running" : "Watcher stopped";
  runningBadge.className = `badge ${state.running ? "running" : "subtle"}`;

  statusBadge.textContent = state.watcher_output.off_task ? "Watcher says off task" : "Watcher says on task";
  statusBadge.className = `badge ${state.watcher_output.off_task ? "alert" : "idle"}`;

  turnBadge.textContent = state.last_turn_at ? `Last turn: ${state.last_turn_at}` : "No turns yet";
  statusText.textContent = state.status || "Ready.";
  captureStatusText.textContent = `${state.capture_status} Vision model: ${state.vision_model}`;
  errorText.textContent = state.last_error || "";
  streakValue.textContent = String(state.off_task_streak);
  exportValue.textContent = String(state.last_export.count || 0);

  renderThreshold(state);
  watcherSummary.textContent = state.watcher_output.summary || "No watcher output yet.";
  renderEvidence(state.watcher_output.relevant_evidence || []);

  const mpa = state.mpa_output || {};
  renderMPA(mpa);

  const paths = state.paths || {};
  pathsText.textContent = `Files in use: webcam ${paths.webcam || ""} | screenshare ${paths.screenshare || ""} | browser ${paths.browser || ""}`;

  webcamOutput.textContent = state.resources.webcam;
  screenshareOutput.textContent = state.resources.screenshare;
  browserOutput.textContent = state.resources.browser;
  watcherOutput.textContent = JSON.stringify(state.watcher_output, null, 2);
  mpaOutput.textContent = JSON.stringify(mpa, null, 2);

  syncCaptureBadges();
}

async function summarizeSources(reason = "manual", requestedKeys = null) {
  const sourceKeys = (requestedKeys || activeSourceKeys()).filter((key) => Boolean(captureSources[key].stream));
  if (sourceKeys.length === 0 || captureInFlight) {
    return;
  }

  captureInFlight = true;
  syncCaptureBadges();
  errorText.textContent = "";

  try {
    for (const sourceKey of sourceKeys) {
      const source = captureSources[sourceKey];
      source.liveStatusEl.textContent =
        reason === "auto"
          ? `${source.label} snipping on the current tick...`
          : `${source.label} capturing a fresh frame...`;

      const imageDataUrl = captureSourceFrame(sourceKey);
      await postJson("/api/analyze", {
        analysisMode: source.analysisMode,
        prompt: capturePromptInput.value.trim(),
        imageDataUrl,
      });

      source.liveStatusEl.textContent = `${source.label} updated at ${source.lastSnipAt}.`;
    }

    await loadState();
    captureStatusText.textContent = `Updated ${formatSourceList(sourceKeys)} on this tick.`;
  } catch (error) {
    errorText.textContent = error.message || "Capture failed.";
    stopAutoCapture();
  } finally {
    captureInFlight = false;
    syncCaptureBadges();
  }
}

async function runOnce() {
  runOnceButton.disabled = true;
  try {
    await postJson("/api/run-once", payloadFromControls());
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

async function stopSession() {
  stopButton.disabled = true;
  try {
    await postJson("/api/stop");
    await loadState();
  } finally {
    stopButton.disabled = false;
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

  const seconds = Number(intervalInput.value || 4);
  if (!Number.isFinite(seconds) || seconds < 4) {
    errorText.textContent = "Choose an interval of at least 4 seconds.";
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
exportTabsButton.addEventListener("click", exportTabs);
launchBrowserButton.addEventListener("click", launchBrowser);

captureSources.webcam.liveStatusEl.textContent = "Webcam not connected.";
captureSources.screen.liveStatusEl.textContent = "Screenshare not connected.";
syncCaptureBadges();
loadState().catch((error) => {
  errorText.textContent = error.message;
});
startPolling();
