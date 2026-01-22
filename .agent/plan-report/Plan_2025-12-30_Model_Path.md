# Model Persistence Debugging Plan

## Goal
Ensure `model_artifacts` created inside the Docker container are persisted to the host and visible.

## Problem
- User reports no model file on the server (host).
- `docker-compose.yml` mounts `.:/app`.
- Models are saved to `model_artifacts/` (relative path).
- **Potential Issue 1: Path Ambiguity.** `PredictorService` uses `"model_artifacts/..."`. If the working directory of the worker is `/app`, this creates `/app/model_artifacts`.
- **Potential Issue 2: Permissions.** The container runs as root. Files created by root might not be visible or modifiable if the host has strict permissions, or vice versa.
- **Potential Issue 3: Directory Existence.** If `model_artifacts` doesn't exist on host, Docker might create it with root permissions, or Python might fail if it doesn't create directories recursively (though `os.makedirs` is used).

## Investigation Steps

### 1. Verify Working Directory
- Check `worker` container working directory (should be `/app` via `WORKDIR` in Dockerfile, but let's confirm).

### 2. Permissions & Write Test
- Run a simple write test inside the container to `/app/model_artifacts/test.txt` and check if it appears on host.
- If it works, the issue might be the *relative path* usage in `app/tasks/training.py`.

### 3. Path Correction
- Change `MODEL_SAVE_PATH` to behave consistently. It's currently in `app/tasks/training.py` as `"model_artifacts"`.
- User mentioned "ml inside you created it". Check if there's confusion about `app/ml/model_artifacts` vs `model_artifacts` in root.

## Proposed Fixes
1.  **Explicit Path**: Use absolute path `/app/model_artifacts` to be safe.
2.  **Permissions**: If strict permission issues exist, adding `user: "${UID}:${GID}"` to docker-compose might be needed, or `chmod 777` as a quick fix (user suggested).
3.  **Code Update**: Ensure `MODEL_SAVE_PATH` is consistent across formatted strings and logic.

## Verification
- Create dummy file in container -> Check on host.
- Run `chmod 777 model_artifacts` on host (if it exists) to rule out permission issues.
