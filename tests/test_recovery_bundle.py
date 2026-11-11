import copy
import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stock_trader import recovery_bundle as bundle


@pytest.fixture
def fixture(tmp_path):
    source, project, backups, output = (
        tmp_path / name for name in ("source", "project", "backups", "output")
    )
    for root in (
        [source / name for name in ("data/runtime", "data/research", "artifacts", "reports")]
        + [project / name for name in bundle.TREES]
        + [backups]
    ):
        root.mkdir(parents=True)
    for name in bundle.TOP:
        (project / name).write_text("reviewed source\n")
    (project / "src/app.py").write_text("# fixed source\n")
    (project / ".env").write_text("NEVER-IN-BUNDLE")
    (source / "data/research/guard.json").write_text('{"consumed":true}')
    (source / "data/research/interrupted.tmp").write_text("partial")
    live = source / "data/runtime/alpaca-budget.sqlite3"
    with sqlite3.connect(live) as db:
        db.execute(
            "CREATE TABLE budget(bucket TEXT PRIMARY KEY, attempts INTEGER, limited INTEGER, waits INTEGER)"
        )
        db.executemany(
            "INSERT INTO budget VALUES(?,?,?,?)",
            [("combined", 17, 1, 0), ("data", 10, 1, 3), ("trading", 7, 0, 2)],
        )
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = "paper-db-" + now + "-12345678.dump"
    raw = b"PGDMPtestfixture"
    (backups / name).write_bytes(raw)
    (backups / (name + ".json")).write_text(
        json.dumps(
            {
                "state": "complete",
                "file": name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "created_at": now,
            }
        )
    )
    return source, project, backups, output, {"engine": "sha256:" + "a" * 64}


def make(fixture):
    result = bundle.create(*fixture)
    return fixture[3] / result["bundle"], result


def test_complete_bundle_preserves_guard_and_budget_excludes_credentials_and_restores_offline(
    fixture, tmp_path
):
    path, result = make(fixture)
    assert result["state"] == "verified" and not result["offsite_copy"]
    assert (path / "data/research/guard.json").read_bytes() == (
        fixture[0] / "data/research/guard.json"
    ).read_bytes()
    assert not list(path.rglob(".env")) and not list(path.rglob("*.tmp"))
    assert bundle.budget_counters(path / "data/runtime/alpaca-budget.sqlite3")["combined"]["attempts"] == 17
    assert bundle.verify(path) == {k: v for k, v in result.items() if k != "bundle"}
    restored = tmp_path / "restored"
    check = bundle.restore(path, restored)
    assert check["restored_to_empty_directory"] and not check["production_database_replaced"]
    assert bundle.verify(restored)["manifest_sha256"] == result["manifest_sha256"]
    assert bundle.digest(restored / "data/runtime/alpaca-budget.sqlite3") == bundle.digest(
        path / "data/runtime/alpaca-budget.sqlite3"
    )
    assert not list(fixture[3].glob(".pending-*"))


@pytest.mark.parametrize("fault", ["alter", "extra", "missing", "symlink", "permission", "root_permission"])
def test_any_bundle_corruption_is_rejected(fixture, fault):
    path, _ = make(fixture)
    item = path / "data/research/guard.json"
    if fault == "alter":
        item.write_text("changed")
    elif fault == "extra":
        extra = path / "surprise.json"
        extra.write_text("extra")
        extra.chmod(0o600)
    elif fault == "missing":
        item.unlink()
    elif fault == "symlink":
        item.unlink()
        item.symlink_to(fixture[0] / "data/research/guard.json")
    elif fault == "permission":
        item.chmod(0o644)
    else:
        path.chmod(0o755)
    with pytest.raises(ValueError):
        bundle.verify(path)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "a/.env", "a/.secrets/key", "a/client.pem"],
)
def test_manifest_path_and_secret_rejection(name):
    with pytest.raises(ValueError):
        bundle.safe_name(name)


def test_invalid_backup_leaves_no_complete_or_pending_bundle(fixture):
    next(fixture[2].glob("*.dump")).write_bytes(b"wrong")
    with pytest.raises(ValueError, match="digest"):
        bundle.create(*fixture)
    assert [p.name for p in fixture[3].iterdir()] == [".bundle.lock"]


def test_stale_backup_is_not_exported_as_current(fixture):
    now = datetime.now(UTC) + timedelta(hours=31)
    with pytest.raises(ValueError, match="stale"):
        bundle.latest_backup(fixture[2], now)


def test_online_wal_snapshot_preserves_committed_counters_and_never_mutates_live(fixture):
    live = fixture[0] / "data/runtime/alpaca-budget.sqlite3"
    with sqlite3.connect(live) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE budget SET attempts=attempts+1 WHERE bucket IN ('combined','trading')")
        db.commit()
        before = db.execute("SELECT * FROM budget ORDER BY bucket").fetchall()
        path, _ = make(fixture)
        assert (
            bundle.budget_counters(path / "data/runtime/alpaca-budget.sqlite3")["combined"]["attempts"] == 18
        )
        assert db.execute("SELECT * FROM budget ORDER BY bucket").fetchall() == before
        assert not list(path.rglob("*-wal")) and not list(path.rglob("*-shm"))


def test_inconsistent_budget_counts_fail_closed(fixture):
    with sqlite3.connect(fixture[0] / "data/runtime/alpaca-budget.sqlite3") as db:
        db.execute("UPDATE budget SET attempts=0 WHERE bucket='combined'")
    with pytest.raises(ValueError, match="sum"):
        bundle.create(*fixture)
    assert not list(fixture[3].glob("recovery-*"))


def test_link_and_secret_in_data_are_not_silently_exported(fixture):
    secret = fixture[0] / "data/research/.env"
    secret.write_text("NEVER-IN-BUNDLE")
    with pytest.raises(ValueError, match="credential"):
        bundle.create(*fixture)
    secret.unlink()
    secret.symlink_to(fixture[1] / ".env")
    with pytest.raises(ValueError, match="link"):
        bundle.create(*fixture)


def test_duplicate_export_is_excluded_and_existing_restore_target_is_untouched(fixture, tmp_path):
    path, _ = make(fixture)
    descriptor = os.open(fixture[3] / ".bundle.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            bundle.create(*fixture)
    finally:
        os.close(descriptor)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "marker").write_text("preserve")
    with pytest.raises(ValueError, match="must not exist"):
        bundle.restore(path, target)
    assert (target / "marker").read_text() == "preserve"


def test_pending_or_nested_output_is_not_accepted(fixture):
    nested = list(fixture)
    nested[3] = fixture[0] / "data/output"
    with pytest.raises(ValueError, match="separate"):
        bundle.create(*nested)
    path, _ = make(fixture)
    pending = path.with_name(".pending-test")
    path.rename(pending)
    with pytest.raises(ValueError, match="completed"):
        bundle.verify(pending)


def test_source_mutation_during_copy_never_publishes_bundle(fixture, monkeypatch):
    copy_function = bundle.shutil.copyfileobj

    def mutate(incoming, outgoing, *args):
        copy_function(incoming, outgoing, *args)
        Path(incoming.name).write_bytes(b"mutated-after-copy")

    monkeypatch.setattr(bundle.shutil, "copyfileobj", mutate)
    with pytest.raises(ValueError, match="changed during"):
        bundle.create(*fixture)
    assert not list(fixture[3].glob("recovery-*"))


def test_self_consistent_manifest_cannot_smuggle_parent_path(fixture):
    path, _ = make(fixture)
    envelope = json.loads((path / "manifest.json").read_text())
    meta = copy.deepcopy(envelope["payload"])
    meta["files"]["../outside"] = {"sha256": "a" * 64, "bytes": 0}
    (path / "manifest.json").write_text(json.dumps({"payload": meta, "sha256": bundle.fingerprint(meta)}))
    with pytest.raises(ValueError):
        bundle.verify(path)
