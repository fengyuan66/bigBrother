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
const statusText = document.getElementById("statusText");
const captureStatusText = document.getElementById("captureStatusText");
const errorText = document.getElementById("errorText");
const streakValue = document.getElementById("streakValue");
const exportValue = document.getElementById("exportValue");
const watcherSummary = document.getElementById("watcherSummary");
const evidenceList = document.getElementById("evidenceList");
const pathsText = document.getElementById("paths");

const webcamOutput = document.getElementById("webcamOutput");
const screenshareOutput = document.getElementById("screenshareOutput");
const browserOutput = document.getElementById("browserOutput");
const watcherOutput = document.getElementById("watcherOutput");

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

const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const snapshot = document.getElementById("snapshot");

let pollHandle = null;
let stream = null;
let autoCaptureHandle = null;
let captureInFlight = false;
let activeSource = null;

function payloadFromControls() {
  return {
    goal: goalInput.value,
    interval_seconds: Number(intervalInput.value || 4),
    threshold: Number(thresholdInput.value || 2),
    browser_name: browserNameInput.value,
    browser_url: browserUrlInput.value,
  };
}

function setCaptureSource(source) {
  activeSource = source;
  captureBadge.textContent =
    source === "webcam" ? "Webcam mode" : source === "screen" ? "Screen mode" : "No source";
}

function setCaptureControlsEnabled(hasStream) {
  captureButton.disabled = !hasStream || captureInFlight;
  autoCaptureButton.disabled = !hasStream || captureInFlight;
  stopCaptureButton.disabled = !hasStream;
  shareButton.textContent =
    activeSource === "screen" && hasStream ? "Re-share screen" : "Share screen";
  webcamButton.textContent =
    activeSource === "webcam" && hasStream ? "Restart webcam" : "Use webcam";
}

function stopCurrentStream() {
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }
  video.srcObject = null;
  setCaptureSource(null);
  setCaptureControlsEnabled(false);
}

function stopAutoCapture() {
  if (autoCaptureHandle) {
    window.clearInterval(autoCaptureHandle);
    autoCaptureHandle = null;
  }
  autoCaptureButton.textContent = "Start auto capture";
}

async function startStream(source) {
  stopAutoCapture();
  stopCurrentStream();

  if (source === "webcam") {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "user",
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
  } else {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: {
        frameRate: { ideal: 8, max: 12 },
      },
      audio: false,
    });
  }

  const [track] = stream.getVideoTracks();
  track.addEventListener("ended", () => {
    stopAutoCapture();
    stopCurrentStream();
    captureStatusText.textContent =
      source === "webcam" ? "Webcam permission ended." : "Screen-share permission ended.";
  });

  video.srcObject = stream;
  await video.play();
  setCaptureSource(source);
  setCaptureControlsEnabled(true);
  captureStatusText.textContent =
    source === "webcam"
      ? "Webcam ready. Capture to write a webcam summary."
      : "Screen ready. Capture to write a screenshare summary.";

  if (source === "screen") {
    window.setTimeout(() => {
      summarizeFrame("auto");
    }, 250);
  }
}

function captureFrame() {
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!width || !height) {
    throw new Error("Video stream is not ready yet.");
  }
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(video, 0, 0, width, height);
  const imageDataUrl = canvas.toDataURL("image/jpeg", 0.92);
  snapshot.src = imageDataUrl;
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
    ? `Watcher enabled · ${state.watcher_model}`
    : "Watcher fallback mode";
  actorModeBadge.textContent = state.watcher_output.actor_mode || "unknown";

  runningBadge.textContent = state.running ? "Watcher running" : "Watcher stopped";
  runningBadge.className = `badge ${state.running ? "running" : "subtle"}`;

  statusBadge.textContent = state.watcher_output.off_task ? "Off task" : "On task / idle";
  statusBadge.className = `badge ${state.watcher_output.off_task ? "alert" : "idle"}`;

  turnBadge.textContent = state.last_turn_at ? `Last turn · ${state.last_turn_at}` : "No turns yet";
  statusText.textContent = state.status;
  captureStatusText.textContent = `${state.capture_status} Vision model: ${state.vision_model}`;
  errorText.textContent = state.last_error || "";
  streakValue.textContent = String(state.off_task_streak);
  exportValue.textContent = String(state.last_export.count || 0);

  watcherSummary.textContent = state.watcher_output.summary || "No watcher output yet.";
  evidenceList.innerHTML = "";
  const evidence = state.watcher_output.relevant_evidence || [];
  if (evidence.length === 0) {
    const item = document.createElement("li");
    item.textContent = "None.";
    evidenceList.appendChild(item);
  } else {
    for (const entry of evidence) {
      const item = document.createElement("li");
      item.textContent = entry;
      evidenceList.appendChild(item);
    }
  }

  const paths = state.paths || {};
  pathsText.textContent = `Watching files — webcam: ${paths.webcam || ""} | screenshare: ${paths.screenshare || ""} | browser: ${paths.browser || ""}`;

  webcamOutput.textContent = state.resources.webcam;
  screenshareOutput.textContent = state.resources.screenshare;
  browserOutput.textContent = state.resources.browser;
  watcherOutput.textContent = JSON.stringify(state.watcher_output, null, 2);
}

async function summarizeFrame(reason = "manual") {
  if (!stream || captureInFlight || !activeSource) {
    return;
  }
  captureInFlight = true;
  setCaptureControlsEnabled(true);
  captureStatusText.textContent =
    reason === "auto" ? "Auto summarizing latest frame..." : "Summarizing latest frame...";
  errorText.textContent = "";

  try {
    const imageDataUrl = captureFrame();
    const payload = await postJson("/api/analyze", {
      analysisMode: activeSource,
      prompt: capturePromptInput.value.trim(),
      imageDataUrl,
    });
    await loadState();
    captureStatusText.textContent =
      `${payload.analysisMode === "webcam" ? "Webcam" : "Screenshare"} summary updated and saved.`;
  } catch (error) {
    errorText.textContent = error.message || "Capture failed.";
    stopAutoCapture();
  } finally {
    captureInFlight = false;
    setCaptureControlsEnabled(Boolean(stream));
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
    captureStatusText.textContent = "Waiting for screen-share permission...";
    await startStream("screen");
  } catch (error) {
    errorText.textContent = error.message || "Unable to start screen share.";
  }
});

webcamButton.addEventListener("click", async () => {
  try {
    captureStatusText.textContent = "Waiting for webcam permission...";
    await startStream("webcam");
  } catch (error) {
    errorText.textContent = error.message || "Unable to start webcam.";
  }
});

captureButton.addEventListener("click", () => summarizeFrame("manual"));

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

  await summarizeFrame("auto");
  autoCaptureHandle = window.setInterval(() => summarizeFrame("auto"), seconds * 1000);
  autoCaptureButton.textContent = "Pause auto capture";
  stopCaptureButton.disabled = false;
});

stopCaptureButton.addEventListener("click", () => {
  stopAutoCapture();
  stopCurrentStream();
  captureStatusText.textContent = "Capture stopped.";
});

runOnceButton.addEventListener("click", runOnce);
startButton.addEventListener("click", startSession);
stopButton.addEventListener("click", stopSession);
exportTabsButton.addEventListener("click", exportTabs);
launchBrowserButton.addEventListener("click", launchBrowser);

setCaptureSource(null);
setCaptureControlsEnabled(false);
loadState().catch((error) => {
  errorText.textContent = error.message;
});
startPolling();
