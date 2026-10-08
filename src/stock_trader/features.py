import numpy as np
import pandas as pd

FEATURES = [
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "volatility",
    "atr_pct",
    "ma_distance",
    "vwap_distance",
    "relative_volume",
    "range_pct",
    "minute_of_session",
    "market_ret_3",
    "market_ret_12",
    "relative_strength",
]
FEATURE_VERSION = "iex-complete-5m-v1"


def feature_frame(bars: pd.DataFrame, benchmarks=("SPY", "QQQ"), *, include_invalid=False) -> pd.DataFrame:
    frame = bars.copy().sort_values(["symbol", "timestamp"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    local = frame["timestamp"].dt.tz_convert("America/New_York")
    frame["date"] = local.dt.strftime("%Y-%m-%d")
    frame["minute_of_session"] = local.dt.hour * 60 + local.dt.minute - 570
    frame = frame.loc[frame["minute_of_session"].between(0, 385)].copy()
    result = []
    for _, group in frame.groupby(["symbol", "date"], sort=False):
        group = group.copy()
        close = group["close"]
        previous = close.shift()
        for n in (1, 3, 6, 12):
            group[f"ret_{n}"] = close.pct_change(n, fill_method=None)
        tr = pd.concat(
            [group["high"] - group["low"], (group["high"] - previous).abs(), (group["low"] - previous).abs()],
            axis=1,
        ).max(axis=1)
        group["atr"] = tr.rolling(14, min_periods=14).mean()
        group["atr_pct"] = group["atr"] / close
        group["volatility"] = group["ret_1"].rolling(12).std()
        group["ma_distance"] = close / close.rolling(12).mean() - 1
        typical = (group["high"] + group["low"] + close) / 3
        vwap = (typical * group["volume"]).cumsum() / group["volume"].cumsum().replace(0, np.nan)
        group["vwap_distance"] = close / vwap - 1
        group["relative_volume"] = group["volume"] / group["volume"].rolling(12).mean().replace(0, np.nan)
        group["range_pct"] = (group["high"] - group["low"]) / close
        # A missing five-minute bucket makes every dependent lookback invalid until continuity returns.
        gaps = group["timestamp"].diff().dt.total_seconds().ne(300)
        group["continuous"] = gaps.rolling(14, min_periods=14).sum().eq(0)
        result.append(group)
    if not result:
        return pd.DataFrame(columns=[*frame.columns, *FEATURES, "atr", "continuous"])
    frame = pd.concat(result)
    market = frame.loc[frame["symbol"].isin(benchmarks)].groupby("timestamp")[["ret_3", "ret_12"]].mean()
    market = market.rename(columns={"ret_3": "market_ret_3", "ret_12": "market_ret_12"})
    frame = frame.join(market, on="timestamp")
    frame["relative_strength"] = frame["ret_3"] - frame["market_ret_3"]
    finite = np.isfinite(frame[FEATURES].to_numpy(dtype=float)).all(axis=1)
    frame["feature_valid"] = finite & frame["continuous"]
    if not include_invalid:
        frame = frame.loc[frame["feature_valid"]]
    return frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def labels(frame: pd.DataFrame, horizon: int) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby(["symbol", "date"]):
        # Entry can occur only after the signal bar closes: use next bar open, not signal close.
        entry = group["open"].shift(-1)
        exit_price = group["close"].shift(-horizon)
        target_time = group["timestamp"].shift(-horizon)
        contiguous = (target_time - group["timestamp"]).dt.total_seconds().eq(horizon * 300)
        result.loc[group.index] = (exit_price / entry - 1).where(contiguous)
    return result
