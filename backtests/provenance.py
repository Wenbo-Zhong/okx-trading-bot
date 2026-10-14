"""JSON-compatible provenance for offline backtest artifacts."""
from datetime import datetime, timezone
import subprocess

from okxbot import config


def provenance():
    def git(*args):
        try:
            return subprocess.check_output(
                ["git", *args], cwd=config.ROOT_DIR, text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "git_commit": commit or "unknown",
        "dirty": bool(status) if status is not None else None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "config": {
            name: value for name, value in vars(config).items()
            if name.isupper() and isinstance(value, (int, float, bool, str))
            and not any(token in name for token in ("KEY", "SECRET", "PASS"))
        },
    }
