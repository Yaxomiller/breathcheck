"""BreathCheck handheld launcher.

  python app.py            start the server and open the kiosk browser
  python app.py serve      server only (for development / remote browser)
  python app.py kiosk      server + fullscreen kiosk browser window
  python app.py term       text UI over SSH — no browser (alias: cli, simple)

Options:
  --port 8000              override the web port
  --host 0.0.0.0           override the bind host
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config, db  # noqa: E402


def _open_kiosk(url: str) -> None:
    time.sleep(1.5)
    candidates = (
        "chromium-browser", "chromium", "google-chrome", "msedge",
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
    )
    for browser in candidates:
        binary = shutil.which(browser) or (browser if Path(browser).is_file() else None)
        if binary:
            subprocess.Popen([binary, f"--app={url}", "--start-fullscreen", "--kiosk"])
            return
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="BreathCheck handheld analyzer")
    parser.add_argument(
        "mode", nargs="?", default="kiosk",
        choices=["kiosk", "serve", "web", "term", "cli", "simple"],
    )
    parser.add_argument("--host", default=config.WEB_HOST)
    parser.add_argument("--port", type=int, default=config.WEB_PORT)
    args = parser.parse_args()

    if args.mode in ("term", "cli", "simple"):
        from src import terminal
        terminal.run()
        return

    db.init_db()

    url = f"http://127.0.0.1:{args.port}/"
    if args.mode == "kiosk":
        threading.Thread(target=_open_kiosk, args=(url,), daemon=True).start()
    elif args.mode == "web":
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()

    import uvicorn
    from src.server import app

    print(f"{config.APP_NAME} {config.APP_VERSION}  ->  {url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
