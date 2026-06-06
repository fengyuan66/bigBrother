# Big Brother

A local browser info demo that launches a dedicated browser window, reads open tabs, and exports lightweight browser data files you can inspect locally.

## What It Does

- Launches a dedicated Chrome, Edge, or Brave window with remote debugging enabled.
- Shows open tab titles and URLs from that demo browser.
- Exports tab summaries to `sources/browser/tabs.txt`.
- Exports richer per-snapshot page text to `sources/browser/browser_live.txt`.
- Exports a structured browser index to `sources/browser/index.json`.
- Exports a lightweight summary to `summaries/browser_summary.json`.
- Lets you run a one-shot snapshot or continuous live updates from the same UI.

## Setup

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create your local config:

```powershell
Copy-Item .env.example .env
```

Defaults included in `.env`:

- Demo browser, demo URL, and interval constants

Run the browser info demo:

```powershell
.\run.ps1
```

Check your active config at any time:

```powershell
.\run.ps1 doctor
```

Run a quick smoke test:

```powershell
.\run.ps1 test
```

## Browser Live Demo

This demo is now the main browser info module. It opens a dedicated browser window and writes both a near-real-time content snapshot and a lightweight tab summary.

```powershell
.\run.ps1 demo
```

In the app:

- Click `Launch Browser` to open the dedicated browser window.
- Click `Export Tabs` to write the current tab summary to `sources/browser/tabs.txt`.
- Click `Snapshot Once` to write both outputs once without starting the loop.
- Click `Start Live Output` to keep both files refreshed on the selected interval.

Generated files:

```text
sources/browser/browser_live.txt
sources/browser/tabs.txt
sources/browser/index.json
summaries/browser_summary.json
```

This uses a separate browser profile in the project folder so the demo is explicit and visible.
