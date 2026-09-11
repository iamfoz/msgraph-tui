"""Allow ``python -m msgraph_tui``."""

from .app.main import run

if __name__ == "__main__":
    raise SystemExit(run())
