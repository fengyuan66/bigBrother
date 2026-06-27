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
const mapCurrentStep = document.getElementById("mapCurrentStep");
const mapCurrentReason = document.getElementById("mapCurrentReason");
const mapCurrentStation = document.getElementById("mapCurrentStation");
const mapCurrentRoute = document.getElementById("mapCurrentRoute");
const mapCurrentStationDot = document.getElementById("mapCurrentStationDot");
const stimulusToast = document.getElementById("stimulusToast");
const stimulusToastTitle = document.getElementById("stimulusToastTitle");
const stimulusToastBody = document.getElementById("stimulusToastBody");

const evidenceList = document.getElementById("evidenceList");
const actionList = document.getElementById("actionList");
const agentTodos = document.getElementById("agentTodos");
const agentMemory = document.getElementById("agentMemory");
const debugEventLog = document.getElementById("debugEventLog");
const agentJson = document.getElementById("agentJson");
const responseJson = document.getElementById("responseJson");
const stimulusHistoryTableBody = document.getElementById("stimulusHistoryTableBody");

const browserOutput = document.getElementById("browserOutput");
const webcamOutput = document.getElementById("webcamOutput");
const screenshareOutput = document.getElementById("screenshareOutput");
const personalityAudio = document.getElementById("personalityAudio");
const metroNodes = Array.from(document.querySelectorAll(".metro-node"));
const metroLines = {
  stimuliContext: document.getElementById("line-stimuli-context"),
  contextActor: document.getElementById("line-context-actor"),
  actorSufficient: document.getElementById("line-actor-sufficient"),
  yesResponse: document.getElementById("line-yes-response"),
  noGetResource: document.getElementById("line-no-getresource"),
  getResourceActor: document.getElementById("line-getresource-actor"),
  sufficientNo: document.getElementById("line-sufficient-no"),
  sufficientYes: document.getElementById("line-sufficient-yes"),
};

const shareButton = document.getElementById("shareButton");
const webcamButton = document.getElementById("webcamButton");
const stopCaptureButton = document.getElementById("stopCaptureButton");
const launchBrowserButton = document.getElementById("launchBrowserButton");
const exportTabsButton = document.getElementById("exportTabsButton");
const freshStartButton = document.getElementById("freshStartButton");
const runOnceButton = document.getElementById("runOnceButton");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const resetStatsButton = document.getElementById("resetStatsButton");

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
  },
};

let pollHandle = null;
let captureInFlight = false;
let latestState = null;
let currentSpeechEventId = "";
let lastCompletedSpeechEventId = "";
let lastStimulusCueKey = "";
let stimulusToastHandle = null;
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

function formatTimestampLabel(timestamp, fallbackUnix = 0) {
  const text = String(timestamp || "").trim();
  if (text) {
    return text.replace("T", " ");
  }
  const unix = Number(fallbackUnix || 0);
  if (!unix) {
    return "Unknown";
  }
  return new Date(unix * 1000).toISOString().replace("T", " ").replace("Z", "");
}

function formatStimulusRelevancy(seconds) {
  if (seconds === null || seconds === undefined || seconds === "") {
    return "Current";
  }
  const value = Number(seconds);
  if (!Number.isFinite(value)) {
    return "Current";
  }
  if (value < 60) {
    const decimals = value < 10 ? 3 : 1;
    return `${value.toFixed(decimals).replace(/0+$/, "").replace(/\.$/, "")}s`;
  }
  const minutes = Math.floor(value / 60);
  const remainder = value - minutes * 60;
  if (minutes < 60) {
    if (remainder <= 0.001) {
      return `${minutes}m`;
    }
    return `${minutes}m ${Math.round(remainder)}s`;
  }
  const hours = Math.floor(minutes / 60);
  const leftoverMinutes = minutes - hours * 60;
  if (leftoverMinutes === 0) {
    return `${hours}h`;
  }
  return `${hours}h ${leftoverMinutes}m`;
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

function renderStimulusHistory(items) {
  if (!stimulusHistoryTableBody) {
    return;
  }
  const entries = Array.isArray(items) ? items : [];
  stimulusHistoryTableBody.innerHTML = "";
  if (!entries.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "No stimuli recorded yet.";
    row.appendChild(cell);
    stimulusHistoryTableBody.appendChild(row);
    return;
  }

  for (const entry of entries) {
    const row = document.createElement("tr");
    if (!entry.replaced_at) {
      row.classList.add("is-current");
    }

    const typeCell = document.createElement("td");
    const typeChip = document.createElement("span");
    typeChip.className = "history-type";
    typeChip.textContent = String(entry.type || "unknown");
    typeCell.appendChild(typeChip);

    const timeCell = document.createElement("td");
    timeCell.textContent = formatTimestampLabel(entry.emitted_at, entry.emitted_at_unix);

    const relevancyCell = document.createElement("td");
    relevancyCell.textContent = formatStimulusRelevancy(entry.relevancy_duration_seconds);
    if (!entry.replaced_at) {
      relevancyCell.classList.add("history-current");
    }

    row.appendChild(typeCell);
    row.appendChild(timeCell);
    row.appendChild(relevancyCell);
    stimulusHistoryTableBody.appendChild(row);
  }
}

function playStimulusCue(stimulusType) {
  if (!stimulusType || !window.AudioContext) {
    return;
  }
  const context = new window.AudioContext();
  const now = context.currentTime;
  const gain = context.createGain();
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.06, now + 0.015);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.22);
  gain.connect(context.destination);

  const first = context.createOscillator();
  first.type = "triangle";
  first.frequency.setValueAtTime(740, now);
  first.connect(gain);
  first.start(now);
  first.stop(now + 0.11);

  const second = context.createOscillator();
  second.type = "sine";
  second.frequency.setValueAtTime(988, now + 0.09);
  second.connect(gain);
  second.start(now + 0.09);
  second.stop(now + 0.22);

  setTimeout(() => {
    context.close().catch(() => {});
  }, 350);
}

function maybePlayStimulusCue(state) {
  const agent = state.agent || {};
  const lastStimulus = agent.last_stimulus || {};
  const stimulusType = String(lastStimulus.type || "").trim();
  const emittedAt = String(lastStimulus.emitted_at || lastStimulus.emitted_at_unix || "").trim();
  if (!stimulusType || stimulusType === "heartbeat") {
    return;
  }
  const cueKey = `${stimulusType}:${emittedAt}`;
  if (!emittedAt || cueKey === lastStimulusCueKey) {
    return;
  }
  lastStimulusCueKey = cueKey;
  playStimulusCue(stimulusType);
  showStimulusToast(stimulusType, lastStimulus);
}

function showStimulusToast(stimulusType, stimulus = {}) {
  if (!stimulusToast || !stimulusToastTitle || !stimulusToastBody) {
    return;
  }
  const payload = stimulus.payload || {};
  const detail =
    String(payload.note || payload.reason || payload.tab_url || payload.url || payload.goal || "").trim() ||
    "A new trigger arrived.";
  stimulusToastTitle.textContent = `Stimulus: ${stimulusType}`;
  stimulusToastBody.textContent = detail;
  stimulusToast.hidden = false;
  if (stimulusToastHandle) {
    clearTimeout(stimulusToastHandle);
  }
  stimulusToastHandle = setTimeout(() => {
    stimulusToast.hidden = true;
  }, 2000);
}

function setMetroNodeState(name, state = "idle") {
  const node = metroNodes.find((entry) => entry.dataset.node === name);
  if (!node) {
    return;
  }
  node.dataset.state = state;
}

function setMetroLineState(lineEl, state = "") {
  if (!lineEl) {
    return;
  }
  lineEl.className = "metro-line";
  if (state === "active" || state === "in-progress") {
    lineEl.classList.add("active");
  }
}

function markMetroNode(name, state) {
  setMetroNodeState(name, state);
}

function markMetroLine(name, state) {
  setMetroLineState(metroLines[name], state);
}

function markMetroStep(nodeName, incomingLineName = "", state = "complete") {
  markMetroNode(nodeName, state);
  if (incomingLineName) {
    markMetroLine(incomingLineName, state);
  }
}

function setCurrentStation(station, route, reason, badgeText, badgeClass, dotClass = "station-idle") {
  mapCurrentStation.textContent = station;
  mapCurrentRoute.textContent = route;
  mapCurrentReason.textContent = reason;
  mapCurrentStep.textContent = badgeText;
  mapCurrentStep.className = `badge ${badgeClass}`;
  mapCurrentStationDot.className = `metro-station-dot ${dotClass}`;
}

function describeRequestedResources(actions) {
  const resourceActions = (Array.isArray(actions) ? actions : []).filter((entry) => {
    const actionType = String((entry && entry.type) || "").trim();
    return actionType === "browser_rag" || actionType === "screen_scan" || actionType === "webcam_scan";
  });
  if (!resourceActions.length) {
    return "";
  }
  return resourceActions.map((entry) => String(entry.type || "").replace("_", " ")).join(", ");
}

function renderAgentMetro(state) {
  for (const node of metroNodes) {
    node.dataset.state = "idle";
  }
  for (const line of Object.values(metroLines)) {
    setMetroLineState(line);
  }

  const speech = state.speech || {};
  const planner = state.planner_output || {};
  const agentOutput = state.agent_output || {};
  const agent = state.agent || {};
  const lastStimulus = agent.last_stimulus || {};
  const stimulusType = String(lastStimulus.type || "").trim() || String(state.last_turn_reason || "").replace("stimulus:", "");
  const pendingActions = Array.isArray(planner.requested_actions) ? planner.requested_actions : [];
  const hasResourceRequest = pendingActions.some((entry) => {
    const actionType = String((entry && entry.type) || "").trim();
    return actionType === "browser_rag" || actionType === "screen_scan" || actionType === "webcam_scan";
  }) || (Array.isArray(agentOutput.requested_resources) && agentOutput.requested_resources.length > 0);
  const speechInProgress = Boolean(speech.in_progress);
  const graceActive = Number(speech.grace_until_unix || 0) > Date.now() / 1000;
  const responseRequired = Boolean(agentOutput.response_required);
  const focusState = String(agentOutput.focus_state || "unknown").trim();
  const runningTurn = String(state.status || "").includes("Running agent turn");

  if (state.last_turn_at) {
    markMetroNode("context", "complete");
  }
  if (stimulusType) {
    markMetroNode("stimuli", runningTurn ? "in-progress" : "complete");
  }
  if (runningTurn) {
    markMetroLine("stimuliContext", "active");
    markMetroNode("context", "complete");
    markMetroStep("actor", "contextActor", "in-progress");
    setCurrentStation(
      "MPA AI LLM Actor",
      "Decision line",
      "The MPA actor is reading the latest evidence and deciding whether it is sufficient.",
      "Judging sufficiency",
      "running",
      "station-active",
    );
    return;
  }

  if (state.last_turn_at) {
    markMetroNode("context", "complete");
    markMetroNode("actor", "complete");
  }

  if (hasResourceRequest) {
    markMetroNode("sufficient", "complete");
    markMetroNode("no", "complete");
    markMetroLine("actorSufficient", "active");
    markMetroLine("sufficientNo", "active");
    markMetroStep("get-resource", "noGetResource", "in-progress");
    markMetroLine("getResourceActor", "active");
    setCurrentStation(
      "Get resource",
      "Resource loop",
      describeRequestedResources(pendingActions) || "The actor judged the evidence incomplete and requested the next source.",
      "Getting resource",
      "warm",
      "station-alert",
    );
    return;
  }

  if (state.last_turn_at) {
    markMetroNode("sufficient", "complete");
    markMetroNode("yes", "complete");
  }

  if (speechInProgress) {
    markMetroLine("actorSufficient", "active");
    markMetroLine("sufficientYes", "active");
    markMetroStep("response", "yesResponse", "in-progress");
    setCurrentStation(
      "Response",
      "Response line",
      "The response signal has been sent and Big Brother is frozen until narration finishes.",
      "Narrating",
      "running",
      "station-active",
    );
    return;
  }

  if (graceActive) {
    markMetroNode("response", "complete");
    setCurrentStation(
      "Response",
      "Response line",
      "Narration has ended. Big Brother is waiting through the post-speech grace window before reassessing.",
      "Grace pause",
      "ready",
      "station-complete",
    );
    return;
  }

  if (responseRequired) {
    markMetroNode("response", "complete");
    setCurrentStation(
      "Response",
      "Response line",
      String((state.personality_output || {}).spoken_text || "The actor has enough evidence and prepared a response."),
      "Response ready",
      "ready",
      "station-complete",
    );
    return;
  }

  if (state.last_turn_at) {
    setCurrentStation(
      focusState === "distracted" ? "Sufficient? (distracted)" : "Sufficient?",
      "Decision line",
      String(agentOutput.summary || "The latest evidence was judged sufficient without needing more resources."),
      focusState === "distracted" ? "Judged distracted" : "Sufficient",
      "running",
      "station-complete",
    );
    return;
  }

  setCurrentStation(
    "Idle",
    "Waiting for a new turn.",
    "Waiting for the next stimulus or manual run.",
    "Idle",
    "subtle",
    "station-idle",
  );
}

function speakResponseIfNeeded(response) {
  if (!("speechSynthesis" in window)) {
    return;
  }
  if (!response || !response.should_speak || !response.spoken_text) {
    return;
  }

  const eventId = String(response.event_id || "").trim();
  if (!eventId || eventId === currentSpeechEventId || eventId === lastCompletedSpeechEventId) {
    return;
  }

  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(String(response.spoken_text));
  utterance.rate = 1;
  utterance.pitch = 1;
  utterance.volume = 1;
  currentSpeechEventId = eventId;
  utterance.onstart = () => {
    postJson("/api/speech-started", {
      event_id: eventId,
      text: String(response.spoken_text || ""),
    }).catch(() => {});
  };
  utterance.onend = () => {
    lastCompletedSpeechEventId = eventId;
    currentSpeechEventId = "";
    postJson("/api/speech-finished", { event_id: eventId }).catch(() => {});
  };
  utterance.onerror = () => {
    currentSpeechEventId = "";
    postJson("/api/speech-finished", { event_id: eventId }).catch(() => {});
  };
  window.speechSynthesis.speak(utterance);
}

function renderState(state) {
  latestState = state;
  maybePlayStimulusCue(state);

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
  renderAgentMetro(state);

  const response = state.personality_output || {};
  const speechState = state.speech || {};
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
  if (!response.should_speak && currentSpeechEventId && !speechState.in_progress) {
    try {
      window.speechSynthesis.cancel();
    } catch (err) {
      // Ignore cancellation errors from the browser TTS engine.
    }
    currentSpeechEventId = "";
  }
  speakResponseIfNeeded(response);

  const agent = state.agent || {};
  const ledger = agent.token_ledger || {};
  const multiplier = Number(ledger.efficiency_multiplier || 0);
  efficiencyBadge.textContent = multiplier > 0 ? `~${multiplier}x efficiency` : "No calls yet";
  efficiencyBadge.className = `badge ${multiplier >= 2 ? "ready" : "subtle"}`;

  const lastStimulus = agent.last_stimulus || {};
  stimulusBadge.textContent = lastStimulus.type ? lastStimulus.type : "No stimulus yet";
  renderStimulusHistory(agent.stimulus_history || []);
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

async function summarizeSources(reason = "agent_action", requestedKeys = null, actionRequest = null) {
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
      analyzedCount += 1;
    }));

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
    await loadState();
  } finally {
    stopButton.disabled = false;
  }
}

async function resetStats() {
  resetStatsButton.disabled = true;
  try {
    completedClientActionIds.clear();
    currentSpeechEventId = "";
    lastCompletedSpeechEventId = "";
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

stopCaptureButton.addEventListener("click", () => {
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
