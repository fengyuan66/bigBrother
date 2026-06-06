import os
from pathlib import Path

from config import load_env_file


APP_DIR = Path(__file__).resolve().parent
load_env_file(APP_DIR / ".env")


def yes_no(value):
    return "yes" if value else "no"


def main():
    api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("BIG_BROTHER_BASE_URL", "https://ai.hackclub.com/proxy/v1")
    model = os.getenv("BIG_BROTHER_MODEL", "google/gemini-2.5-flash")

    print("Big Brother config")
    print(f"app_dir: {APP_DIR}")
    print(f"env_file: {APP_DIR / '.env'}")
    print(f"api_key_present: {yes_no(bool(api_key))}")
    print(f"base_url: {base_url}")
    print(f"model: {model}")
    print(f"demo_browser: {os.getenv('BIG_BROTHER_DEMO_BROWSER', 'Chrome')}")
    print(f"demo_url: {os.getenv('BIG_BROTHER_DEMO_URL', 'https://en.wikipedia.org/wiki/Calculus')}")
    print(f"focus_interval_seconds: {os.getenv('BIG_BROTHER_INTERVAL_SECONDS', '8')}")
    print(f"off_task_threshold: {os.getenv('BIG_BROTHER_OFF_TASK_THRESHOLD', '2')}")
    print()
    print("Quick commands")
    print(r".\run.ps1")
    print(r".\run.ps1 demo")
    print(r"python .\browser_live_demo.py")
    print(r".\run.ps1 app")
    print(r".\run.ps1 test")


if __name__ == "__main__":
    main()
