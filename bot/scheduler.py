"""
Daily scheduled backtest for the Arena Sports Bot.

Re-checks the strategy once a day (configurable hour/minute, server-local time),
saves the report, and raises an alert with the summary — so you are notified if
the strategy's performance drifts.

Config lives in the engine settings (persisted with state.json):
  daily_backtest_enabled  : bool
  daily_backtest_hour     : 0-23
  daily_backtest_minute   : 0-59
  daily_backtest_runs     : number of seeds
  daily_backtest_ticks    : ticks per run

Results are written to data/daily_backtest.json (+ a .html report) and the
latest summary is exposed over the API via GET /api/schedule.
"""

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from bot.backtest import run_report


class DailyBacktestScheduler:
    def __init__(self, engine, data_dir: str, poll_seconds: int = 30):
        self.engine = engine
        self.data_dir = data_dir
        self.poll_seconds = poll_seconds
        self.json_path = os.path.join(data_dir, "daily_backtest.json")
        self.html_path = os.path.join(data_dir, "daily_backtest_report.html")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._state: Dict[str, Any] = self._load_state()
        self._running_backtest = False
        self.telegram = None  # set by the server after the commander is created

    # -- persistence ------------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path) as f:
                    d = json.load(f)
                return {"last_run": d.get("last_run"), "summary": d.get("summary")}
            except Exception:
                pass
        return {"last_run": None, "summary": None}

    def _save_state(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.json_path, "w") as f:
                json.dump({"last_run": self._state["last_run"], "summary": self._state["summary"]}, f, indent=2)
        except Exception:
            pass

    # -- scheduling -------------------------------------------------------
    def next_run(self) -> Optional[str]:
        if not self.engine.settings.get("daily_backtest_enabled"):
            return None
        hour = int(self.engine.settings.get("daily_backtest_hour", 8))
        minute = int(self.engine.settings.get("daily_backtest_minute", 0))
        now = time.time()
        lt = time.localtime()
        candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
        if candidate <= now:
            candidate += 86400.0
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(candidate))

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.engine.settings.get("daily_backtest_enabled")),
            "hour": int(self.engine.settings.get("daily_backtest_hour", 8)),
            "minute": int(self.engine.settings.get("daily_backtest_minute", 0)),
            "runs": int(self.engine.settings.get("daily_backtest_runs", 50)),
            "ticks": int(self.engine.settings.get("daily_backtest_ticks", 600)),
            "next_run": self.next_run(),
            "last_run": self._state.get("last_run"),
            "last_summary": self._state.get("summary"),
            "running": self._running_backtest,
            "report_url": "/daily-backtest-report" if self._state.get("last_run") else None,
        }

    # -- execution --------------------------------------------------------
    def _due(self) -> bool:
        nxt = self.next_run()
        if not nxt:
            return False
        return time.time() >= time.mktime(time.strptime(nxt, "%Y-%m-%d %H:%M"))

    def _loop(self) -> None:
        last_fired_key = None
        while not self._stop.is_set():
            try:
                if self.engine.settings.get("daily_backtest_enabled") and self._due():
                    key = self.next_run()
                    if key != last_fired_key:
                        last_fired_key = key
                        self.run_now(broadcast=True)
            except Exception:
                pass
            self._stop.wait(self.poll_seconds)

    def run_now(self, broadcast: bool = False) -> Dict[str, Any]:
        if self._running_backtest:
            return {"error": "already running"}
        self._running_backtest = True
        try:
            runs = max(5, min(200, int(self.engine.settings.get("daily_backtest_runs", 50))))
            ticks = max(100, min(5000, int(self.engine.settings.get("daily_backtest_ticks", 600))))
            report = run_report(runs, ticks,
                                json_path=self.json_path, html_path=self.html_path)
            agg = report["aggregate"]
            summary = {
                "num_runs": agg["num_runs"],
                "ticks": agg["ticks"],
                "mean_pnl": agg["pnl"]["mean"],
                "median_pnl": agg["pnl"]["median"],
                "min_pnl": agg["pnl"]["min"],
                "max_pnl": agg["pnl"]["max"],
                "profitable_pct": agg["profitable_pct"],
                "mean_return_pct": agg["mean_return_pct"],
                "worst_drawdown": agg["worst_drawdown"],
                "worst_drawdown_pct": agg["worst_drawdown_pct"],
            }
            self._state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._state["summary"] = summary
            self._save_state()

            # notify via the configured alert channels (email/webhook/log).
            # telegram is skipped here — it gets a richer dedicated message below.
            if self.engine.alerts:
                self.engine.alerts.emit(
                    "notice", "Daily backtest complete",
                    f"{runs} runs × {ticks} ticks: mean P&L {summary['mean_pnl']:+,.2f} · "
                    f"{summary['profitable_pct']:.0f}% profitable · "
                    f"worst drawdown -{summary['worst_drawdown']:,.0f} "
                    f"({summary['worst_drawdown_pct']:.1f}%)",
                    skip={"telegram"},
                )

            # push the "morning report" to the owner's Telegram chat
            if broadcast and self.telegram is not None:
                self.telegram.broadcast_morning_report(
                    summary, time.strftime("%Y-%m-%d %H:%M:%S"))
            return {"ok": True, "summary": summary}
        finally:
            self._running_backtest = False

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
