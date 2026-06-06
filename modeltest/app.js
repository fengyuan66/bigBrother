const shareButton = document.getElementById("shareButton");
const webcamButton = document.getElementById("webcamButton");
const captureButton = document.getElementById("captureButton");
const autoButton = document.getElementById("autoButton");
const stopButton = document.getElementById("stopButton");
const promptInput = document.getElementById("prompt");
const intervalInput = document.getElementById("intervalSeconds");
const sourceBadge = document.getElementById("sourceBadge");
const statusBadge = document.getElementById("statusBadge");
const modelName = document.getElementById("modelName");
const output = document.getElementById("output");
const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const snapshot = document.getElementById("snapshot");

let stream = null;
let autoTimer = null;
let captureInFlight = false;
let activeSource = null;

function setSource(source) {
  activeSource = source;
  sourceBadge.textContent = source === "webcam" ? "Webcam mode" : source === "screen" ? "Screen mode" : "No source";
}

function setStatus(label, variant = "idle") {
  statusBadge.textContent = label;
  statusBadge.className = `badge ${variant}`;
}

function setOutput(message, empty = false) {
  output.textContent = message;
  output.classList.toggle("empty", empty);
}

function stopAutoMode() {
  if (autoTimer) {
    window.clearInterval(autoTimer);
    autoTimer = null;
  }
  autoButton.textContent = "Start auto mode";
  stopButton.disabled = true;
}

function setControlsEnabled(hasStream) {
  captureButton.disabled = !hasStream || captureInFlight;
  autoButton.disabled = !hasStream || captureInFlight;
  shareButton.textContent = activeSource === "screen" && hasStream ? "Re-share screen" : "Share screen";
  webcamButton.textContent = activeSource === "webcam" && hasStream ? "Restart webcam" : "Use webcam";
}

function stopCurrentStream() {
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }
  video.srcObject = null;
}

async function startStream(source) {
  stopAutoMode();
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
    stopCurrentStream();
    setSource(null);
    setControlsEnabled(false);
    stopAutoMode();
    setStatus(source === "webcam" ? "Webcam ended" : "Screen share ended", "warn");
  });

  video.srcObject = stream;
  await video.play();

  setSource(source);
  setControlsEnabled(true);
  setStatus(source === "webcam" ? "Webcam ready" : "Screen ready", "ready");
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

async function summarizeFrame(reason = "manual") {
  if (!stream || captureInFlight || !activeSource) {
    return;
  }

  captureInFlight = true;
  setControlsEnabled(true);
  setStatus(reason === "auto" ? "Auto summarizing..." : "Summarizing...", "busy");
  setOutput("Analyzing the latest frame...", false);

  try {
    const imageDataUrl = captureFrame();
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        analysisMode: activeSource,
        prompt: promptInput.value.trim(),
        imageDataUrl,
      }),
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.details ? `${payload.error}\n\n${payload.details}` : payload.error);
    }

    modelName.textContent = `${payload.model} - ${payload.analysisMode}`;
    setOutput(payload.summary);
    if (payload.writtenFiles) {
      setOutput(
        `${payload.summary}\n\nSaved locally:\n- ${payload.writtenFiles.latestText}\n- ${payload.writtenFiles.latestJson}\n- ${payload.writtenFiles.summaryJson}`,
      );
    }
    setStatus(reason === "auto" ? "Auto updated" : "Summary ready", "ready");
  } catch (error) {
    setStatus("Request failed", "error");
    setOutput(error.message || "Something went wrong.");
    stopAutoMode();
  } finally {
    captureInFlight = false;
    setControlsEnabled(Boolean(stream));
  }
}

shareButton.addEventListener("click", async () => {
  try {
    setStatus("Waiting for permission...", "busy");
    await startStream("screen");
  } catch (error) {
    setStatus("Permission denied", "error");
    setOutput(error.message || "Unable to start screen sharing.");
  }
});

webcamButton.addEventListener("click", async () => {
  try {
    setStatus("Waiting for webcam...", "busy");
    await startStream("webcam");
  } catch (error) {
    setStatus("Permission denied", "error");
    setOutput(error.message || "Unable to start webcam.");
  }
});

captureButton.addEventListener("click", () => summarizeFrame("manual"));

autoButton.addEventListener("click", async () => {
  if (autoTimer) {
    stopAutoMode();
    setStatus("Auto mode stopped", "idle");
    return;
  }

  const seconds = Number(intervalInput.value);
  if (!Number.isFinite(seconds) || seconds < 5) {
    setOutput("Choose an interval of at least 5 seconds.");
    return;
  }

  await summarizeFrame("auto");
  autoTimer = window.setInterval(() => summarizeFrame("auto"), seconds * 1000);
  autoButton.textContent = "Pause auto mode";
  stopButton.disabled = false;
});

stopButton.addEventListener("click", () => {
  stopAutoMode();
  setStatus("Auto mode stopped", "idle");
});

setControlsEnabled(false);
setSource(null);
