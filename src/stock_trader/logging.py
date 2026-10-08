import json
import logging


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP libraries include complete request URLs, including webhook tokens.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def event(kind: str, **fields) -> None:
    logging.info(json.dumps({"event": kind, **fields}, ensure_ascii=False, default=str))
