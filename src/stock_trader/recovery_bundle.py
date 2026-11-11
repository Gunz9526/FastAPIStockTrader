"""ADR-0083: credential-free mounts, offline recovery staging; never installs state."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

VERSION = 1
TOP = (
    "AGENTS.md",
    "README.md",
    "DEVELOPMENT_PLAN.md",
    "Dockerfile",
    "Dockerfile.backup",
    "compose.yaml",
    "pyproject.toml",
    "uv.lock",
    "requirements.lock",
    ".python-version",
    ".dockerignore",
    ".gitignore",
)
TREES = ("src", "tests", "scripts", "docs")
DUMP_NAME = re.compile(r"paper-db-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}\.dump")
CONTRACT = {
    "decision": "ADR-0083",
    "schema_version": VERSION,
    "broker_requests": 0,
    "production_restore": False,
    "includes_credentials": False,
    "includes_docker_images": False,
    "encrypted": False,
    "offsite_copy": False,
    "global_atomic_snapshot": False,
    "reconciliation_before_resume_required": True,
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def private_directory(path):
    if path.is_symlink():
        raise ValueError("directory link rejected")
    missing = []
    parent = path
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError("directory link rejected")
        missing.append(parent)
        parent = parent.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700)
    if not path.is_dir():
        raise ValueError("directory required")
    path.chmod(0o700)


def safe_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or str(path) != name
        or path.is_absolute()
        or any(p in {"", ".", ".."} for p in path.parts)
        or "\\" in name
    ):
        raise ValueError("unsafe relative path")
    if any(
        p.lower() in {".env", ".secrets", "secrets"}
        or p.lower().endswith((".pem", ".key"))
        or p.lower().startswith(".env.")
        for p in path.parts
    ):
        raise ValueError("credential file rejected")
    return path


def regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("regular file required")


def stable_copy(source, target):
    regular(source)
    before = source.stat()
    private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
        if identity(os.fstat(incoming.fileno())) != identity(before):
            raise ValueError("source changed before copy")
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    after = source.stat()
    if identity(before) != identity(after) or digest(source) != digest(target):
        raise ValueError("source changed during copy")


def identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def scan(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source directory required")
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            raise ValueError("source link rejected")
        if path.name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
            continue
        if path.name.endswith((".tmp", ".pyc", "-wal", "-shm", "-journal")):
            continue
        if path.name.endswith(".lock") and path.name not in {"uv.lock", "requirements.lock"}:
            continue
        if path.is_dir():
            yield from scan(path)
        else:
            regular(path)
            yield path


def budget_counters(path):
    regular(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("invalid budget integrity")
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() != [
            ("budget",)
        ]:
            raise ValueError("unexpected budget schema")
        rows = db.execute("SELECT bucket,attempts,limited,waits FROM budget ORDER BY bucket").fetchall()
    if len(rows) != 3 or {r[0] for r in rows} != {"combined", "data", "trading"}:
        raise ValueError("missing budget buckets")
    if any(type(v) is not int or v < 0 for row in rows for v in row[1:]):
        raise ValueError("invalid budget counters")
    result = {r[0]: dict(zip(("attempts", "http_429", "waits"), r[1:], strict=True)) for r in rows}
    for key in ("attempts", "http_429"):
        if result["combined"][key] != result["data"][key] + result["trading"][key]:
            raise ValueError("budget counter sum mismatch")
    return result


def snapshot_budget(source, target):
    regular(source)
    private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + 45

    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError("budget snapshot deadline")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as live:
        with closing(sqlite3.connect(target)) as copy:
            live.backup(copy, pages=64, progress=progress, sleep=0.05)
            # A WAL source also copies the persistent journal mode. Normalize
            # only the isolated snapshot so later read-only checks need no WAL.
            if copy.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise ValueError("budget snapshot journal mode")
    return budget_counters(target)


def latest_backup(root, now):
    candidates = sorted(root.glob("paper-db-*.dump.json"))
    if not candidates:
        raise ValueError("no completed backup")
    manifest = candidates[-1]
    regular(manifest)
    meta = json.loads(manifest.read_text())
    name = meta.get("file", "")
    if not DUMP_NAME.fullmatch(name) or manifest.name != name + ".json" or meta.get("state") != "complete":
        raise ValueError("invalid backup manifest")
    stamp = datetime.strptime(meta["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if not timedelta(0) <= now - stamp <= timedelta(hours=30) or stamp.strftime("%Y%m%dT%H%M%SZ") not in name:
        raise ValueError("backup stale or future")
    dump = root / name
    regular(dump)
    if dump.stat().st_size != meta["bytes"] or digest(dump) != meta["sha256"]:
        raise ValueError("backup digest mismatch")
    with dump.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise ValueError("not a custom PostgreSQL dump")
    return dump, manifest, meta


def validate_inventory(inventory):
    if not isinstance(inventory, dict) or not inventory or len(inventory) > 20:
        raise ValueError("invalid image inventory")
    for role, image in inventory.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", role)
            or not isinstance(image, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        ):
            raise ValueError("invalid image identity")
    return inventory


def write_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify(bundle, *, _staging=False):
    if bundle.is_symlink() or not bundle.is_dir() or (bundle.name.startswith(".pending-") and not _staging):
        raise ValueError("completed bundle required")
    if stat.S_IMODE(bundle.stat().st_mode) != 0o700:
        raise ValueError("unsafe bundle root permission")
    envelope_path = bundle / "manifest.json"
    regular(envelope_path)
    envelope = json.loads(envelope_path.read_text())
    meta = envelope["payload"]
    if envelope["sha256"] != fingerprint(meta) or meta.get("contract") != CONTRACT:
        raise ValueError("bundle manifest mismatch")
    expected = meta["files"]
    if not isinstance(expected, dict) or not expected:
        raise ValueError("empty bundle")
    actual = set()
    for directory, dirs, files in os.walk(bundle, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or stat.S_IMODE(path.lstat().st_mode) != (0o700 if path.is_dir() else 0o600):
                raise ValueError("unsafe bundle permission or link")
        for name in files:
            path = Path(directory) / name
            regular(path)
            relative = path.relative_to(bundle).as_posix()
            safe_name(relative)
            actual.add(relative)
    if actual != set(expected) | {"manifest.json"}:
        raise ValueError("bundle file set mismatch")
    for name, entry in expected.items():
        safe_name(name)
        path = bundle / name
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("bundle file digest mismatch")
    dump, _, backup = latest_backup(bundle / "postgres", datetime.fromisoformat(meta["started_at"]))
    if backup != meta["database_backup"] or str(dump.relative_to(bundle)) not in expected:
        raise ValueError("bundle database identity mismatch")
    counters = budget_counters(bundle / "data/runtime/alpaca-budget.sqlite3")
    if counters != meta["sqlite_counters"]:
        raise ValueError("bundle budget identity mismatch")
    validate_inventory(meta["image_ids"])
    return {
        "state": "verified",
        "files": len(expected),
        "bytes": sum(x["bytes"] for x in expected.values()),
        "manifest_sha256": envelope["sha256"],
        "broker_requests": 0,
        "offsite_copy": False,
    }


def create(source, project, backups, output, inventory):
    source, project, backups, output = (p.absolute() for p in (source, project, backups, output))
    for root in (source, project, backups):
        if output.resolve().is_relative_to(root.resolve()) or root.is_symlink():
            raise ValueError("output must be separate from source")
    validate_inventory(inventory)
    private_directory(output)
    lock_fd = os.open(output / ".bundle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = datetime.now(UTC)
        identity = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        stage = output / (".pending-" + identity)
        final = output / ("recovery-" + identity)
        stage.mkdir(mode=0o700)
        try:
            dump, manifest, backup = latest_backup(backups, started)
            for path in (dump, manifest):
                stable_copy(path, stage / "postgres" / path.name)
            for tree in ("data", "artifacts", "reports"):
                for path in scan(source / tree):
                    relative = path.relative_to(source).as_posix()
                    safe_name(relative)
                    if relative.startswith("data/runtime/"):
                        if relative != "data/runtime/alpaca-budget.sqlite3":
                            raise ValueError("unreviewed runtime state")
                        continue
                    stable_copy(path, stage / relative)
            budget_time = datetime.now(UTC).isoformat()
            counters = snapshot_budget(
                source / "data/runtime/alpaca-budget.sqlite3", stage / "data/runtime/alpaca-budget.sqlite3"
            )
            for name in TOP:
                stable_copy(project / name, stage / "project" / name)
            for tree in TREES:
                for path in scan(project / tree):
                    relative = path.relative_to(project).as_posix()
                    safe_name(relative)
                    stable_copy(path, stage / "project" / relative)
            entries = {
                p.relative_to(stage).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
                for p in scan(stage)
            }
            meta = {
                "contract": CONTRACT,
                "started_at": started.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "sqlite_observed_at": budget_time,
                "sqlite_counters": counters,
                "database_backup": backup,
                "image_ids": inventory,
                "files": entries,
            }
            write_json(stage / "manifest.json", {"payload": meta, "sha256": fingerprint(meta)})
            # Verification ignores the pending name only internally; no public
            # completion is visible until the validated directory is renamed.
            result = verify(stage, _staging=True)
            for directory, _, _ in os.walk(stage, topdown=False):
                sync_directory(Path(directory))
            os.rename(stage, final)
            sync_directory(output)
            return {**result, "bundle": final.name}
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def restore(bundle, destination):
    # Staging only: no SQL, no Docker socket and no production target mounts.
    result = verify(bundle)
    if destination.exists() or destination.is_symlink():
        raise ValueError("restore target must not exist")
    destination.mkdir(mode=0o700)
    try:
        for path in scan(bundle):
            stable_copy(path, destination / path.relative_to(bundle))
        if verify(destination)["manifest_sha256"] != result["manifest_sha256"]:
            raise ValueError("restore verification mismatch")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {**result, "restored_to_empty_directory": True, "production_database_replaced": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("create")
    build.add_argument("--source", type=Path, default=Path("/source"))
    build.add_argument("--project", type=Path, default=Path("/app/project"))
    build.add_argument("--backups", type=Path, default=Path("/backups"))
    build.add_argument("--output", type=Path, default=Path("/recovery-output"))
    build.add_argument("--inventory", type=Path, default=Path("/inventory.json"))
    for name in ("verify", "restore"):
        command = sub.add_parser(name)
        command.add_argument("bundle", type=Path)
        if name == "restore":
            command.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create(
                args.source, args.project, args.backups, args.output, json.loads(args.inventory.read_text())
            )
        elif args.command == "verify":
            result = verify(args.bundle)
        else:
            result = restore(args.bundle, args.destination)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
