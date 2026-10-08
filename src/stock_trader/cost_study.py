"""Pre-registered cost diagnostics; no training, holdout access or promotion."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import STRATEGIES, checksum
from stock_trader.research import prepare_replay, simulate
from stock_trader.study import summarize_trades, write_json


def fixed_trade_costs(trades, capital):
    gross = sum(t["gross_pnl"] for t in trades)
    # Existing simulator charges half the assumed round-trip cost on each notional.
    notional = sum(t["qty"] * (t["entry_price"] + t["exit_price"]) / 2 for t in trades)
    return {
        "trades": len(trades),
        "gross_return": gross / capital,
        "break_even_bps": gross / notional * 10000 if notional else None,
        "net_returns": {str(bps): (gross - notional * bps / 10000) / capital for bps in (0, 5, 10, 20, 30)},
    }


def validate_predictions(frame, saved, holdout):
    keys = ["timestamp", "symbol", "feature_valid", "date"]
    if frame["date"].max() >= holdout or saved["date"].max() >= holdout:
        raise ValueError("sealed holdout in research input")
    if not saved[keys].reset_index(drop=True).equals(frame[keys].reset_index(drop=True)):
        raise ValueError("prediction keys do not match validation frame")
    if not set(STRATEGIES).issubset(saved.columns):
        raise ValueError("missing model predictions")


def run_cost_study(settings, source):
    started = time.monotonic()
    original = json.loads((source / "report.json").read_text())
    if original["holdout_evaluated"] or original["promoted"] or len(original["candidates"]) != 108:
        raise ValueError("expected completed sealed ADR-0011 study")
    if original["settings"] != settings.public_contract():
        raise ValueError("settings differ from the registered reference")
    out = settings.report_dir / f"cost-study-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "trades").mkdir()
    (out / "source").mkdir()
    code_hashes = {}
    for name in ("cost_study.py", "research.py", "study.py", "risk.py", "config.py"):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code_hashes[name] = checksum(path)
    inputs = {}

    def tracked(path):
        inputs[str(path.relative_to(source))] = checksum(path)
        return path

    tracked(source / "report.json")
    frame = pd.read_parquet(tracked(source / "development-features.parquet"))
    holdout = original["holdout_start"]
    if frame["date"].max() >= holdout:
        raise ValueError("sealed holdout in feature input")
    validation = frame.loc[frame["date"] >= original["folds"][0]["validation_start"]]
    fold_frames = [
        validation.loc[validation["date"].between(f["validation_start"], f["validation_end"])]
        for f in original["folds"]
    ]
    replays = [prepare_replay(f) for f in (validation, *fold_frames)]
    report = {
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "holdout_start": holdout,
        "holdout_evaluated": False,
        "promoted": False,
        "additional_fits": 0,
        "fixed_trades": [],
        "candidates": [],
        "code_sha256": code_hashes,
        "input_sha256": inputs,
        "settings": settings.public_contract(),
        "scope": "ADR-0014: fixed 72 ledgers; 120-minute 72 resimulations + 24 references",
    }
    for c in original["candidates"]:
        if c["phase"] == "legacy":
            continue
        ledger = json.loads(tracked(source / "trades" / c["trade_file"]).read_text())
        fixed = fixed_trade_costs(ledger, settings.capital_limit)
        if fixed["trades"] != c["metrics"]["trades"] or not np.isclose(
            fixed["net_returns"]["20"], c["metrics"]["net_return"], atol=1e-10
        ):
            raise ValueError("20bps ledger reproduction mismatch")
        report["fixed_trades"].append(
            {
                "phase": c["phase"],
                "family": c["family"],
                "horizon": c["horizon"],
                "threshold": c["threshold"],
                **fixed,
            }
        )
        if c["horizon"] == 24:
            report["candidates"].append({**c, "cost_bps": 20, "origin": "reference"})
    for target, phase in (("endpoint", "timing_corrected"), ("aligned", "aligned")):
        saved = pd.read_parquet(tracked(source / "predictions" / f"{target}-24.parquet"))
        validate_predictions(validation, saved, holdout)
        for family in STRATEGIES:
            predictions = pd.Series(saved[family].to_numpy(), index=validation.index)
            for threshold in (0.003, 0.0045, 0.006):
                for bps in (5, 10, 30):
                    print(
                        json.dumps(
                            {
                                "stage": "evaluate",
                                "phase": phase,
                                "family": family,
                                "threshold": threshold,
                                "cost_bps": bps,
                                "completed": len(report["candidates"]),
                                "total": 96,
                            }
                        ),
                        flush=True,
                    )
                    alternate = settings.model_copy(update={"round_trip_cost_bps": bps})
                    trades = []
                    metrics = simulate(
                        validation,
                        predictions,
                        threshold=threshold,
                        horizon=24,
                        settings=alternate,
                        replay=replays[0],
                        execution="timed_open",
                        trades=trades,
                    )
                    folds = [
                        asdict(
                            simulate(
                                f,
                                predictions,
                                threshold=threshold,
                                horizon=24,
                                settings=alternate,
                                replay=r,
                                execution="timed_open",
                            )
                        )
                        for f, r in zip(fold_frames, replays[1:], strict=True)
                    ]
                    ledger = summarize_trades(trades, settings.capital_limit)
                    if not np.isclose(metrics.net_return, ledger["net_return_from_ledger"], atol=1e-10):
                        raise ValueError("portfolio ledger mismatch")
                    filename = f"{phase}-{family}-{threshold:.4f}-{bps}bps.json"
                    write_json(out / "trades" / filename, trades)
                    report["candidates"].append(
                        {
                            "phase": phase,
                            "family": family,
                            "horizon": 24,
                            "threshold": threshold,
                            "cost_bps": bps,
                            "origin": "resimulation",
                            "metrics": asdict(metrics),
                            "folds": folds,
                            "ledger": ledger,
                            "trade_file": filename,
                            "eligible_under_assumed_cost": metrics.trades >= 60
                            and metrics.net_return > 0
                            and metrics.max_drawdown <= 0.08
                            and metrics.profit_factor >= 1.1
                            and sum(f["net_return"] > 0 for f in folds) >= 2,
                        }
                    )
        write_json(out / "report.json", report)
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_at"] = utcnow().isoformat()
    write_json(out / "report.json", report)
    print(
        json.dumps({"stage": "complete", "report": str(out), "candidates": len(report["candidates"])}),
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    run_cost_study(Settings(), parser.parse_args().source)


if __name__ == "__main__":
    main()
