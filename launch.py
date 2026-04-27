import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SERVER_FILE = BASE_DIR / "server.py"


def main():
    if not SERVER_FILE.exists():
        print("server.py not found. Run this script inside the study-sprint folder.")
        sys.exit(1)

    env = os.environ.copy()
    port = int(env.get("PORT", "8010"))
    app_url = f"http://127.0.0.1:{port}/mvp-study-sprint.html"
    server_proc = subprocess.Popen(
        [sys.executable, str(SERVER_FILE)],
        cwd=str(BASE_DIR),
        env=env,
    )

    try:
        time.sleep(1.2)
        webbrowser.open(app_url)
        print(f"Opened {app_url}")
        print("Closing this window will also stop the local server.")
        server_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_proc.kill()


if __name__ == "__main__":
    main()
