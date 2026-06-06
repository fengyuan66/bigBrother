# Hack Club Screen Summary Demo

Tiny local demo that captures either your screen or your webcam in the browser and sends a frame to the Hack Club AI API using `qwen/qwen3-vl-235b-a22b-instruct`.

## What it does

- Uses `navigator.mediaDevices.getDisplayMedia()` so you can choose a screen, window, or tab.
- Uses `navigator.mediaDevices.getUserMedia()` for webcam capture.
- Captures a still frame locally in your browser.
- Sends that frame to a tiny Python proxy at `http://127.0.0.1:8000/api/analyze`.
- Writes the returned summary to local files in the main project structure.
- The proxy forwards the request to Hack Club's OpenAI-compatible endpoint:
  - Base URL: `https://ai.hackclub.com/proxy/v1`
  - Route: `POST /chat/completions`
  - Model: `qwen/qwen3-vl-235b-a22b-instruct`

## Run it

1. Get a Hack Club AI API key from `https://ai.hackclub.com/`.
2. Create a `.env` file in this folder:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Edit `.env` and add your key:

   ```env
   HACKCLUB_API_KEY=your-key-here
   ```

4. Start the local server:

   ```powershell
   python server.py
   ```

5. Open `http://127.0.0.1:8000`.
6. Click `Share screen` for desktop analysis, or `Use webcam` for real-world analysis.
7. Click `Capture and summarize`.

## Local output files

The web demo still shows the summary in the page, but it now also saves the result locally:

- Webcam captures write to `sources/video/latest.txt` and `sources/video/latest.json`
- Screenshare captures write to `sources/webcam/latest.txt` and `sources/webcam/latest.json`
- Structured latest summaries are also mirrored to:
  - `summaries/webcam_summary.json`
  - `summaries/screen_summary.json`

## Notes

- The browser keeps the live screen stream local.
- Only the captured image frame is sent when you click summarize or enable auto mode.
- The model can only describe what is visible in the captured frame. Small text or occluded UI may be uncertain.
- The server reads `.env` on startup without any extra Python packages.
- Webcam mode aims to produce an AI-agent-friendly summary of your actions, surroundings, and likely task.
