import sys
import uvicorn
from .config import settings

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        uvicorn.run("polydesk.api:app", host="127.0.0.1", port=settings.dashboard_port, log_level="warning")
    else:
        print("usage: python -m polydesk run")
