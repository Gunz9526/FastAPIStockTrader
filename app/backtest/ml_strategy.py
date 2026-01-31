import logging

import backtrader as bt
import pandas as pd

from app.ml.features import FeatureEngineer
from app.ml.predictor import PredictorService

logger = logging.getLogger(__name__)

class MLStrategy(bt.Strategy):
    """
    Backtrader Strategy using PredictorService for signals.
    """
    params = (
        ('threshold', 0.005),  # Buy/Sell threshold (0.5%)
        ('risk_per_trade', 0.1), # Invest 10% of cash per trade (simple sizing)
    )

    def __init__(self):
        self.predictor = PredictorService()
        self.feature_engineer = FeatureEngineer()
        self.dataclose = self.datas[0].close
        self.order = None
        self.buyprice = None
        self.buycomm = None

        # To keep track of prediction history
        self.predictions = []

    def log(self, txt, dt=None):
        """Logging function for this strategy"""
        dt = dt or self.datas[0].datetime.date(0)
        logger.info(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            # Buy/Sell order submitted/accepted to/by broker - Nothing to do
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(
                    f'BUY EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}'
                )
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            else:  # Sell
                self.log(
                    f'SELL EXECUTED, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.2f}'
                )

            self.bar_executed = len(self)

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('Order Canceled/Margin/Rejected')

        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return

        self.log(f'OPERATION PROFIT, GROSS {trade.pnl:.2f}, NET {trade.pnlcomm:.2f}')

    def next(self):
        # Simply log the closing price of the series from the reference
        # self.log(f'Close, {self.dataclose[0]:.2f}')

        if self.order:
            return

        # 1. Prepare Data for Prediction (Need history for features)
        # Backtrader feeds data bar by bar. We need to construct a DataFrame window.
        # This is tricky in Backtrader. We'll use a hack or assume we can access full history.
        # Better approach: We pre-calculate predictions before backtest?
        # No, to simulate reality, we should calculate on the fly (or use pre-calc aligned by date).

        # Simpler approach for verification:
        # Pre-calculated predictions passed as a separate data feed or csv is more efficient,
        # but let's try to simulate 'PredictorService' usage.

        try:
            # Construct DataFrame from recent history (e.g. last 100 bars)
            # Accessing internal deque of Backtrader
            # Note: This is slow. For production backtest, optimize this.
            lookback = 60 # Enough for some indicators
            if len(self) < lookback:
                return

            data_window = {
                'open': list(self.datas[0].open.get(ago=0, size=lookback)),
                'high': list(self.datas[0].high.get(ago=0, size=lookback)),
                'low': list(self.datas[0].low.get(ago=0, size=lookback)),
                'close': list(self.datas[0].close.get(ago=0, size=lookback)),
                'volume': list(self.datas[0].volume.get(ago=0, size=lookback)),
            }
            # Dates
            dates = [
                bt.num2date(d) for d in self.datas[0].datetime.get(ago=0, size=lookback)
            ]

            df = pd.DataFrame(data_window)
            df['date_time'] = dates
            df.set_index('date_time', inplace=True)

            # 2. Features
            features_df = self.feature_engineer.create_features(df)
            if features_df.empty:
                return

            # 3. Predict
            current_features = features_df.iloc[[-1]]
            scaled_features = self.feature_engineer.extract_feature_vector(
                current_features, fit_scaler=False
            )

            prediction = self.predictor.predict_next(scaled_features)
            self.predictions.append(prediction)

            # 4. Trading Logic
            threshold = self.params.threshold

            if not self.position:
                if prediction > threshold:
                    # BUY
                    size = int(self.broker.get_cash() * self.params.risk_per_trade / self.dataclose[0])
                    if size > 0:
                        self.log(f'BUY CREATE, {self.dataclose[0]:.2f} (Pred: {prediction:.4f})')
                        self.order = self.buy(size=size)

            else:
                # Sell rule: Signal reversal or Stop loss/Take profit (not implemented here)
                if prediction < -threshold:
                    # SELL (Exit)
                    self.log(f'SELL CREATE, {self.dataclose[0]:.2f} (Pred: {prediction:.4f})')
                    self.order = self.close()

        except Exception:
            # logger.error(f"Strategy error: {e}")
            pass
