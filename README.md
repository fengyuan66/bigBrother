# Big Brother

A local desktop focus monitor that checks your screen every few seconds against a stated task and nudges you back when you drift.

This MVP is designed for self-monitoring only. It keeps monitoring visible, has a stop button, and does not save screenshot history by default.

## What It Does

- Lets you enter a focus goal, such as `I am going to study calculus`.
- Captures your screen every 5-10 seconds.
- Uses an OpenAI vision model when `OPENAI_API_KEY` is set.
- Falls back to a local placeholder evaluator when no API key is present.
- Shows an always-on-top reminder after repeated off-task checks.
- Exports open tab titles and URLs from supported browsers to `tabs.txt`.

## Setup

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Optional, for AI vision checks:

```powershell
$env:OPENAI_API_KEY = "your_api_key_here"
```

Run it:

```powershell
.\run.ps1
```

## Export Browser Tabs

Browsers do not expose all tabs to desktop apps by default. To make tab export work, close your browser, then launch it from this folder with one of these commands:

```powershell
.\start-debug-browser.ps1 chrome
.\start-debug-browser.ps1 edge
.\start-debug-browser.ps1 brave
```

Then open the tabs you want Big Brother to see and click **Export Tabs** in the app.

The tab list is saved to:

```text
tabs.txt
```

## Browser Live Demo

This demo opens a dedicated browser window and writes a near-real-time text snapshot of the visible page content and tab URLs.

```powershell
python browser_live_demo.py
```

Click **Launch Browser**, then **Start Live Output**. The live feed is shown in the app and saved to:

```text
browser_live.txt
```

This uses a separate browser profile in the project folder so the demo is explicit and visible.

## Notes

- Screenshots are sent to OpenAI only if `OPENAI_API_KEY` is present.
- Screenshots are not written to disk unless you enable debugging in the code.
- Tab export writes tab titles and URLs to `tabs.txt` only when you click **Export Tabs**.
- Browser live output writes page text and URLs to `browser_live.txt` only while the demo is running.
- The app works best if you monitor your primary display.
- If reminders feel too aggressive, raise the interval or the off-task threshold.
