import logging
from app.worker import celery_app
from app.core.database import SessionLocal
from app.services.portfolio_optimizer import PortfolioOptimizer
from app.services.portfolio_rebalancer import PortfolioRebalancer
from app.repositories.portfolio_repo import PortfolioRepository
from app.repositories.stock_repo_sync import SyncStockRepository

from alpaca.trading.client import TradingClient
from app.core.config import settings

logger = logging.getLogger(__name__)

# 심볼 목록 (확장 가능)
# PORTFOLIO_SYMBOLS = [
#     'AAPL', 'MSFT', 'GOOGL', 'NVDA', 'TSLA',
#     'META', 'AMZN', 'AMD', 'NFLX', 'SPY'
# ]

def get_portfolio_symbols():
    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        symbols = repo.get_active_symbols()
        return symbols
    
    except Exception as e:
        logger.error("포트폴리오 심볼 조회 실패: %s", str(e), exc_info=True)
    finally:
        session.close()

@celery_app.task(name="app.tasks.portfolio.update_portfolio_parameters")
def update_portfolio_parameters():
    """
    일일 파라미터 업데이트 태스크 (00:00 ET).
    
    업데이트 항목:
    - 상관 행렬 (14일 롤링)
    - VaR (95% 신뢰도)
    - Kelly Criterion 크기
    
    충분한 거래 데이터 존재 시 백테스트에서 실시간 데이터로 자동 전환.
    """
    logger.info("일일 포트폴리오 파라미터 업데이트 시작")
    
    session = SessionLocal()
    try:
        # Initialize services
        portfolio_repo = PortfolioRepository(session)
        optimizer = PortfolioOptimizer(lookback_days=14, min_live_trades=50)
        
        # 포트폴리오 가치 가져오기
        is_paper = 'paper' in settings.ALPACA_TRADING_URL.lower()
        trading_client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=is_paper
        )
        account = trading_client.get_account()
        portfolio_value = float(account.portfolio_value)
        
        logger.info("포트폴리오 가치: $%.2f", portfolio_value)
        symbols = get_portfolio_symbols()
        # 1. 상관 행렬 업데이트
        corr_matrix = optimizer.calculate_correlation_matrix(
            portfolio_repo,
            symbols,
            use_live_data=True
        )
        logger.info("상관행렬 업데이트 완료 (%s)", str(corr_matrix.shape))
        
        # 2. VaR 업데이트
        var = optimizer.calculate_var(
            portfolio_repo,
            portfolio_value,
            confidence=0.95,
            use_live_data=True
        )
        logger.info("VaR (95%%) 업데이트: $%.2f", var)
        
        # 3. Kelly 크기 업데이트
        for symbol in symbols:
            kelly = optimizer.kelly_criterion(
                portfolio_repo,
                symbol,
                use_live_data=True
            )
            logger.info("%s Kelly 비율: %.2f%%", symbol, kelly * 100)
        
        logger.info("포트폴리오 파라미터 업데이트 완료")
        
    except Exception as e:
        logger.error("파라미터 업데이트 실패: %s", str(e), exc_info=True)
    finally:
        session.close()


@celery_app.task(name="app.tasks.portfolio.rebalance_portfolio")
def rebalance_portfolio(force: bool = False):
    """
    일일 포트폴리오 리밸런싱 태스크 (15:45 ET, 종가 15분 전).
    
    프로세스:
    1. 최적 가중치 계산 (MPT)
    2. 현재 가중치로부터의 드리프트 확인
    3. 드리프트 > 5% 시 리밸런싱 (또는 force=True)
    
    Args:
        force: True일 경우 드리프트와 무관하게 리밸런싱
    """
    logger.info("일일 포트폴리오 리밸런싱 시작")
    
    session = SessionLocal()
    try:
        # Initialize services
        portfolio_repo = PortfolioRepository(session)
        optimizer = PortfolioOptimizer(lookback_days=14)
        
        is_paper = 'paper' in settings.ALPACA_TRADING_URL.lower()
        trading_client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=is_paper
        )
        
        rebalancer = PortfolioRebalancer(trading_client, portfolio_repo, optimizer)
        symbols = get_portfolio_symbols()
        # 리밸런싱 실행
        rebalancer.rebalance(symbols, force=force)
        
        logger.info("포트폴리오 리밸런싱 완료")
        
    except Exception as e:
        logger.error("리밸런싱 실패: %s", str(e), exc_info=True)
    finally:
        session.close()
