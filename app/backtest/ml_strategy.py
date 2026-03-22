import logging

import backtrader as bt
import pandas as pd

from app.core.config import REGIME_TRADING_CONFIG
from app.ml.features import FeatureEngineer
from app.ml.models import CLASS_NAMES
from app.ml.predictor import PredictorService
from app.services.regime import MarketRegime, RegimeDetector

logger = logging.getLogger(__name__)

# Build backtest-specific config from the canonical source
BACKTEST_REGIME_CONFIG: dict[str, dict] = {
    regime: {
        "confidence_threshold": cfg["confidence_threshold"],
        "min_hold_days": cfg.get("min_hold_days", 1),
        "position_scale": cfg.get("position_scale", 1.0),
    }
    for regime, cfg in REGIME_TRADING_CONFIG.items()
}

_DEFAULT_REGIME_CONFIG: dict = {
    "confidence_threshold": 0.50,
    "min_hold_days": 1,
    "position_scale": 1.0,
}


class MLStrategy(bt.Strategy):
    """Backtrader Strategy using ternary classification (DOWN/NEUTRAL/UP).

    Uses ``PredictorService.predict_class()`` which returns
    ``(predicted_class, confidence, probabilities)``.

    Signal logic:
        * **BUY** — ``predicted_class == 2`` (UP) *and*
          ``confidence >= confidence_threshold``
        * **SELL** — ``predicted_class == 0`` (DOWN) *and*
          ``confidence >= confidence_threshold``
        * Minimum hold-days guard prevents premature exit.

    Position sizing is ATR-based (2× ATR stop, 2 % risk-per-trade) with a
    regime-dependent ``position_scale`` multiplier.
    """

    params = (
        ("risk_per_trade", 0.10),  # Fallback fraction of cash per trade
        ("regime_aware", True),     # Use regime-specific thresholds & model
        ("symbol", ""),
        ("trailing_atr_mult", 1.5),  # Matches production risk_manager
        ("stop_loss_atr_mult", 2.0),  # Matches production risk_manager
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self.predictor = PredictorService()
        self.feature_engineer = FeatureEngineer()
        self.regime_detector = RegimeDetector()
        self.dataclose = self.datas[0].close
        self.order: bt.Order | None = None
        self.buyprice: float | None = None
        self.buycomm: float | None = None

        # Classification history: list of (class, confidence, probabilities)
        self.predictions: list[tuple[int, float, dict[str, float]]] = []

        # Minimum-hold tracking (bar index of entry)
        self.entry_bar: int = 0
        self.bar_executed: int = 0

        # Risk management: ATR-based stops (production parity)
        self.trailing_stop: float | None = None
        self.stop_loss: float | None = None
        self.last_atr: float = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def log(self, txt: str, dt=None) -> None:
        """Write a timestamped log line."""
        dt = dt or self.datas[0].datetime.date(0)
        logger.info("%s, %s", dt.isoformat(), txt)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify_order(self, order: bt.Order) -> None:
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(
                    f"BUY EXECUTED, Price: {order.executed.price:.2f}, "
                    f"Cost: {order.executed.value:.2f}, "
                    f"Comm: {order.executed.comm:.2f}"
                )
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
                self.entry_bar = len(self)

                # Set initial stops (ATR-based, matching production risk_manager)
                if self.last_atr > 0:
                    self.stop_loss = order.executed.price - self.params.stop_loss_atr_mult * self.last_atr
                    self.trailing_stop = order.executed.price - self.params.trailing_atr_mult * self.last_atr
                else:
                    # Fallback: percentage-based when ATR unavailable
                    self.stop_loss = order.executed.price * 0.90
                    self.trailing_stop = order.executed.price * 0.985
            else:
                self.log(
                    f"SELL EXECUTED, Price: {order.executed.price:.2f}, "
                    f"Cost: {order.executed.value:.2f}, "
                    f"Comm: {order.executed.comm:.2f}"
                )

                # Reset stops on position exit
                self.trailing_stop = None
                self.stop_loss = None

            self.bar_executed = len(self)

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log("Order Canceled/Margin/Rejected")

        self.order = None

    def notify_trade(self, trade: bt.Trade) -> None:
        if not trade.isclosed:
            return
        self.log(f"OPERATION PROFIT, GROSS {trade.pnl:.2f}, NET {trade.pnlcomm:.2f}")

    def stop(self) -> None:
        """Log end-of-test state for diagnostics."""
        if self.position:
            self.log("END-OF-TEST: WARNING — position still open after force-close")

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def next(self) -> None:  # noqa: C901 — kept as single method for Backtrader compat
        """Generate signals using ternary classification (DOWN/NEUTRAL/UP)."""
        if self.order:
            return

        # Force close open position on SECOND-TO-LAST bar so the order
        # fills on the final bar.  Calling self.close() on the very last bar
        # creates an order that would need bar N+1 to fill, which does not
        # exist — leaving the position open and trades uncounted.
        if len(self) >= self.datas[0].buflen() - 1 and self.position:
            self.log("END-OF-TEST: closing open position (penultimate bar)")
            self.order = self.close()
            return

        try:
            self._process_bar()
        except Exception as exc:
            logger.error("Strategy error on bar %d: %s", len(self), exc)

    def _process_bar(self) -> None:
        """Internal: build features, predict, and act."""
        lookback = 120
        if len(self) < lookback:
            return

        # ----- 1. Build DataFrame from Backtrader buffers -----
        data_window = {
            "open": list(self.datas[0].open.get(ago=0, size=lookback)),
            "high": list(self.datas[0].high.get(ago=0, size=lookback)),
            "low": list(self.datas[0].low.get(ago=0, size=lookback)),
            "close": list(self.datas[0].close.get(ago=0, size=lookback)),
            "volume": list(self.datas[0].volume.get(ago=0, size=lookback)),
        }
        dates = [
            bt.num2date(d)
            for d in self.datas[0].datetime.get(ago=0, size=lookback)
        ]

        df = pd.DataFrame(data_window)
        df["date_time"] = dates
        df.set_index("date_time", inplace=True)

        # Add symbol for sector_id lookup (avoid Unknown=12 default)
        if self.params.symbol:
            df['symbol'] = self.params.symbol

        # ----- 2. Feature engineering -----
        features_df = self.feature_engineer.create_features(df)
        if features_df.empty:
            return

        # Store current ATR for stop calculations
        current_atr = features_df.iloc[-1].get("atr", None)
        if current_atr is not None and current_atr > 0:
            self.last_atr = float(current_atr)

        # ----- 3. Regime detection -----
        if self.params.regime_aware:
            detected_regime = self.regime_detector.detect_regime(features_df)
            regime_key = detected_regime.value
            regime_cfg = BACKTEST_REGIME_CONFIG.get(regime_key, _DEFAULT_REGIME_CONFIG)
        else:
            detected_regime = MarketRegime.SIDEWAYS_CALM
            regime_cfg = _DEFAULT_REGIME_CONFIG

        confidence_threshold: float = regime_cfg["confidence_threshold"]
        min_hold_days: int = regime_cfg["min_hold_days"]
        position_scale: float = regime_cfg["position_scale"]

        # ----- 4. Extract scaled features (base set) -----
        current_features = features_df.iloc[[-1]]
        regime_suffix = detected_regime.value if detected_regime else 'sideways_calm'
        scaled_features = self.feature_engineer.extract_feature_vector(
            current_features, fit_scaler=False, feature_set="base",
            scaler_suffix=regime_suffix
        )

        # ----- 5. Classification prediction -----
        predicted_class, confidence, probabilities = self.predictor.predict_class(
            scaled_features, regime=detected_regime
        )
        self.predictions.append((predicted_class, confidence, probabilities))
        class_name = CLASS_NAMES[predicted_class]

        # ----- 6. Trading logic -----
        if not self.position:
            # --- BUY: predicted UP with sufficient confidence ---
            if predicted_class == 2 and confidence >= confidence_threshold:
                size = self._calc_position_size(features_df, position_scale)
                if size > 0:
                    self.log(
                        f"BUY CREATE, {self.dataclose[0]:.2f} "
                        f"(Class: {class_name}, Conf: {confidence:.2%}, "
                        f"Regime: {detected_regime.value})"
                    )
                    self.order = self.buy(size=size)
        else:
            # --- Risk Management: Stop Checks (before ML signal) ---
            current_close = self.dataclose[0]

            # 1. Hard stop-loss
            if self.stop_loss is not None and current_close <= self.stop_loss:
                self.log(
                    f"STOP-LOSS HIT, {current_close:.2f} "
                    f"(stop={self.stop_loss:.2f}, entry={self.buyprice:.2f})"
                )
                self.order = self.close()
                return

            # 2. Update trailing stop (ratchet up only)
            if self.trailing_stop is not None and self.last_atr > 0:
                new_trail = current_close - self.params.trailing_atr_mult * self.last_atr
                if new_trail > self.trailing_stop:
                    self.trailing_stop = new_trail

            # 3. Trailing stop check
            if self.trailing_stop is not None and current_close <= self.trailing_stop:
                self.log(
                    f"TRAILING STOP HIT, {current_close:.2f} "
                    f"(trail={self.trailing_stop:.2f}, entry={self.buyprice:.2f})"
                )
                self.order = self.close()
                return

            # --- ML Signal: SELL on DOWN prediction ---
            bars_held = len(self) - self.entry_bar
            if bars_held < min_hold_days:
                return

            if predicted_class == 0 and confidence >= confidence_threshold:
                self.log(
                    f"SELL CREATE, {self.dataclose[0]:.2f} "
                    f"(Class: {class_name}, Conf: {confidence:.2%}, "
                    f"Regime: {detected_regime.value}, Held: {bars_held}d)"
                )
                self.order = self.close()

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def _calc_position_size(
        self, features_df: pd.DataFrame, position_scale: float
    ) -> int:
        """Compute ATR-based position size with regime scale.

        Args:
            features_df: Feature DataFrame (must contain ``atr`` column).
            position_scale: Regime-dependent multiplier (0.0–1.0).

        Returns:
            Number of shares to buy (≥ 0).
        """
        atr = features_df.iloc[-1].get("atr", None)
        if atr and atr > 0:
            risk_amount = self.broker.get_cash() * 0.02  # 2 % risk per trade
            size = int(risk_amount / (atr * 2) * position_scale)
        else:
            # Fallback: fraction of cash
            size = int(
                self.broker.get_cash()
                * self.params.risk_per_trade
                * position_scale
                / self.dataclose[0]
            )
        return max(size, 0)
