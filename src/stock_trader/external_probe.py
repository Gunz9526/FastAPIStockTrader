"""ADR-0081: stdlib-only, one-shot SSH monitor for a separate host; no secrets copied."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ssh_command(target, project):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", target):
        raise ValueError("expected SSH user@hostname without options")
    path = PurePosixPath(project)
    if not path.is_absolute() or any(c in project for c in "\r\n\0"):
        raise ValueError("expected absolute remote project path")
    remote = (
        "docker compose -f "
        + shlex.quote(str(path / "compose.yaml"))
        + " exec -T api python -m stock_trader.health"
    )
    return [
        "ssh",
        "-n",
        "-T",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oServerAliveInterval=5",
        "-oServerAliveCountMax=1",
        target,
        remote,
    ]


def check(target, project, *, now=None, execute=subprocess.run):
    now = now or datetime.now(UTC)
    command = ssh_command(target, project)
    result = {"state": "unavailable", "issues": ["ssh_unreachable"]}
    try:
        completed = execute(command, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode not in {0, 1} or len(completed.stdout) > 65536:
            return result
        data = json.loads(completed.stdout)
        state, issues = data["state"], data["issues"]
        if state == "unavailable":
            return {"state": "unavailable", "issues": ["remote_probe_unavailable"]}
        observed = datetime.fromisoformat(data["observed_at"])
        if observed.tzinfo is None or not -30 <= (now - observed).total_seconds() <= 120:
            return {"state": "unavailable", "issues": ["remote_observation_stale"]}
        if (
            data["schema_version"] != 1
            or data["paper_only"] is not True
            or state not in {"healthy", "warning", "blocked"}
            or not isinstance(issues, list)
            or len(issues) > 32
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z_]{1,64}", v) for v in issues)
            or completed.returncode != (1 if state == "blocked" else 0)
            or (state == "healthy") != (not issues)
        ):
            return {"state": "unavailable", "issues": ["remote_response_invalid"]}
        return {"state": state, "issues": sorted(issues)}
    except subprocess.TimeoutExpired, OSError:
        return result
    except ValueError, KeyError, TypeError:
        return {"state": "unavailable", "issues": ["remote_response_invalid"]}


def _record(path, target, project, result, now):
    now = now or datetime.now(UTC)
    path = Path(path)
    config = digest({"target": target, "project": project})
    if path.is_symlink():
        raise ValueError("state symlink rejected")
    previous = json.loads(path.read_text()) if path.exists() else None
    if previous and previous["config_sha256"] != config:
        raise ValueError("use a separate state file for a different target")
    if previous and datetime.fromisoformat(previous["checked_at"]) > now:
        return None
    changed = not previous or previous["result"] != result
    event = changed and (result["state"] != "healthy" or previous is not None)
    value = {
        "config_sha256": config,
        "checked_at": now.isoformat(),
        "result": result,
        "transition": (previous["transition"] if previous else 0) + int(event),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return (
        {"event": "external_health_changed", "transition": value["transition"], **result} if event else None
    )


def record(path, target, project, result, *, now=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return _record(path, target, project, result, now or datetime.now(UTC))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--project", default="/home/ubuntu/stock-trader")
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    started = datetime.now(UTC)
    result = check(args.target, args.project, now=started)
    event = record(args.state, args.target, args.project, result, now=started)
    if event:
        print(json.dumps(event))
    raise SystemExit(0 if result["state"] in {"healthy", "warning"} else 1)


if __name__ == "__main__":
    main()
