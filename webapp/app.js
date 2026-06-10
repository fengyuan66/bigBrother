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
const evidenceList = document.getElementById("evidenceList");
const pathsText = document.getElementById("paths");
const actorStageGrid = document.getElementById("actorStageGrid");
const debugLogPath = document.getElementById("debugLogPath");
const debugEventLog = document.getElementById("debugEventLog");

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
let lastSpokenPersonalityEventId = "";
let availableSpeechVoices = [];
let audioContext = null;
const lastActorStageVersions = {};
let latestState = null;
let speechInFlight = false;
let speechPauseUntilMs = 0;

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
      personality.spoken_text || "Personality actor is waiting for an MPA agenda.";
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

  watcherBadge.textContent = state.watcher_enabled
    ? `Watcher online · ${state.watcher_model}`
    : "Watcher fallback mode";
  actorModeBadge.textContent = state.watcher_output.actor_mode || "unknown";

  runningBadge.textContent = state.running ? "Watcher running" : "Watcher stopped";
  runningBadge.className = `badge ${state.running ? "running" : "subtle"}`;
  guidedBadge.textContent = state.running ? "Session active" : "Ready to launch";
  guidedBadge.className = `badge ${state.running ? "running" : "subtle"}`;

  const cooldownRemaining = Number(state.cooldown_remaining_seconds || 0);
  statusBadge.textContent =
    cooldownRemaining > 0
      ? "Watcher cooldown active"
      : state.watcher_output.off_task
        ? "Watcher says off task"
        : "Watcher says on task";
  statusBadge.className = `badge ${
    cooldownRemaining > 0 ? "warm" : state.watcher_output.off_task ? "alert" : "idle"
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
    ? "Big Brother is live. The tab will keep speaking interventions until the timer expires."
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
  watcherSummary.textContent = state.watcher_output.summary || "No watcher output yet.";
  renderEvidence(state.watcher_output.relevant_evidence || []);

  const mpa = state.mpa_output || {};
  renderMPA(mpa);
  const personality = state.personality_output || {};
  renderPersonality(personality);
  speakPersonalityIfNeeded(personality, state.last_turn_at || "");
  renderActorStages(state.actor_stages || {}, state.snapshot_at || "");
  playActorStageUpdates(state.actor_stages || {});
  debugLogPath.textContent = `Debug log file: ${state.debug_log_path || "state/debug_events.jsonl"} | Live events in memory: ${
    (state.debug_events || []).length
  }`;
  debugEventLog.textContent = formatDebugEvents(state.debug_events || []);

  const paths = state.paths || {};
  pathsText.textContent = `Files in use: webcam ${paths.webcam || ""} | screenshare ${paths.screenshare || ""} | browser ${paths.browser || ""}`;

  webcamOutput.textContent = state.resources.webcam;
  screenshareOutput.textContent = state.resources.screenshare;
  browserOutput.textContent = state.resources.browser;
  watcherOutput.textContent = JSON.stringify(state.watcher_output, null, 2);
  mpaOutput.textContent = JSON.stringify(mpa, null, 2);
  personalityOutput.textContent = JSON.stringify(personality, null, 2);

  syncCaptureBadges();
}

async function summarizeSources(reason = "manual", requestedKeys = null) {
  const sourceKeys = (requestedKeys || activeSourceKeys()).filter((key) => Boolean(captureSources[key].stream));
  if (sourceKeys.length === 0 || captureInFlight) {
    return;
  }
  if (reason === "auto" && speechPauseActive()) {
    captureStatusText.textContent = "Voice intervention pause active. Reassessment resumes in a few seconds.";
    return;
  }

  captureInFlight = true;
  syncCaptureBadges();
  errorText.textContent = "";

  try {
    await Promise.all(sourceKeys.map(async (sourceKey) => {
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
    }));

    if (latestState && latestState.running) {
      await postJson("/api/run-once", payloadFromControls());
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

    guidedStatus.textContent = "Starting watcher pipeline...";
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
