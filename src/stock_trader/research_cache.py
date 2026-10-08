"""Validate a completed immutable research collection before creating an API client."""

import json

from stock_trader.modeling import checksum


def completed_collection(directory, contract):
    path = directory / "collection.json"
    if not path.exists():
        return None
    manifest = directory / "manifest.json"
    if not manifest.is_file() or json.loads(manifest.read_text()) != contract:
        raise ValueError("completed research cache manifest mismatch")
    result = json.loads(path.read_text())
    if result.get("contract") != contract or not result.get("file_sha256"):
        raise ValueError("completed research cache contract mismatch")
    for name, expected in result["file_sha256"].items():
        source = directory / name
        if (
            source.is_symlink()
            or not source.resolve().is_relative_to(directory.resolve())
            or not source.is_file()
            or checksum(source) != expected
        ):
            raise ValueError("completed research cache checksum mismatch")
    return result
