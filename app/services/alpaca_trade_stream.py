"""
Alpaca WebSocket 주문 업데이트 서비스
Phase E.1: WebSocket을 통한 실시간 주문 상태 추적

목적:
- 폴링을 이벤트 기반 업데이트로 대체
- API 호출 감소 (60회/분 제한 고려)
- 빠른 주문 확정 (<1초)
- 부분 체결, 거부, 취소를 실시간 처리
"""
import logging
from collections.abc import Callable
from datetime import datetime

from alpaca.trading.models import TradeUpdate
from alpaca.trading.stream import TradingStream

from app.core.config import settings
from app.core.database import get_sync_session
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)


class AlpacaTradeStream:
    """
    WebSocket client for real-time trade updates.
    
    Events Handled:
    - new: Order submitted to exchange
    - partial_fill: Order partially executed
    - fill: Order fully executed
    - canceled: Order canceled by user or system
    - rejected: Order rejected by exchange
    - pending_new: Order acknowledged by Alpaca
    - stopped, pending_cancel, expired: Other lifecycle events
    
    Benefits over Polling:
    - Instant notification (< 1s vs 15-30s polling delay)
    - Lower API usage (WebSocket persistent connection vs repeated GET requests)
    - Better for 15-minute trading (fast order confirmation critical)
    """

    def __init__(
        self,
        on_fill_callback: Callable | None = None,
        on_reject_callback: Callable | None = None,
        on_cancel_callback: Callable | None = None
    ):
        """
        Initialize WebSocket client.
        
        Args:
            on_fill_callback: Function called when order fully filled
            on_reject_callback: Function called when order rejected
            on_cancel_callback: Function called when order canceled
        """
        self.stream = TradingStream(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=settings.ALPACA_PAPER,
            raw_data=False  # Use parsed TradeUpdate objects
        )

        self.on_fill_callback = on_fill_callback
        self.on_reject_callback = on_reject_callback
        self.on_cancel_callback = on_cancel_callback

        # Subscribe to trade updates
        self.stream.subscribe_trade_updates(self._handle_trade_update)

        logger.info("Alpaca Trade Stream 초기화 완료")

    async def _handle_trade_update(self, data: TradeUpdate):
        """
        Main handler for all trade update events.
        
        Args:
            data: TradeUpdate object from Alpaca
                - order: Order object (symbol, qty, side, status, etc.)
                - event: Event type (new, fill, partial_fill, canceled, rejected, etc.)
                - timestamp: Event timestamp
                - position_qty: Current position quantity after event
        """
        try:
            order = data.order
            event = data.event
            symbol = order.symbol

            logger.info(
                f"[WEBSOCKET] {event.upper()}: {order.side} {order.qty} {symbol} "
                f"@ ${order.filled_avg_price or order.limit_price or 'MARKET'} "
                f"(상태: {order.status}, 주문ID: {order.id})"
            )

            # Handle different event types
            if event == 'fill':
                await self._handle_fill(data)
            elif event == 'partial_fill':
                await self._handle_partial_fill(data)
            elif event == 'rejected':
                await self._handle_rejection(data)
            elif event == 'canceled':
                await self._handle_cancellation(data)
            elif event == 'new':
                logger.info(f"주문 제출됨: {order.id}")
            elif event == 'pending_new':
                logger.debug(f"주문 대기중: {order.id}")
            else:
                logger.debug(f"처리되지 않은 이벤트: {event} (주문 {order.id})")

        except Exception as e:
            logger.error(f"거래 업데이트 처리 오류: {e}", exc_info=True)

    async def _handle_fill(self, data: TradeUpdate):
        """완전 체결된 주문 처리"""
        order = data.order
        position_qty = data.position_qty

        logger.info(
            f"주문 체결: {order.side} {order.filled_qty} {order.symbol} "
            f"@ ${order.filled_avg_price:.2f} | "
            f"현재 포지션: {position_qty} 주"
        )

        # Update position tracking in database
        with get_sync_session() as db:
            repo = SyncStockRepository(db)

            try:
                # Record in trade_logs table
                repo.record_trade(
                    symbol=order.symbol,
                    action=order.side.name,
                    quantity=int(order.filled_qty),
                    price=float(order.filled_avg_price),
                    order_id=order.id,
                    execution_time=datetime.now()
                )

                logger.info(f"거래가 DB에 기록됨: {order.id}")
            except Exception as e:
                logger.error(f"거래 기록 실패 {order.id}: {e}")

        # Call user callback if provided
        if self.on_fill_callback:
            try:
                await self.on_fill_callback(data)
            except Exception as e:
                logger.error(f"Fill 콜백 오류: {e}", exc_info=True)

    async def _handle_partial_fill(self, data: TradeUpdate):
        """부분 체결된 주문 처리"""
        order = data.order
        logger.warning(
            f"부분 체결: {order.filled_qty}/{order.qty} {order.symbol} "
            f"@ ${order.filled_avg_price:.2f} | "
            f"잔여: {float(order.qty) - float(order.filled_qty)}"
        )

        # Note: Position tracking updated when order fully fills
        # Partial fills don't trigger position closure checks

    async def _handle_rejection(self, data: TradeUpdate):
        """거부된 주문 처리"""
        order = data.order
        logger.error(
            f"주문 거부: {order.side} {order.qty} {order.symbol} | "
            f"사유: {order.status} | 주문ID: {order.id}"
        )

        # Call user callback if provided
        if self.on_reject_callback:
            try:
                await self.on_reject_callback(data)
            except Exception as e:
                logger.error(f"Reject 콜백 오류: {e}", exc_info=True)

    async def _handle_cancellation(self, data: TradeUpdate):
        """취소된 주문 처리"""
        order = data.order
        logger.warning(
            f"주문 취소: {order.side} {order.qty} {order.symbol} | "
            f"체결: {order.filled_qty}/{order.qty} | 주문ID: {order.id}"
        )

        # Call user callback if provided
        if self.on_cancel_callback:
            try:
                await self.on_cancel_callback(data)
            except Exception as e:
                logger.error(f"Cancel 콜백 오류: {e}", exc_info=True)

    def start(self):
        """
        Start WebSocket connection (blocking).
        
        This should be run in a separate thread/process or as an async task.
        For Celery workers, use a background thread.
        """
        try:
            logger.info("Alpaca Trade Stream 시작")
            self.stream.run()
        except KeyboardInterrupt:
            logger.info("사용자에 의해 Trade Stream 중단됨")
        except Exception as e:
            logger.error(f"Trade stream 오류: {e}", exc_info=True)
            raise

    def stop(self):
        """Stop WebSocket connection gracefully."""
        try:
            logger.info("Alpaca Trade Stream 중지 중...")
            self.stream.stop()
        except Exception as e:
            logger.error(f"Trade Stream 중지 오류: {e}")


# Singleton instance
_stream_instance: AlpacaTradeStream | None = None


def get_trade_stream(
    on_fill_callback: Callable | None = None,
    on_reject_callback: Callable | None = None,
    on_cancel_callback: Callable | None = None
) -> AlpacaTradeStream:
    """
    Get singleton trade stream instance.
    
    Usage:
        # In Celery worker startup
        stream = get_trade_stream(
            on_fill_callback=handle_fill,
            on_reject_callback=handle_reject
        )
        threading.Thread(target=stream.start, daemon=True).start()
    """
    global _stream_instance
    if _stream_instance is None:
        _stream_instance = AlpacaTradeStream(
            on_fill_callback=on_fill_callback,
            on_reject_callback=on_reject_callback,
            on_cancel_callback=on_cancel_callback
        )
    return _stream_instance
