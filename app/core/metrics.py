# Prometheus metrics for FastAPI
import logging

from fastapi import Response
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest

logger = logging.getLogger(__name__)

# ==================== API Metrics ====================
api_requests_total = Counter(
    'api_requests_total',
    'Total API requests',
    ['method', 'endpoint', 'status']
)

api_response_time = Histogram(
    'api_response_time_seconds',
    'API response time in seconds',
    ['method', 'endpoint']
)

# ==================== Trading Metrics ====================
# 계좌 잔고 및 포지션
account_balance = Gauge(
    'account_balance_usd',
    'Current account balance in USD'
)

account_buying_power = Gauge(
    'account_buying_power_usd',
    'Current buying power in USD'
)

portfolio_value = Gauge(
    'portfolio_value_usd',
    'Total portfolio value in USD'
)

open_positions_count = Gauge(
    'open_positions_count',
    'Number of currently open positions'
)

position_value = Gauge(
    'position_value_usd',
    'Current position value in USD',
    ['symbol']
)

position_pnl = Gauge(
    'position_pnl_usd',
    'Position unrealized P&L in USD',
    ['symbol']
)

position_pnl_pct = Gauge(
    'position_pnl_pct',
    'Position unrealized P&L percentage',
    ['symbol']
)

# 거래 실행
trades_total = Counter(
    'trades_total',
    'Total number of trades executed',
    ['action', 'symbol']  # action: BUY, SELL
)

trade_value = Counter(
    'trade_value_usd_total',
    'Total trade value in USD',
    ['action', 'symbol']
)

trade_quantity = Counter(
    'trade_quantity_total',
    'Total trade quantity (shares)',
    ['action', 'symbol']
)

# 시장 레짐
market_regime = Info(
    'market_regime',
    'Current market regime classification'
)

regime_confidence = Gauge(
    'regime_confidence_score',
    'Confidence score for regime classification',
    ['regime']
)

# ML 예측
ml_prediction_score = Gauge(
    'ml_prediction_score',
    'ML model prediction score',
    ['symbol', 'regime']
)

ml_model_accuracy = Gauge(
    'ml_model_accuracy',
    'ML model accuracy metric',
    ['model_name', 'regime']
)

# 리스크 관리
risk_var = Gauge(
    'risk_var_usd',
    'Value at Risk (VaR) in USD',
    ['confidence_level']  # 95%, 99%
)

circuit_breaker_triggers = Counter(
    'circuit_breaker_triggers_total',
    'Number of circuit breaker triggers',
    ['reason']  # max_daily_trades, max_daily_loss, cooldown
)

circuit_breaker_state = Gauge(
    'circuit_breaker_state',
    'Circuit breaker state (0=closed, 1=half_open, 2=open)'
)

stop_loss_triggers = Counter(
    'stop_loss_triggers_total',
    'Number of stop-loss triggers',
    ['symbol']
)

take_profit_triggers = Counter(
    'take_profit_triggers_total',
    'Number of take-profit triggers',
    ['symbol']
)

# 시장 데이터
market_price = Gauge(
    'market_price_usd',
    'Current market price',
    ['symbol']
)

market_volume = Gauge(
    'market_volume',
    'Current market volume',
    ['symbol']
)

price_change_pct = Gauge(
    'price_change_pct_1d',
    'Price change percentage (1 day)',
    ['symbol']
)

# Sentiment & Fundamentals
sentiment_score = Gauge(
    'sentiment_score',
    'News sentiment score (-1 to 1)',
    ['symbol']
)

pe_ratio = Gauge(
    'pe_ratio',
    'Price-to-Earnings ratio',
    ['symbol']
)

pb_ratio = Gauge(
    'pb_ratio',
    'Price-to-Book ratio',
    ['symbol']
)

# ==================== Helper Functions ====================

def update_account_metrics(account_info: dict):
    """
    계좌 정보 메트릭 업데이트
    
    Args:
        account_info: {
            'balance': float,
            'buying_power': float,
            'portfolio_value': float
        }
    """
    try:
        if 'balance' in account_info:
            account_balance.set(account_info['balance'])
        if 'buying_power' in account_info:
            account_buying_power.set(account_info['buying_power'])
        if 'portfolio_value' in account_info:
            portfolio_value.set(account_info['portfolio_value'])
    except Exception as e:
        logger.error(f"계좌 메트릭 업데이트 실패: {e}")

def update_position_metrics(positions: list):
    """
    포지션 메트릭 업데이트
    
    Args:
        positions: List of {
            'symbol': str,
            'qty': float,
            'market_value': float,
            'unrealized_pl': float,
            'unrealized_plpc': float
        }
    """
    try:
        open_positions_count.set(len(positions))

        for pos in positions:
            symbol = pos.get('symbol', 'UNKNOWN')
            position_value.labels(symbol=symbol).set(pos.get('market_value', 0))
            position_pnl.labels(symbol=symbol).set(pos.get('unrealized_pl', 0))
            position_pnl_pct.labels(symbol=symbol).set(pos.get('unrealized_plpc', 0) * 100)
    except Exception as e:
        logger.error(f"포지션 메트릭 업데이트 실패: {e}")

def record_trade(action: str, symbol: str, quantity: float, price: float):
    """
    거래 실행 메트릭 기록
    
    Args:
        action: 'BUY' or 'SELL'
        symbol: 종목 심볼
        quantity: 거래 수량
        price: 거래 가격
    """
    try:
        trades_total.labels(action=action, symbol=symbol).inc()
        trade_value.labels(action=action, symbol=symbol).inc(quantity * price)
        trade_quantity.labels(action=action, symbol=symbol).inc(quantity)
    except Exception as e:
        logger.error(f"거래 메트릭 기록 실패: {e}")

def update_regime_metrics(regime: str, confidence: float = 1.0):
    """
    시장 레짐 메트릭 업데이트
    
    Args:
        regime: 'BULL_TRENDING', 'BEAR_TRENDING', 'SIDEWAYS_VOLATILE', 'SIDEWAYS_CALM'
        confidence: 0.0 ~ 1.0
    """
    try:
        market_regime.info({'regime': regime})
        regime_confidence.labels(regime=regime).set(confidence)
    except Exception as e:
        logger.error(f"레짐 메트릭 업데이트 실패: {e}")

def update_ml_prediction(symbol: str, regime: str, score: float):
    """
    ML 예측 메트릭 업데이트
    
    Args:
        symbol: 종목 심볼
        regime: 현재 레짐
        score: 예측 점수
    """
    try:
        ml_prediction_score.labels(symbol=symbol, regime=regime).set(score)
    except Exception as e:
        logger.error(f"ML 예측 메트릭 업데이트 실패: {e}")

def update_market_price(symbol: str, price: float, volume: float = 0, price_change_pct_1d: float = 0):
    """
    시장 가격 메트릭 업데이트
    
    Args:
        symbol: 종목 심볼
        price: 현재 가격
        volume: 거래량
        price_change_pct_1d: 1일 가격 변화율 (%)
    """
    try:
        market_price.labels(symbol=symbol).set(price)
        if volume > 0:
            market_volume.labels(symbol=symbol).set(volume)
        if price_change_pct_1d != 0:
            price_change_pct.labels(symbol=symbol).set(price_change_pct_1d)
    except Exception as e:
        logger.error(f"시장 가격 메트릭 업데이트 실패: {e}")

def metrics_endpoint():
    """Generate Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type="text/plain")
