"""
Telegram command interface for the Arena Sports Bot.

Lets you inspect the bot from your phone via Telegram chat commands:

  /start, /help   — list available commands
  /status         — wallet balance, equity, running/halted state, open positions
  /summary        — latest backtest summary (daily scheduled, else on-demand)
  /positions      — open positions and their unrealized P&L
  /run            — run a backtest now and return the summary

How it works
------------
Uses Telegram's getUpdates long-polling (no public HTTPS webhook required).
Reuses the same bot_token / chat_id from the Alerts config, so you configure
Telegram once for both notifications and commands.

Security: only the configured chat_id (owner) can command the bot. If no
chat_id is set yet, the first person to message the bot becomes the owner.
"""

import json
import os
import threading
import urllib.request
from typing import Any, Dict, List, Optional

TELEGRAM_API = "https://api.telegram.org/bot"

NAIRA = "\u20a6"  # ₦


def _f(n: float) -> str:
    return f"{n:+,.2f}"


class TelegramCommander:
    def __init__(self, engine, scheduler, alerts, data_dir: str, poll_seconds: int = 3):
        self.engine = engine
        self.scheduler = scheduler
        self.alerts = alerts
        self.data_dir = data_dir
        self.poll_seconds = poll_seconds
        self._offset = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # -- config -----------------------------------------------------------
    def _token(self) -> str:
        return self.alerts.config.get("telegram", {}).get("bot_token", "")

    def _chat_ids(self) -> List[str]:
        raw = self.alerts.config.get("telegram", {}).get("chat_id", "")
        if isinstance(raw, list):
            return [str(x) for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    def configured(self) -> bool:
        return bool(self._token() and self._chat_ids())

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._token()),
            "configured": self.configured(),
            "chat_ids": self._chat_ids(),
        }

    def _register_owner(self, chat_id: str) -> None:
        self.alerts.save_config({"telegram": {"chat_id": chat_id}})

    # -- telegram http ----------------------------------------------------
    def _api(self, method: str, payload: Dict[str, Any]):
        token = self._token()
        if not token:
            return None
        url = f"{TELEGRAM_API}{token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _send(self, chat_id: str, text: str) -> Optional[Dict[str, Any]]:
        try:
            return self._api("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception:
            return None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _prime(self) -> None:
        # discard any pending updates so commands aren't replayed after a restart
        try:
            resp = self._api("getUpdates", {"offset": 0, "timeout": 0})
            if resp and resp.get("ok"):
                for upd in resp.get("result", []):
                    self._offset = upd.get("update_id", 0) + 1
        except Exception:
            pass

    def _loop(self) -> None:
        if self._token():
            self._prime()
        while not self._stop.is_set():
            if not self._token():
                self._stop.wait(self.poll_seconds)
                continue
            try:
                resp = self._api("getUpdates", {"offset": self._offset, "timeout": 20})
                if resp and resp.get("ok"):
                    for upd in resp.get("result", []):
                        self._offset = upd.get("update_id", 0) + 1
                        self._handle(upd)
            except Exception:
                self._stop.wait(self.poll_seconds)

    # -- command dispatch -------------------------------------------------
    def _handle(self, upd: Dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return

        allowed = self._chat_ids()
        if not allowed:
            # first person to talk to the bot becomes the owner
            self._register_owner(chat_id)
            self._send(chat_id, "👋 You are now the owner of this Arena Sports Bot.\n\n" + self._help())
            return
        if chat_id not in allowed:
            return  # ignore strangers

        if not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0].lower()

        if cmd in ("/start", "/help"):
            self._send(chat_id, self._help())
        elif cmd == "/status":
            self._send(chat_id, self._status())
        elif cmd in ("/summary", "/backtest"):
            self._send(chat_id, self._summary())
        elif cmd == "/positions":
            self._send(chat_id, self._positions())
        elif cmd == "/run":
            self._send(chat_id, self._run())
        else:
            self._send(chat_id, "Unknown command.\n\n" + self._help())

    # -- message builders -------------------------------------------------
    def _help(self) -> str:
        return (
            "🤖 Arena Sports Bot — commands:\n\n"
            "/status — wallet & bot status\n"
            "/summary — latest backtest result\n"
            "/positions — open positions\n"
            "/run — run a backtest now\n\n"
            "Tip: the same Telegram account gets your alerts."
        )

    def _status(self) -> str:
        s = self.engine.snapshot()
        a = s["account"]
        live = s.get("live") or {}
        cur = s.get("currency", "NGN")
        sym = NAIRA if cur == "NGN" else cur + " "

        state = "⏸ STOPPED"
        if a.get("halted"):
            state = "⛔ HALTED (daily limit)"
        elif s.get("running"):
            state = "🟢 TRADING"

        lines = [
            f"🤖 Arena Sports Bot — {state}",
            f"Tick: {s['tick']}",
            "",
            "📈 Paper wallet",
            f"Balance: {sym}{a['balance']:,.2f}",
            f"Equity:  {sym}{s['equity']:,.2f}",
            f"Realized P&L: {sym}{_f(a['realized_pnl'])}",
        ]
        # trade capital / reserved
        cap = s.get("capital") or {}
        if cap.get("allocated", 0) > 0:
            lines += [
                "",
                "🎯 Trade capital",
                f"Allocated: {sym}{cap['allocated']:,.2f}",
                f"In play:   {sym}{cap['in_play']:,.2f}",
                f"Available: {sym}{cap['available']:,.2f}",
                f"🔒 Reserved (untouched): {sym}{cap['reserved']:,.2f}",
            ]
        else:
            lines += [
                "",
                "🎯 Trade capital: full balance (nothing reserved)",
            ]
        if live.get("account"):
            lines += [
                "",
                "💼 Live (real) wallet [shadow]",
                f"Balance: {sym}{live['account']['balance']:,.2f}",
                f"Equity:  {sym}{live['equity']:,.2f}",
                f"Realized P&L: {sym}{_f(live['account']['realized_pnl'])}",
            ]
        lines += ["", f"Open positions: {len(s['positions'])}"]
        return "\n".join(lines)

    def _positions(self) -> str:
        s = self.engine.snapshot()
        cur = s.get("currency", "NGN")
        sym = NAIRA if cur == "NGN" else cur + " "
        if not s["positions"]:
            return "No open positions right now."
        lines = ["📊 Open positions:"]
        for p in s["positions"]:
            upnl = p["unrealized"]
            arrow = "▲" if upnl >= 0 else "▼"
            lines.append(
                f"\n{p['market_name']}\n"
                f"  lots {len(p['lots'])} · stake {sym}{p['total_stake']:,.2f} · "
                f"{arrow} {sym}{_f(upnl)}"
            )
        return "\n".join(lines)

    def _summary(self) -> str:
        # prefer the daily scheduled backtest, fall back to the on-demand report
        st = self.scheduler.status()
        summary, source = st.get("last_summary"), st.get("last_run")
        if summary:
            source = "Daily scheduled · " + source
        else:
            summary, source = self._load_on_demand()
        if not summary:
            return "No backtest yet. Run one from the dashboard or send /run."
        return self._format_summary(summary, source)

    def _format_summary(self, summary: Dict[str, Any], source: str) -> str:
        return (
            "📊 Backtest summary\n"
            f"Source: {source}\n"
            f"Runs: {summary.get('num_runs')} × {summary.get('ticks')} ticks\n\n"
            f"Mean P&L:      {NAIRA}{_f(summary.get('mean_pnl', 0))}\n"
            f"Median P&L:    {NAIRA}{_f(summary.get('median_pnl', 0))}\n"
            f"Profitable:    {summary.get('profitable_pct', 0):.0f}%\n"
            f"Worst drawdown: -{NAIRA}{summary.get('worst_drawdown', 0):,.0f} "
            f"({summary.get('worst_drawdown_pct', 0):.1f}%)"
        )

    # -- push / broadcast -------------------------------------------------
    def broadcast_morning_report(self, summary: Dict[str, Any], source: str) -> int:
        """Send the backtest summary + current status to all owner chat ids.
        Returns the number of chat ids the message was delivered to."""
        chat_ids = self._chat_ids()
        if not self._token() or not chat_ids:
            return 0
        s = self.engine.snapshot()
        a = s["account"]
        lines = [
            "☀️ Morning report — Arena Sports Bot",
            "",
            self._format_summary(summary, source),
            "",
            "📈 Wallet now",
            f"Equity: {NAIRA}{s['equity']:,.2f} · "
            f"Realized P&L: {NAIRA}{_f(a['realized_pnl'])} · "
            f"Open positions: {len(s['positions'])}",
        ]
        text = "\n".join(lines)
        sent = 0
        for cid in chat_ids:
            if self._send(cid, text):
                sent += 1
        return sent

    def _load_on_demand(self):
        p = os.path.join(self.data_dir, "backtest_report.json")
        if os.path.exists(p):
            try:
                with open(p) as f:
                    rep = json.load(f)
                agg = rep.get("aggregate", {})
                summary = {
                    "num_runs": agg.get("num_runs"),
                    "ticks": agg.get("ticks"),
                    "mean_pnl": agg["pnl"]["mean"],
                    "median_pnl": agg["pnl"]["median"],
                    "profitable_pct": agg["profitable_pct"],
                    "worst_drawdown": agg["worst_drawdown"],
                    "worst_drawdown_pct": agg["worst_drawdown_pct"],
                }
                return summary, "on-demand (" + (rep.get("generated") or "?") + ")"
            except Exception:
                pass
        return None, None

    def _run(self) -> str:
        if self.scheduler.status().get("running"):
            return "A backtest is already running — try again shortly."
        res = self.scheduler.run_now()
        if res.get("error"):
            return "Could not start backtest: " + res["error"]
        return self._summary()
