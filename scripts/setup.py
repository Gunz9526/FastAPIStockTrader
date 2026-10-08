"""Create local secrets without printing their values; preserve every existing file."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / ".secrets"
directory.mkdir(mode=0o700, exist_ok=True)
os.chmod(directory, 0o700)
for name in ("db_password", "api_token"):
    path = directory / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(36))
env = root / ".env"
if not env.exists():
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write((root / ".env.example").read_text())
for name in ("data", "artifacts", "reports", "verification"):
    (root / name).mkdir(exist_ok=True)
print("Local configuration prepared. Existing keys and settings were preserved.")
