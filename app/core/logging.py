import logging
import sys
from pythonjsonlogger.json import JsonFormatter
from app.core.config import settings

def setup_logging():
    """Configure structured JSON logging"""
    log_level = logging.DEBUG if settings.ENV_STATE == "dev" else logging.INFO
    
    logger = logging.getLogger()
    logger.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    
    formatter = JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        json_ensure_ascii=False
    )
    handler.setFormatter(formatter)
    
    # Remove existing handlers
    logger.handlers = []
    logger.addHandler(handler)

    # Set libraries to warning to reduce noise
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
