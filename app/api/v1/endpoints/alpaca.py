import logging
from typing import Any

from alpaca.trading.client import TradingClient
from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.core.security import get_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

def get_alpaca_client() -> TradingClient:
    """Alpaca Trading Client 초기화"""
    try:
        is_paper = 'paper' in settings.ALPACA_TRADING_URL.lower()
        return TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=is_paper
        )
    except Exception as e:
        logger.error(f"Alpaca 클라이언트 초기화 실패: {e}")
        raise HTTPException(status_code=500, detail="Alpaca API 연결 실패") from e


@router.get("/account")
async def get_account_info(api_key: str = Depends(get_api_key)) -> dict[str, Any]:
    """
    Alpaca 계좌 정보 조회
    
    반환 정보:
    - buying_power: 매수 가능 금액
    - cash: 현금 잔고
    - portfolio_value: 총 포트폴리오 가치
    - equity: 자산 총액
    - long_market_value: 롱 포지션 시장가
    - short_market_value: 숏 포지션 시장가
    - pattern_day_trader: PDT 여부
    - trading_blocked: 거래 차단 여부
    - account_blocked: 계좌 차단 여부
    - last_equity: 전일 자산 총액
    - initial_margin: 초기 증거금
    - maintenance_margin: 유지 증거금
    """
    try:
        client = get_alpaca_client()
        account = client.get_account()

        return {
            "account_number": account.account_number,
            "status": account.status,
            "currency": account.currency,
            "buying_power": float(account.buying_power),
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "equity": float(account.equity),
            "long_market_value": float(account.long_market_value),
            "short_market_value": float(account.short_market_value),
            "pattern_day_trader": account.pattern_day_trader,
            "trading_blocked": account.trading_blocked,
            "account_blocked": account.account_blocked,
            "transfers_blocked": account.transfers_blocked,
            "last_equity": float(account.last_equity),
            "multiplier": account.multiplier,
            "daytrade_count": account.daytrade_count,
            "daytrading_buying_power": float(account.daytrading_buying_power),
            "regt_buying_power": float(account.regt_buying_power),
            "initial_margin": float(account.initial_margin) if account.initial_margin else 0.0,
            "maintenance_margin": float(account.maintenance_margin) if account.maintenance_margin else 0.0,
        }
    except Exception as e:
        logger.error(f"계좌 정보 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/positions")
async def get_all_positions(api_key: str = Depends(get_api_key)) -> list[dict[str, Any]]:
    """
    모든 활성 포지션 조회
    
    반환 정보 (각 포지션):
    - symbol: 종목 심볼
    - qty: 보유 수량
    - avg_entry_price: 평균 진입 가격
    - current_price: 현재 가격
    - market_value: 시장가치
    - unrealized_pl: 미실현 손익 (금액)
    - unrealized_plpc: 미실현 손익 (%)
    - cost_basis: 총 매입 비용
    - side: long/short
    """
    try:
        client = get_alpaca_client()
        positions = client.get_all_positions()

        result = []
        for pos in positions:
            result.append({
                "symbol": pos.symbol,
                "asset_id": pos.asset_id,
                "exchange": pos.exchange,
                "asset_class": pos.asset_class,
                "qty": int(pos.qty),
                "qty_available": int(pos.qty_available) if pos.qty_available else int(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "lastday_price": float(pos.lastday_price),
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "unrealized_intraday_pl": float(pos.unrealized_intraday_pl),
                "unrealized_intraday_plpc": float(pos.unrealized_intraday_plpc),
                "change_today": float(pos.change_today),
                "side": pos.side,
            })

        return result
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/positions/{symbol}")
async def get_position_by_symbol(
    symbol: str,
    api_key: str = Depends(get_api_key)
) -> dict[str, Any]:
    """
    특정 종목의 포지션 조회
    
    Args:
        symbol: 종목 심볼 (예: AAPL, MSFT)
    
    Returns:
        포지션 상세 정보 (존재하지 않으면 404 에러)
    """
    try:
        client = get_alpaca_client()
        pos = client.get_open_position(symbol)

        return {
            "symbol": pos.symbol,
            "asset_id": pos.asset_id,
            "exchange": pos.exchange,
            "asset_class": pos.asset_class,
            "qty": int(pos.qty),
            "qty_available": int(pos.qty_available) if pos.qty_available else int(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "lastday_price": float(pos.lastday_price),
            "market_value": float(pos.market_value),
            "cost_basis": float(pos.cost_basis),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc),
            "unrealized_intraday_pl": float(pos.unrealized_intraday_pl),
            "unrealized_intraday_plpc": float(pos.unrealized_intraday_plpc),
            "change_today": float(pos.change_today),
            "side": pos.side,
        }
    except Exception as e:
        if "position does not exist" in str(e).lower():
            raise HTTPException(
                status_code=404, detail=f"{symbol} 포지션이 존재하지 않습니다"
            )
        logger.error(f"{symbol} 포지션 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/portfolio/history")
async def get_portfolio_history(
    period: str = "1M",
    timeframe: str = "1D",
    api_key: str = Depends(get_api_key)
) -> dict[str, Any]:
    """
    포트폴리오 히스토리 조회 (시계열 데이터)
    
    Args:
        period: 조회 기간 (1D, 1W, 1M, 3M, 1A, all)
        timeframe: 시간 간격 (1Min, 5Min, 15Min, 1H, 1D)
    
    Returns:
        - timestamp: 타임스탬프 배열
        - equity: 자산 총액 배열
        - profit_loss: 손익 배열
        - profit_loss_pct: 손익률 배열
    """
    try:
        client = get_alpaca_client()
        history = client.get_portfolio_history(
            period=period,
            timeframe=timeframe
        )

        return {
            "timestamp": history.timestamp,
            "equity": [float(e) for e in history.equity],
            "profit_loss": [float(p) for p in history.profit_loss],
            "profit_loss_pct": [float(p) for p in history.profit_loss_pct],
            "base_value": float(history.base_value),
            "timeframe": history.timeframe,
        }
    except Exception as e:
        logger.error(f"포트폴리오 히스토리 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/orders")
async def get_orders(
    status: str | None = "all",
    limit: int = 50,
    api_key: str = Depends(get_api_key)
) -> list[dict[str, Any]]:
    """
    주문 내역 조회
    
    Args:
        status: 주문 상태 필터 (open, closed, all)
        limit: 최대 조회 개수 (기본 50)
    
    Returns:
        주문 목록
    """
    try:
        client = get_alpaca_client()

        from alpaca.trading.enums import QueryOrderStatus
        if status == "open":
            order_status = QueryOrderStatus.OPEN
        elif status == "closed":
            order_status = QueryOrderStatus.CLOSED
        else:
            order_status = QueryOrderStatus.ALL

        from alpaca.trading.requests import GetOrdersRequest
        request = GetOrdersRequest(
            status=order_status,
            limit=limit
        )
        orders = client.get_orders(filter=request)

        result = []
        for order in orders:
            result.append({
                "id": order.id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "asset_class": order.asset_class,
                "qty": float(order.qty) if order.qty else None,
                "filled_qty": float(order.filled_qty) if order.filled_qty else 0.0,
                "type": order.type,
                "side": order.side,
                "time_in_force": order.time_in_force,
                "status": order.status,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "updated_at": order.updated_at.isoformat() if order.updated_at else None,
                "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                "limit_price": float(order.limit_price) if order.limit_price else None,
                "stop_price": float(order.stop_price) if order.stop_price else None,
            })

        return result
    except Exception as e:
        logger.error(f"주문 내역 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/activities")
async def get_activities(
    activity_type: str | None = None,
    limit: int = 50,
    api_key: str = Depends(get_api_key)
) -> list[dict[str, Any]]:
    """
    계좌 활동 내역 조회 (거래, 입출금 등)
    
    Args:
        activity_type: 활동 타입 필터 (FILL, CSD, DIV 등)
        limit: 최대 조회 개수
    
    Returns:
        활동 내역 목록
    """
    try:
        client = get_alpaca_client()

        from alpaca.trading.enums import ActivityType
        from alpaca.trading.requests import GetAccountActivitiesRequest

        activity_types = None
        if activity_type:
            try:
                activity_types = [ActivityType[activity_type.upper()]]
            except KeyError:
                raise HTTPException(
                    status_code=400,
                    detail=f"잘못된 activity_type: {activity_type}"
                )

        request = GetAccountActivitiesRequest(
            activity_types=activity_types
        )
        activities = client.get_activities(filter=request)

        result = []
        for activity in activities[:limit]:
            activity_dict = {
                "id": activity.id,
                "activity_type": activity.activity_type,
                "date": activity.date.isoformat() if activity.date else None,
            }

            # FILL 타입인 경우 추가 정보
            if hasattr(activity, 'symbol'):
                activity_dict.update({
                    "symbol": activity.symbol,
                    "side": activity.side if hasattr(activity, 'side') else None,
                    "qty": float(activity.qty) if hasattr(activity, 'qty') else None,
                    "price": float(activity.price) if hasattr(activity, 'price') else None,
                })

            result.append(activity_dict)

        return result
    except Exception as e:
        logger.error(f"활동 내역 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
