"""
Parameter tuning / grid search for the Arena Sports Bot.

Searches a grid of strategy settings, runs each combo across many seeds, and
ranks the results. Optimises for *low drawdown* (the user's #1 concern with a
small bankroll) while tracking profit honestly.

Metrics per combo (aggregated over seeds):
  * mean / median / worst realized P&L, % profitable runs, mean return %
  * mean / worst max-drawdown (₦ and % of bankroll)

Run a default search:
    python3 -m bot.tune

Or pass an explicit grid in Python (see grid_search below).
"""

import argparse
import itertools
import random
import time
from typing import Any, Dict, List

from bot.backtest import run_once

# Default grid: the levers that most affect drawdown / spread-drag.
DEFAULT_GRID: Dict[str, List[Any]] = {
    "max_stake_pct": [0.01, 0.02],
    "max_open_positions": [1, 2],
    "max_adds": [0, 1],
    "stop_loss_pct": [0.30, 0.45, 0.60],
    "take_profit_pct": [0.15, 0.25, 0.40],
    "entry_signal": [0.03, 0.05, 0.08],
    "exit_signal": [0.0, 0.01, 0.02],
    "add_signal": [0.09],
    "ema_slow": [15],
}


def grid_search(grid: Dict[str, List[Any]], seeds: int = 10, ticks: int = 500,
                base_settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    keys = list(grid.keys())
    combos = list(itertools.product(*(grid[k] for k in keys)))
    results: List[Dict[str, Any]] = []
    for combo in combos:
        params = dict(zip(keys, combo))
        settings = dict(base_settings or {})
        settings.update(params)
        pnls, dds, dds_pct = [], [], []
        for seed in range(seeds):
            r = run_once(seed, ticks, settings)
            pnls.append(r["realized_pnl"])
            dds.append(r["max_drawdown"])
            dds_pct.append(r["max_drawdown_pct"])
        n = len(pnls)
        mean = sum(pnls) / n
        results.append({
            "params": params,
            "mean_pnl": round(mean, 2),
            "median_pnl": round(sorted(pnls)[n // 2], 2),
            "worst_pnl": round(min(pnls), 2),
            "profitable_pct": round(sum(1 for p in pnls if p > 0) / n * 100, 1),
            "mean_drawdown": round(sum(dds) / n, 2),
            "worst_drawdown": round(max(dds), 2),
            "worst_drawdown_pct": round(max(dds_pct), 1),
        })
    return results


def print_table(results: List[Dict[str, Any]], sort_by: str = "worst_drawdown",
                reverse: bool = False, top: int = 25) -> None:
    rows = sorted(results, key=lambda r: r[sort_by], reverse=reverse)[:top]
    hdr = ("mean P&L", "median", "worst P&L", "%prof", "meanDD", "worstDD", "DD%", "params")
    print(f"{hdr[0]:>9} {hdr[1]:>8} {hdr[2]:>9} {hdr[3]:>6} {hdr[4]:>8} {hdr[5]:>8} {hdr[6]:>6}  {hdr[7]}")
    print("-" * 120)
    for r in rows:
        p = r["params"]
        plist = ", ".join(f"{k}={v}" for k, v in p.items())
        print(f"{r['mean_pnl']:>9.2f} {r['median_pnl']:>8.2f} {r['worst_pnl']:>9.2f} "
              f"{r['profitable_pct']:>5.1f}% {r['mean_drawdown']:>8.2f} {r['worst_drawdown']:>8.2f} "
              f"{r['worst_drawdown_pct']:>5.1f}%  {plist}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Grid-search strategy settings for low drawdown.")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--ticks", type=int, default=500)
    ap.add_argument("--sort", default="worst_drawdown", choices=[
        "worst_drawdown", "mean_drawdown", "mean_pnl", "profitable_pct"])
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    t0 = time.time()
    results = grid_search(DEFAULT_GRID, seeds=args.seeds, ticks=args.ticks)
    print(f"Grid search: {len(results)} combos x {args.seeds} seeds x {args.ticks} ticks "
          f"in {time.time()-t0:.1f}s\n")
    print_table(results, sort_by=args.sort, top=args.top)
