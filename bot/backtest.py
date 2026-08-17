"""
Multi-run backtest for the Arena Sports Bot.

Runs the engine many times with different random seeds over a fixed number of
ticks, collects per-run metrics, and aggregates them into a JSON + HTML report.

Metrics per run:
  * realized P&L (and % return vs starting bankroll)
  * final equity
  * win rate, closed trades, total actions
  * max drawdown (NGN and %) from the equity curve
  * profit factor (gross wins / gross losses)

Aggregate across runs:
  * mean / median / min / max P&L, % profitable runs, mean return
  * worst drawdown, mean win rate, mean profit factor
  * histogram of per-run P&L

Honesty note: results reflect the SIMULATED market model (market_momentum),
not a guarantee of real-market performance.
"""

import json
import math
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional

from bot.engine import Engine


def run_once(seed: int, ticks: int, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one backtest pass and return its metrics."""
    random.seed(seed)
    e = Engine(state_path=None, alerts_on=False, settings=settings)
    for _ in range(ticks):
        e.tick()

    start = e.settings["starting_balance"]
    realized = e.account.realized_pnl
    equity_series = [p["equity"] for p in e.equity_history]

    # drawdown from equity curve
    peak = start
    max_dd = 0.0
    for eq in equity_series:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    max_dd_pct = (max_dd / peak * 100.0) if peak > 0 else 0.0

    closed = [t for t in e.trades if t.action in ("EXIT", "SETTLE")]
    wins = sum(t.pnl for t in closed if t.pnl > 0)
    losses = -sum(t.pnl for t in closed if t.pnl < 0)
    profit_factor = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)

    exits = [t for t in closed if t.action == "EXIT"]
    win_rate = sum(1 for t in exits if t.pnl > 0) / len(exits) if exits else 0.0

    return {
        "seed": seed,
        "ticks": ticks,
        "starting_balance": _r2(start),
        "final_equity": _r2(e.equity()),
        "realized_pnl": _r2(realized),
        "return_pct": _r2(realized / start * 100.0) if start else 0.0,
        "win_rate": _r2(win_rate),
        "total_trades": len(e.trades),
        "closed_trades": len(closed),
        "max_drawdown": _r2(max_dd),
        "max_drawdown_pct": _r2(max_dd_pct),
        "profit_factor": _r2(profit_factor) if math.isfinite(profit_factor) else None,
    }


def _r2(v: float) -> float:
    return round(v, 2)


def _pct_rank(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p * (len(s) - 1))))
    return s[idx]


def run_many(num_runs: int, ticks: int, settings: Optional[Dict[str, Any]] = None,
             on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    for i in range(num_runs):
        runs.append(run_once(i, ticks, settings))
        if on_progress:
            on_progress(i + 1, num_runs)

    pnls = [r["realized_pnl"] for r in runs]
    returns = [r["return_pct"] for r in runs]
    profitable = sum(1 for p in pnls if p > 0)
    mean = sum(pnls) / len(pnls) if pnls else 0.0

    hist = _histogram(pnls, 10)

    aggregate = {
        "num_runs": num_runs,
        "ticks": ticks,
        "starting_balance": runs[0]["starting_balance"] if runs else 0.0,
        "pnl": {
            "mean": _r2(mean),
            "median": _r2(_pct_rank(pnls, 0.5)),
            "min": _r2(min(pnls)) if pnls else 0.0,
            "max": _r2(max(pnls)) if pnls else 0.0,
            "p5": _r2(_pct_rank(pnls, 0.05)) if pnls else 0.0,
            "p95": _r2(_pct_rank(pnls, 0.95)) if pnls else 0.0,
        },
        "profitable_runs": profitable,
        "profitable_pct": _r2(profitable / num_runs * 100.0) if num_runs else 0.0,
        "mean_return_pct": _r2(sum(returns) / len(returns)) if returns else 0.0,
        "mean_win_rate": _r2(sum(r["win_rate"] for r in runs) / len(runs)) if runs else 0.0,
        "worst_drawdown": _r2(max(r["max_drawdown"] for r in runs)) if runs else 0.0,
        "worst_drawdown_pct": _r2(max(r["max_drawdown_pct"] for r in runs)) if runs else 0.0,
        "mean_profit_factor": _r2(
            sum(r["profit_factor"] for r in runs if r["profit_factor"] is not None) / max(1, len(runs))
        ) if runs else 0.0,
        "histogram": hist,
    }

    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "settings": (settings or {}),
        "aggregate": aggregate,
        "runs": runs,
    }


def _histogram(values: List[float], bins: int) -> List[Dict[str, Any]]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        hi = lo + 1.0
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = min(bins - 1, int((v - lo) / width))
        counts[idx] += 1
    return [{"from": _r2(lo + i * width), "to": _r2(lo + (i + 1) * width), "count": c} for i, c in enumerate(counts)]


def build_html(report: Dict[str, Any]) -> str:
    agg = report["aggregate"]
    mean = agg["pnl"]["mean"]
    prof_pct = agg["profitable_pct"]

    # histogram bars
    max_count = max([h["count"] for h in agg["histogram"]] or [1])
    bars = []
    for h in agg["histogram"]:
        w = max(0.0, (h["count"] / max_count) * 100)
        color = "#22c55e" if (h["from"] + h["to"]) / 2 >= 0 else "#ef4444"
        bars.append(
            "<i style='width:%.1f%%;background:%s' title='%.0f–%.0f: %d'></i>"
            % (w, color, h["from"], h["to"], h["count"])
        )

    rows = "".join(
        "<tr><td>%.0f – %.0f</td><td>%d</td></tr>" % (h["from"], h["to"], h["count"])
        for h in agg["histogram"]
    )

    detail = []
    for r in report["runs"][:50]:
        cls = "pos" if r["realized_pnl"] >= 0 else "neg"
        pf = "—" if r["profit_factor"] is None else "%.2f" % r["profit_factor"]
        detail.append(
            "<tr><td>%d</td><td class='%s'>%+.2f</td><td>%+.2f%%</td>"
            "<td>%.0f%%</td><td class='neg'>-%.0f</td><td>%s</td><td>%d</td></tr>"
            % (r["seed"], cls, r["realized_pnl"], r["return_pct"],
               r["win_rate"] * 100, r["max_drawdown"], pf, r["closed_trades"])
        )

    css = """body{{margin:0;background:#0b0f17;color:#e6eaf2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px}}
.wrap{{max-width:960px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px}} .muted{{color:#8b94a7;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:14px}}
.card{{background:#121826;border:1px solid #1f2937;border-radius:12px;padding:14px 16px}}
.k{{color:#8b94a7;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
.v{{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
.pos{{color:#22c55e}}.neg{{color:#ef4444}}
table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}}
th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid #161d2b}}
th{{color:#8b94a7;font-size:11px;text-transform:uppercase}}
th:first-child,td:first-child{{text-align:left}}
.banner{{background:#1a1206;border:1px solid #3a2b0a;color:#f5d18a;padding:10px 14px;border-radius:10px;font-size:12.5px}}
.bar{{height:18px;background:#121826;border-radius:5px;overflow:hidden;display:flex}}
.bar i{{height:100%}}"""

    tone = "pos" if mean >= 0 else "neg"
    bars_str = "".join(bars)

    html = "<!DOCTYPE html>\n<html><head><meta charset='utf-8'>" \
        "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
        "<title>Backtest Report — Arena Sports Bot</title><style>" + css + "</style></head><body>" \
        "<div class='wrap'>" \
        "<h1>Backtest report</h1>" \
        "<p class='muted'>Generated {gen} · {runs} runs × {ticks} ticks · starting bankroll ₦{start:,.0f}</p>" \
        "<div class='banner'>⚠️ Results are from the <b>simulated market model</b>. " \
        "They are not a prediction or guarantee of real-market performance.</div>" \
        "<div class='cards'>" \
        "<div class='card'><div class='k'>Mean P&amp;L</div><div class='v {tone}'>{mean:+,.2f}</div></div>" \
        "<div class='card'><div class='k'>Median P&amp;L</div><div class='v'>{median:+,.2f}</div></div>" \
        "<div class='card'><div class='k'>Best / Worst</div><div class='v' style='font-size:16px'>{mx:+,.0f} / {mn:+,.0f}</div></div>" \
        "<div class='card'><div class='k'>Profitable runs</div><div class='v'>{prof}/{runs} ({profpct:.0f}%)</div></div>" \
        "<div class='card'><div class='k'>Mean return</div><div class='v'>{ret:+.2f}%</div></div>" \
        "<div class='card'><div class='k'>Worst drawdown</div><div class='v neg'>-{dd:,.0f} ({ddpct:.1f}%)</div></div>" \
        "<div class='card'><div class='k'>Mean win rate</div><div class='v'>{wr:.0f}%</div></div>" \
        "<div class='card'><div class='k'>Mean profit factor</div><div class='v'>{pf:.2f}</div></div>" \
        "</div>" \
        "<h2>P&amp;L distribution (₦ per run)</h2>" \
        "<div class='bar'>{bars}</div>" \
        "<table><thead><tr><th>Bin (₦)</th><th>Runs</th></tr></thead><tbody>{rows}</tbody></table>" \
        "<h2>Per-run detail (first 50)</h2>" \
        "<table><thead><tr><th>Seed</th><th>P&amp;L</th><th>Return</th><th>Win rate</th>" \
        "<th>Max DD</th><th>Profit factor</th><th>Closed</th></tr></thead><tbody>{detail}</tbody></table>" \
        "<p class='muted' style='margin-top:24px'>Arena Sports Bot · nothing here is financial advice.</p>" \
        "</div></body></html>"

    return html.format(
        gen=report["generated"], runs=agg["num_runs"], ticks=agg["ticks"],
        start=agg["starting_balance"], tone=tone, mean=mean,
        median=agg["pnl"]["median"], mx=agg["pnl"]["max"], mn=agg["pnl"]["min"],
        prof=agg["profitable_runs"], profpct=prof_pct, ret=agg["mean_return_pct"],
        dd=agg["worst_drawdown"], ddpct=agg["worst_drawdown_pct"],
        wr=agg["mean_win_rate"] * 100, pf=agg["mean_profit_factor"],
        bars=bars_str, rows=rows, detail="".join(detail),
    )


def run_report(num_runs: int = 50, ticks: int = 500,
               settings: Optional[Dict[str, Any]] = None,
               json_path: Optional[str] = None, html_path: Optional[str] = None,
               on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    report = run_many(num_runs, ticks, settings, on_progress)
    if json_path:
        os.makedirs(os.path.dirname(json_path) or ".", exist_ok=True)
        with open(json_path, "w") as f:
            json.dump(report, f, indent=2)
    if html_path:
        os.makedirs(os.path.dirname(html_path) or ".", exist_ok=True)
        with open(html_path, "w") as f:
            f.write(build_html(report))
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run a multi-run backtest report.")
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--ticks", type=int, default=500)
    ap.add_argument("--out", default="data/backtest")
    args = ap.parse_args()
    t0 = time.time()
    rep = run_report(args.runs, args.ticks,
                     json_path=args.out + "_report.json", html_path=args.out + "_report.html")
    agg = rep["aggregate"]
    print(f"Backtest complete in {time.time()-t0:.1f}s")
    print(f"  runs={agg['num_runs']} ticks={agg['ticks']}")
    print(f"  mean P&L {agg['pnl']['mean']:+,.2f}  median {agg['pnl']['median']:+,.2f}")
    print(f"  profitable {agg['profitable_runs']}/{agg['num_runs']} ({agg['profitable_pct']:.0f}%)")
    print(f"  worst drawdown -{agg['worst_drawdown']:,.2f} ({agg['worst_drawdown_pct']:.1f}%)")
    print(f"  report -> {args.out}_report.html")
