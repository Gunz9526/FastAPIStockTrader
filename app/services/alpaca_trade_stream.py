"""
Alpaca WebSocket Trade Updates Service
Phase E.1: Real-time order status tracking via WebSocket

Purpose:
- Replace polling with event-driven order updates
- Reduce API calls (60/minute limit)
- Faster order confirmation (<1 second vs 15-30 seconds)
- Handle partial fills, rejections, cancellations in real-time
"""
import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime

from alpaca.trading.stream import TradingStream
from alpaca.trading.models import TradeUpdate
from alpaca.trading.enums import OrderStatus

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
        on_fill_callback: Optional[Callable] = None,
        on_reject_callback: Optional[Callable] = None,
        on_cancel_callback: Optional[Callable] = None
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
        
        logger.info("Alpaca Trade Stream initialized")
    
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
                f"(Status: {order.status}, Order ID: {order.id})"
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
                logger.info(f"✓ Order {order.id} submitted successfully")
            elif event == 'pending_new':
                logger.debug(f"Order {order.id} pending on exchange")
            else:
                logger.debug(f"Unhandled event: {event} for order {order.id}")
        
        except Exception as e:
            logger.error(f"Error handling trade update: {e}", exc_info=True)
    
    async def _handle_fill(self, data: TradeUpdate):
        """Handle fully filled orders."""
        order = data.order
        position_qty = data.position_qty
        
        logger.info(
            f"✓ ORDER FILLED: {order.side} {order.filled_qty} {order.symbol} "
            f"@ ${order.filled_avg_price:.2f} | "
            f"Position now: {position_qty} shares"
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
                
                logger.info(f"Trade recorded in database: {order.id}")
            except Exception as e:
                logger.error(f"Failed to record trade {order.id}: {e}")
        
        # Call user callback if provided
        if self.on_fill_callback:
            try:
                await self.on_fill_callback(data)
            except Exception as e:
                logger.error(f"Fill callback error: {e}", exc_info=True)
    
    async def _handle_partial_fill(self, data: TradeUpdate):
        """Handle partially filled orders."""
        order = data.order
        
        logger.warning(
            f"⚠ PARTIAL FILL: {order.filled_qty}/{order.qty} {order.symbol} "
            f"@ ${order.filled_avg_price:.2f} | "
            f"Remaining: {float(order.qty) - float(order.filled_qty)}"
        )
        
        # Note: Position tracking updated when order fully fills
        # Partial fills don't trigger position closure checks
    
    async def _handle_rejection(self, data: TradeUpdate):
        """Handle rejected orders."""
        order = data.order
        
        logger.error(
            f"✗ ORDER REJECTED: {order.side} {order.qty} {order.symbol} | "
            f"Reason: {order.status} | "
            f"Order ID: {order.id}"
        )
        
        # Call user callback if provided
        if self.on_reject_callback:
            try:
                await self.on_reject_callback(data)
            except Exception as e:
                logger.error(f"Reject callback error: {e}", exc_info=True)
    
    async def _handle_cancellation(self, data: TradeUpdate):
        """Handle canceled orders."""
        order = data.order
        
        logger.warning(
            f"⚠ ORDER CANCELED: {order.side} {order.qty} {order.symbol} | "
            f"Filled: {order.filled_qty}/{order.qty} | "
            f"Order ID: {order.id}"
        )
        
        # Call user callback if provided
        if self.on_cancel_callback:
            try:
                await self.on_cancel_callback(data)
            except Exception as e:
                logger.error(f"Cancel callback error: {e}", exc_info=True)
    
    def start(self):
        """
        Start WebSocket connection (blocking).
        
        This should be run in a separate thread/process or as an async task.
        For Celery workers, use a background thread.
        """
        try:
            logger.info("Starting Alpaca Trade Stream...")
            self.stream.run()
        except KeyboardInterrupt:
            logger.info("Trade stream stopped by user")
        except Exception as e:
            logger.error(f"Trade stream error: {e}", exc_info=True)
            raise
    
    def stop(self):
        """Stop WebSocket connection gracefully."""
        try:
            logger.info("Stopping Alpaca Trade Stream...")
            self.stream.stop()
        except Exception as e:
            logger.error(f"Error stopping trade stream: {e}")


# Singleton instance
_stream_instance: Optional[AlpacaTradeStream] = None


def get_trade_stream(
    on_fill_callback: Optional[Callable] = None,
    on_reject_callback: Optional[Callable] = None,
    on_cancel_callback: Optional[Callable] = None
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
