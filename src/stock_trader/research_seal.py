"""Small immutable research cache; no model, broker or configuration imports."""

import hashlib
import json
import os
import uuid


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read(path):
    if path.is_symlink():
        raise ValueError("research cache link")
    envelope = json.loads(path.read_text())
    if envelope["sha256"] != fingerprint(envelope["payload"]):
        raise ValueError("research cache checksum")
    return envelope["payload"]


def seal(path, payload):
    # All production calls are fenced by the same PostgreSQL leader connection.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if read(path) != payload:
            raise ValueError("immutable research cache changed")
        return
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"payload": payload, "sha256": fingerprint(payload)}, stream, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Do not silently overwrite an unexpectedly concurrent commit.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
