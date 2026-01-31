
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging

import pandas as pd

from app.core.database import SessionLocal
from app.ml.features import FeatureEngineer
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.regime import RegimeDetector

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_regime_distribution():
    """Check regime distribution across historical data"""

    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        feature_engineer = FeatureEngineer()
        regime_detector = RegimeDetector()

        # Load SPY data (30 days)
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - pd.Timedelta(days=30)

        spy_ohlcv = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='15m')

        if len(spy_ohlcv) < 50:
            logger.error(f"Insufficient SPY data: {len(spy_ohlcv)} bars")
            return

        logger.info(f"SPY data loaded: {len(spy_ohlcv)} bars")

        # Create DataFrame
        spy_df = pd.DataFrame([{
            'symbol': bar.symbol,
            'date_time': bar.date_time,
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume
        } for bar in spy_ohlcv])
        spy_df.set_index('date_time', inplace=True)
        spy_df.sort_index(inplace=True)

        # Generate features
        spy_features = feature_engineer.create_features(spy_df)

        if spy_features.empty:
            logger.error("Feature engineering failed")
            return

        # Detect regime for each timestamp
        regimes = []
        regime_details = []

        for idx in range(50, len(spy_features)):  # Start after 50 bars for SMA50
            window = spy_features.iloc[:idx+1]

            # Get latest data point
            latest = window.iloc[-1]
            close = float(latest.get('close', 0))
            sma_50 = float(latest.get('sma_50', close))
            adx = float(latest.get('adx', 0))
            atr_pct = float(latest.get('atr_pct', 0))

            # Calculate price change
            if idx >= 60:
                price_change_10d = (close - spy_features['close'].iloc[idx-10]) / spy_features['close'].iloc[idx-10]
            else:
                price_change_10d = 0

            regime = regime_detector.detect_regime(window)
            regimes.append(regime.value)

            regime_details.append({
                'timestamp': window.index[-1],
                'regime': regime.value,
                'close': close,
                'sma_50': sma_50,
                'adx': adx,
                'atr_pct': atr_pct,
                'price_change_10bar': price_change_10d,
                'trend_up': close > sma_50
            })

        # Print distribution
        regime_counts = pd.Series(regimes).value_counts()
        logger.info("\n" + "="*60)
        logger.info("REGIME DISTRIBUTION (Last 30 days)")
        logger.info("="*60)
        for regime, count in regime_counts.items():
            pct = count / len(regimes) * 100
            logger.info(f"{regime:20s}: {count:4d} bars ({pct:5.1f}%)")
        logger.info("="*60)

        # Print sample periods for EACH regime
        df_details = pd.DataFrame(regime_details)

        for regime_name in ['bull_trending', 'bear_trending', 'sideways_volatile', 'sideways_calm']:
            regime_periods = df_details[df_details['regime'] == regime_name]

            logger.info(f"\n{'='*60}")
            logger.info(f"{regime_name.upper()} Periods: {len(regime_periods)} bars")
            logger.info(f"{'='*60}")

            if len(regime_periods) > 0:
                logger.info("Sample periods (first 5):")
                sample = regime_periods.head(5)[['timestamp', 'close', 'sma_50', 'adx', 'atr_pct', 'price_change_10bar', 'trend_up']]
                logger.info(sample.to_string())

                # Statistics
                logger.info("\nStatistics:")
                logger.info(f"  Average ADX: {regime_periods['adx'].mean():.2f}")
                logger.info(f"  Average ATR%: {regime_periods['atr_pct'].mean():.4f}")
                logger.info(f"  Average Price Change (10 bars): {regime_periods['price_change_10bar'].mean():.4f}")
                logger.info(f"  Trend Up %: {regime_periods['trend_up'].sum() / len(regime_periods) * 100:.1f}%")
            else:
                logger.warning(f"NO {regime_name.upper()} periods found!")

                # Show what conditions were not met
                logger.info(f"\nConditions for {regime_name.upper()}:")
                if regime_name == 'bull_trending':
                    logger.info("  1. ADX > 25 (strong trend)")
                    logger.info("  2. Close > SMA50 (uptrend)")
                    logger.info("  3. Price change (10 bars) > 0.005 (0.5%)")

                    # Check how many bars met each condition
                    adx_met = (df_details['adx'] > 25).sum()
                    trend_up_met = df_details['trend_up'].sum()
                    price_change_met = (df_details['price_change_10bar'] > 0.005).sum()

                    logger.info(f"\nCondition Analysis (out of {len(df_details)} bars):")
                    logger.info(f"  ADX > 25: {adx_met} bars ({adx_met/len(df_details)*100:.1f}%)")
                    logger.info(f"  Close > SMA50: {trend_up_met} bars ({trend_up_met/len(df_details)*100:.1f}%)")
                    logger.info(f"  Price change > 0.5%: {price_change_met} bars ({price_change_met/len(df_details)*100:.1f}%)")

                    # Find closest to bull conditions
                    df_details['bull_score'] = 0
                    df_details.loc[df_details['trend_up'], 'bull_score'] += 1
                    df_details.loc[df_details['adx'] > 25, 'bull_score'] += 1
                    df_details.loc[df_details['price_change_10bar'] > 0.005, 'bull_score'] += 1

                    almost_bull = df_details.nlargest(5, 'bull_score')
                    logger.info("\nClosest to BULL conditions (score 3 = all met):")
                    logger.info(almost_bull[['timestamp', 'regime', 'adx', 'price_change_10bar', 'trend_up', 'bull_score']].to_string())

                elif regime_name == 'bear_trending':
                    logger.info("  1. ADX > 25 (strong trend)")
                    logger.info("  2. Close < SMA50 (downtrend)")
                    logger.info("  3. Price change (10 bars) < -0.005 (-0.5%)")

                    # Check conditions
                    adx_met = (df_details['adx'] > 25).sum()
                    trend_down_met = (~df_details['trend_up']).sum()
                    price_change_met = (df_details['price_change_10bar'] < -0.005).sum()

                    logger.info(f"\nCondition Analysis (out of {len(df_details)} bars):")
                    logger.info(f"  ADX > 25: {adx_met} bars ({adx_met/len(df_details)*100:.1f}%)")
                    logger.info(f"  Close < SMA50: {trend_down_met} bars ({trend_down_met/len(df_details)*100:.1f}%)")
                    logger.info(f"  Price change < -0.5%: {price_change_met} bars ({price_change_met/len(df_details)*100:.1f}%)")

    finally:
        session.close()


if __name__ == '__main__':
    check_regime_distribution()
