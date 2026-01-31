import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

class TradeDecisionLogger:
    """
    RAG용으로 거래 의사결정 로그를 JSON 파일로 저장합니다.
    형식: logs/trade_decisions/{date}/{symbol}_{uuid}.json
    """

    def __init__(self, base_dir: str = "logs/trade_decisions"):
        self.base_dir = base_dir
        self._ensure_dir(self.base_dir)

    def _ensure_dir(self, path: str):
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)

    def log_decision(self, symbol: str, action: str, reason: str, metrics: dict[str, Any]):
        """
        Saves a decision log.
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            log_dir = os.path.join(self.base_dir, today)
            self._ensure_dir(log_dir)

            log_entry = {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "action": action, # BUY, SELL, HOLD
                "reason": reason,
                "metrics": metrics # RSI, Prediction Score, etc.
            }

            filename = f"{symbol}_{log_entry['id']}.json"
            filepath = os.path.join(log_dir, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(log_entry, f, indent=4, ensure_ascii=False)

            logger.info(f"{symbol} 거래 결정 로그 저장: {filepath}")

        except Exception as e:
            logger.error(f"거래 결정 로그 저장 실패: {e}")
