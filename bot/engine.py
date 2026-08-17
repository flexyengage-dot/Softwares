"""
Arena Sports Bot — core trading engine.

This is a *paper-trading* engine. It simulates live sports markets (odds that
move like a real bookmaker/exchange feed) and runs a risk-managed momentum
strategy against them.

Two ledgers run side by side so you can rehearse safely before going live:

  * PAPER wallet  — ideal execution (zero slippage, zero fees).
  * LIVE wallet   — a realistic shadow of "the real deal": it applies slippage
                    and commission, and remains clearly marked NOT CONNECTED
                    until you plug in a real broker/exchange API. Until then it
                    only mirrors the paper signals so you can see what real
                    results would look like (they will be slightly worse).

Strategy behaviour (maps 1:1 to what you asked for):
  * ENTER — open a position when the momentum signal crosses the entry bar.
  * ADD   — scale in ("add when it will succeed") when the signal strengthens.
  * EXIT  — "pull out" when the signal reverses (the model judges the outcome
            likely to fail) OR when a stop-loss / take-profit level is hit.

Important honesty note: no engine can *guarantee* profit. This module gives you
the machinery to define, back-test and paper-trade a strategy with real risk
controls. Real-money automation requires a platform that exposes a trading API.
"""

import json
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bot.alerts import AlertManager

CURRENCY = "NGN"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "currency": CURRENCY,
    "starting_balance": 3000.0,     # editable — top up anytime (simulated wallet)
    # -- risk controls (tuned via bot.tune for LOW drawdown) ----------------
    "max_stake_pct": 0.005,         # tiny lots (~min_stake) — "little wins"
    "max_open_positions": 1,        # one position at a time
    "max_adds": 0,                  # scaling-in OFF by default (backtests show it
                                    # increases drawdown without adding edge —
                                    # the feature is still available)
    "stop_loss_pct": 0.30,          # pull out at -30% of a stake
    "take_profit_pct": 0.25,        # small win target: +25% of stake
    "max_daily_loss_pct": 0.10,     # halt the bot after -10% of bankroll/day
    "margin": 0.03,                 # bookmaker margin baked into back/lay odds
    # -- strategy (momentum) ------------------------------------------------
    "entry_signal": 0.10,           # prob. points above slow EMA to open (selective)
    "add_signal": 0.14,             # scale-in threshold (only if max_adds > 0)
    "exit_signal": 0.02,            # momentum stalled -> pull out
    "ema_slow": 15,                 # slow EMA window (ticks)
    # -- engine / simulation ------------------------------------------------
    "tick_seconds": 2.0,            # seconds between engine ticks
    "market_lifetime": 90,          # ticks before a market "settles"
    "market_count": 12,             # live markets in the simulated feed
    "market_momentum": 0.55,        # simulated market trending (see README honesty note)
    "min_stake": 20.0,              # smallest single lot, in NGN
    "trade_capital": 0.0,           # ₦ the bot may trade with (0 = full balance).
                                    # YOU allocate this manually; the rest of your
                                    # balance is reserved and never touched by the
                                    # strategy. Stake sizing & the daily loss limit
                                    # are based on this amount, not the full balance.
    # -- live (real) ledger shadow ----------------------------------------
    "live_mode": "shadow",          # "shadow" = mirror with slippage+fees; "off" = disable
    "live_slippage": 0.01,          # odds degradation on fills (1%)
    "live_commission_pct": 0.05,    # exchange-style fee on net winnings (5%)
    # -- scheduled daily backtest ------------------------------------------
    "daily_backtest_enabled": False,  # re-check the strategy once a day
    "daily_backtest_hour": 8,         # server-local hour (0-23)
    "daily_backtest_minute": 0,
    "daily_backtest_runs": 50,
    "daily_backtest_ticks": 600,
}

LIVE_MODES = ("shadow", "off")


# --- odds helpers ---------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _r2(v: float) -> float:
    return round(v, 2)


def back_odds(prob: float, margin: float) -> float:
    """Odds to BACK a selection at probability `prob` (back < lay on an exchange)."""
    p = _clamp(prob, 0.08, 0.92)
    return _r2(1.0 / (p * (1.0 + margin)))


def lay_odds(prob: float, margin: float) -> float:
    """Odds to LAY (trade out) the same selection (lay > back)."""
    p = _clamp(prob, 0.08, 0.92)
    return _r2(1.0 / (p * (1.0 - margin)))


def ema(values: List[float], window: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (window + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


# --- market simulator -----------------------------------------------------

MARKET_NAMES = [
    "Arsenal vs Chelsea - Over 2.5",
    "Man City vs Liverpool - Both Teams Score",
    "Real Madrid vs Barcelona - Home Win",
    "Juventus vs Inter - Under 3.5",
    "PSG vs Marseille - Over 1.5",
    "Bayern vs Dortmund - Away Win",
    "Tottenham vs Man Utd - Draw No Bet",
    "Atletico vs Sevilla - Under 2.5",
    "Napoli vs Roma - Over 2.5",
    "Benfica vs Porto - Home Win",
    "Ajax vs PSV - Both Teams Score",
    "Celtic vs Rangers - Over 1.5",
]


@dataclass
class Market:
    id: str
    name: str
    prob: float
    margin: float
    lifetime: int
    history: List[float] = field(default_factory=list)
    ticks: int = 0
    settled: bool = False
    result: Optional[bool] = None

    def __post_init__(self):
        # seed enough history so the EMA is stable at t=0
        for _ in range(24):
            self.history.append(self.prob)

    @property
    def back(self) -> float:
        return back_odds(self.prob, self.margin)

    @property
    def lay(self) -> float:
        return lay_odds(self.prob, self.margin)

    def slow_ema(self, window: int) -> float:
        return ema(self.history, window)

    def signal(self, ema_slow: int) -> float:
        """Momentum = current implied prob - slow EMA (probability points)."""
        return self.prob - self.slow_ema(ema_slow)

    def step(self, momentum_coef: float = 0.55) -> None:
        if self.settled:
            return
        # Price series with momentum persistence (real prices do trend)
        # plus gentle mean reversion back to 0.5.
        if not hasattr(self, "_last_change"):
            self._last_change = 0.0
        drift = (0.5 - self.prob) * 0.01
        momentum = momentum_coef * self._last_change
        change = drift + momentum + random.gauss(0, 0.02)
        self.prob = _clamp(self.prob + change, 0.08, 0.92)
        self._last_change = change
        self.history.append(self.prob)
        self.ticks += 1
        if self.ticks >= self.lifetime:
            self.settle()

    def settle(self) -> None:
        if self.settled:
            return
        self.settled = True
        self.result = random.random() < self.prob

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prob": round(self.prob, 4),
            "back": self.back,
            "lay": self.lay,
            "ticks": self.ticks,
            "lifetime": self.lifetime,
            "settled": self.settled,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Market":
        m = cls(d.get("id", ""), d.get("name", ""), d.get("prob", 0.5),
                d.get("margin", 0.03), d.get("lifetime", 90))
        m.history = d.get("history") or [m.prob] * 24
        m.ticks = d.get("ticks", 0)
        m.settled = d.get("settled", False)
        m.result = d.get("result")
        return m


# --- positions & trades ---------------------------------------------------


@dataclass
class Lot:
    back: float       # odds the lot was backed at
    stake: float      # NGN staked


@dataclass
class Position:
    market_id: str
    market_name: str
    lots: List[Lot] = field(default_factory=list)
    opened_tick: int = 0

    def total_stake(self) -> float:
        return sum(l.stake for l in self.lots)

    def unrealized(self, lay: float) -> float:
        """Green-up P&L if we trade out (lay off) every lot right now."""
        return sum(l.stake * (l.back / lay - 1.0) for l in self.lots)

    def to_dict(self, lay: float) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "market_name": self.market_name,
            "lots": [{"back": l.back, "stake": l.stake} for l in self.lots],
            "total_stake": _r2(self.total_stake()),
            "avg_back": _r2(sum(l.back * l.stake for l in self.lots) / max(self.total_stake(), 1e-9)),
            "unrealized": _r2(self.unrealized(lay)),
            "opened_tick": self.opened_tick,
        }


@dataclass
class Trade:
    id: int
    ts: str
    action: str        # OPEN / ADD / EXIT / SETTLE
    market_name: str
    side: str          # BACK
    odds: float
    stake: float
    pnl: float
    balance_after: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "action": self.action,
            "market_name": self.market_name,
            "side": self.side,
            "odds": self.odds,
            "stake": _r2(self.stake),
            "pnl": _r2(self.pnl),
            "balance_after": _r2(self.balance_after),
        }


# --- account --------------------------------------------------------------


@dataclass
class Account:
    balance: float = 3000.0
    deposits: float = 0.0
    realized_pnl: float = 0.0
    day_start_balance: float = 3000.0
    day: str = ""
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance": _r2(self.balance),
            "deposits": _r2(self.deposits),
            "realized_pnl": _r2(self.realized_pnl),
            "day_start_balance": _r2(self.day_start_balance),
            "day": self.day,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


# --- engine ---------------------------------------------------------------


class Engine:
    def __init__(self, state_path: Optional[str], settings: Optional[Dict[str, Any]] = None,
                 alerts_on: bool = True, alert_config_path: Optional[str] = None):
        self.state_path = state_path
        self.settings = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update(settings)

        start = self.settings["starting_balance"]
        self.account = Account(balance=start, day_start_balance=start, day=time.strftime("%Y-%m-%d"))
        self.markets: Dict[str, Market] = {}
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_history: List[Dict[str, Any]] = []
        self.trade_seq = 0
        self.tick_count = 0
        self.running = True
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # live (real) ledger shadow
        self.live_account: Optional[Account] = None
        self.live_trades: List[Trade] = []
        self._live_positions: Dict[str, Position] = {}
        if self.settings.get("live_mode", "shadow") != "off":
            self.live_account = Account(balance=start, day_start_balance=start,
                                        day=time.strftime("%Y-%m-%d"))

        # alerts
        self.alerts: Optional[AlertManager] = None
        if alerts_on:
            acp = alert_config_path or (
                os.path.join(os.path.dirname(state_path), "alert_config.json") if state_path else None)
            self.alerts = AlertManager(acp)

        self._last_milestone_bucket = 0
        # decision desk: proposals awaiting YOUR approval (never auto-executed)
        self.proposals: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self._proposal_seq = 0
        self._signal_high: set = set()  # markets above entry last tick (rising-edge detect)

        self._seed_markets()
        if state_path and os.path.exists(state_path):
            self._load()

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        if not self.state_path:
            return
        with self.lock:
            data = {
                "settings": self.settings,
                "account": self.account.to_dict(),
                "live_account": self.live_account.to_dict() if self.live_account else None,
                "markets": {k: m.to_dict() for k, m in self.markets.items()},
                "positions": {k: p.to_dict(self._lay(k)) for k, p in self.positions.items()},
                "live_positions": {k: p.to_dict(self._lay(k)) for k, p in self._live_positions.items()},
                "trades": [t.to_dict() for t in self.trades[-500:]],
                "live_trades": [t.to_dict() for t in self.live_trades[-500:]],
                "equity_history": self.equity_history[-600:],
                "trade_seq": self.trade_seq,
                "tick_count": self.tick_count,
                "running": self.running,
                "proposals": self.proposals[-50:],
                "decisions": self.decisions[-100:],
                "proposal_seq": self._proposal_seq,
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)

    def _load(self) -> None:
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.settings.update(data.get("settings", {}))
            a = data.get("account", {})
            self.account = Account(
                balance=a.get("balance", 3000.0),
                deposits=a.get("deposits", 0.0),
                realized_pnl=a.get("realized_pnl", 0.0),
                day_start_balance=a.get("day_start_balance", 3000.0),
                day=a.get("day", time.strftime("%Y-%m-%d")),
                halted=a.get("halted", False),
                halt_reason=a.get("halt_reason", ""),
            )
            la = data.get("live_account")
            self.live_account = self._account_from_dict(la) if la else None
            self.equity_history = data.get("equity_history", [])
            self.trade_seq = data.get("trade_seq", 0)
            self.tick_count = data.get("tick_count", 0)
            self.running = data.get("running", True)
            self.trades = [self._trade_from_dict(t) for t in data.get("trades", [])]
            self.live_trades = [self._trade_from_dict(t) for t in data.get("live_trades", [])]
            self.proposals = data.get("proposals", [])
            self.decisions = data.get("decisions", [])
            self._proposal_seq = data.get("proposal_seq", 0)
            self.markets = {k: Market.from_dict(m) for k, m in data.get("markets", {}).items()}
            if not self.markets:
                self._seed_markets()
            self.positions = {}
            for pid, p in data.get("positions", {}).items():
                if pid in self.markets:
                    self.positions[pid] = self._pos_from_dict(p)
            self._live_positions = {}
            for pid, p in data.get("live_positions", {}).items():
                if pid in self.markets:
                    self._live_positions[pid] = self._pos_from_dict(p)
        except Exception:
            # corrupted/partial state -> keep defaults and fall through
            pass
        # reconcile live ledger (always run, regardless of load success)
        if self.settings.get("live_mode", "shadow") == "off":
            self.live_account = None
            self._live_positions = {}
            self.live_trades = []
        elif self.live_account is None:
            self.live_account = Account(balance=self.settings["starting_balance"],
                                        day_start_balance=self.settings["starting_balance"],
                                        day=time.strftime("%Y-%m-%d"))

    def _account_from_dict(self, a: Dict[str, Any]) -> Account:
        return Account(
            balance=a.get("balance", 3000.0),
            deposits=a.get("deposits", 0.0),
            realized_pnl=a.get("realized_pnl", 0.0),
            day_start_balance=a.get("day_start_balance", 3000.0),
            day=a.get("day", time.strftime("%Y-%m-%d")),
            halted=a.get("halted", False),
            halt_reason=a.get("halt_reason", ""),
        )

    def _pos_from_dict(self, p: Dict[str, Any]) -> Position:
        lots = [Lot(l["back"], l["stake"]) for l in p.get("lots", [])]
        return Position(p.get("market_id", ""), p.get("market_name", ""), lots, p.get("opened_tick", 0))

    def _trade_from_dict(self, d: Dict[str, Any]) -> Trade:
        return Trade(
            d["id"], d["ts"], d["action"], d["market_name"], d["side"],
            d["odds"], d["stake"], d["pnl"], d["balance_after"],
        )

    # -- markets ----------------------------------------------------------
    def _seed_markets(self) -> None:
        n = int(self.settings.get("market_count", 12))
        names = MARKET_NAMES[:n] + MARKET_NAMES[:(max(0, n - len(MARKET_NAMES)))]
        for i, name in enumerate(names):
            mid = f"mkt-{i}"
            if mid not in self.markets:
                self.markets[mid] = Market(
                    id=mid, name=name, prob=random.uniform(0.35, 0.65),
                    margin=self.settings["margin"],
                    lifetime=int(self.settings["market_lifetime"]),
                )

    def _lay(self, market_id: str) -> float:
        m = self.markets.get(market_id)
        return m.lay if m else 2.0

    # -- control ----------------------------------------------------------
    def start(self) -> None:
        with self.lock:
            self.running = True
            self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self._stop.set()

    def reset(self) -> None:
        with self.lock:
            self.stop()
            bal = self.settings["starting_balance"]
            self.account = Account(balance=bal, day_start_balance=bal, day=time.strftime("%Y-%m-%d"))
            self.live_account = (Account(balance=bal, day_start_balance=bal, day=time.strftime("%Y-%m-%d"))
                                 if self.settings.get("live_mode", "shadow") != "off" else None)
            self.markets = {}
            self.positions = {}
            self._live_positions = {}
            self.trades = []
            self.live_trades = []
            self.equity_history = []
            self.trade_seq = 0
            self.tick_count = 0
            self._last_milestone_bucket = 0
            self.proposals = []
            self.decisions = []
            self._proposal_seq = 0
            self._signal_high = set()
            self._seed_markets()
            self.save()

    def deposit(self, amount: float, target: str = "paper") -> Dict[str, Any]:
        amount = _r2(max(0.0, amount))
        with self.lock:
            if target in ("paper", "both"):
                self.account.balance += amount
                self.account.deposits += amount
                self.account.day_start_balance += amount
            if target in ("live", "both") and self.live_account:
                self.live_account.balance += amount
                self.live_account.deposits += amount
                self.live_account.day_start_balance += amount
            self.save()
            return self.snapshot()

    def withdraw(self, amount: float, target: str = "paper") -> Dict[str, Any]:
        """Debit the (simulated) wallet. Guarded so it can never overdraw."""
        amount = _r2(max(0.0, amount))
        with self.lock:
            if target in ("paper", "both"):
                amount = min(amount, self.account.balance)
                self.account.balance -= amount
            if target in ("live", "both") and self.live_account:
                lamount = min(amount, self.live_account.balance)
                self.live_account.balance -= lamount
            self.save()
            return self.snapshot()

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            allowed = set(DEFAULT_SETTINGS) - {"currency"}
            bools = {"daily_backtest_enabled"}
            ints = {"tick_seconds", "market_lifetime", "market_count",
                    "max_adds", "max_open_positions", "ema_slow",
                    "daily_backtest_hour", "daily_backtest_minute",
                    "daily_backtest_runs", "daily_backtest_ticks"}
            for k, v in patch.items():
                if k not in allowed:
                    continue
                try:
                    if k in bools:
                        self.settings[k] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
                    elif k == "starting_balance":
                        self.settings[k] = max(0.0, float(v))
                    elif k in ints:
                        self.settings[k] = max(1, int(float(v)))
                    elif k == "live_mode":
                        if str(v) in LIVE_MODES:
                            self.settings[k] = str(v)
                    else:
                        self.settings[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
            # clamp hour/minute into valid ranges
            self.settings["daily_backtest_hour"] = max(0, min(23, int(self.settings["daily_backtest_hour"])))
            self.settings["daily_backtest_minute"] = max(0, min(59, int(self.settings["daily_backtest_minute"])))
            # reconcile live ledger after a mode change
            if self.settings.get("live_mode", "shadow") == "off":
                self.live_account = None
                self._live_positions = {}
                self.live_trades = []
            elif self.live_account is None:
                self.live_account = Account(balance=self.settings["starting_balance"],
                                            day_start_balance=self.settings["starting_balance"],
                                            day=time.strftime("%Y-%m-%d"))
            self.save()
            return self.snapshot()

    # -- alerts -----------------------------------------------------------
    def _emit(self, level: str, title: str, message: str) -> None:
        if self.alerts:
            self.alerts.emit(level, title, message)

    def _check_milestone(self) -> None:
        start = self.account.day_start_balance
        if start <= 0:
            return
        eq = self.equity()
        pct = (eq - start) / start
        bucket = int(pct * 10)  # 10% steps
        if bucket != self._last_milestone_bucket and bucket != 0:
            self._last_milestone_bucket = bucket
            arrow = "\u25b2" if bucket > 0 else "\u25bc"
            self._emit("notice", "Equity milestone",
                       f"{arrow} Equity {eq:,.2f} ({pct*100:+.1f}% vs day start {start:,.2f})")

    # -- decision desk -----------------------------------------------------
    def _emit_proposals(self) -> None:
        """Surface a proposal when a market's momentum RISES across the entry
        threshold (rising edge). Independent of the paper auto-trader, so the
        decision desk always shows live opportunities. Proposals await YOUR
        decision — they are never auto-executed."""
        ema_slow = int(self.settings["ema_slow"])
        entry = self.settings["entry_signal"]
        high = set()
        for mid, m in self.markets.items():
            if m.settled:
                continue
            sig = m.signal(ema_slow)
            is_high = sig >= entry
            if is_high:
                high.add(mid)
            # rising edge: was not above entry last tick, is now
            if is_high and mid not in self._signal_high:
                if any(p["market_id"] == mid and p["status"] == "pending" for p in self.proposals):
                    continue
                stake = self._stake()
                if stake <= 0:
                    continue
                prob = m.prob
                odds = m.back
                ev = stake * (prob * odds - 1.0)   # honest expected value (₦)
                self._proposal_seq += 1
                self.proposals.append({
                    "id": self._proposal_seq,
                    "ts": time.strftime("%H:%M:%S"),
                    "market_id": mid,
                    "market_name": m.name,
                    "side": "BACK",
                    "odds": odds,
                    "prob": round(prob, 4),
                    "signal": round(sig, 4),
                    "confidence": round(_clamp(0.5 + sig * 20.0, 0.0, 1.0), 3),
                    "ev": _r2(ev),
                    "ev_pct": round((prob * odds - 1.0) * 100.0, 2),
                    "stake": stake,
                    "status": "pending",
                })
        self._signal_high = high
        if len(self.proposals) > 50:
            self.proposals = self.proposals[-50:]

    def approve_proposal(self, pid: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            for p in self.proposals:
                if p["id"] == pid and p["status"] == "pending":
                    p["status"] = "approved"
                    self.decisions.append({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "id": pid,
                        "market": p["market_name"], "action": "approved",
                        "odds": p["odds"], "stake": p["stake"], "ev": p["ev"],
                    })
                    self.save()
                    return dict(p)
        return None

    def reject_proposal(self, pid: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            for p in self.proposals:
                if p["id"] == pid and p["status"] == "pending":
                    p["status"] = "rejected"
                    self.decisions.append({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "id": pid,
                        "market": p["market_name"], "action": "rejected",
                        "odds": p["odds"], "stake": p["stake"], "ev": p["ev"],
                    })
                    self.save()
                    return dict(p)
        return None

    def mark_executed(self, pid: int, result: Dict[str, Any]) -> None:
        with self.lock:
            for p in self.proposals:
                if p["id"] == pid:
                    p["status"] = "executed" if result.get("ok") else "failed"
                    p["result"] = result
                    self.decisions.append({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "id": pid,
                        "market": p["market_name"],
                        "action": "executed" if result.get("ok") else "failed",
                        "odds": p["odds"], "stake": p["stake"], "ev": p["ev"],
                        "detail": (result.get("symbol", "") + " " + result.get("side", "")
                                   if result.get("ok") else result.get("error", "")),
                    })
                    self.save()

    # -- execution --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                if self.running and not self.account.halted:
                    self.tick()
                    self.save()
            self._stop.wait(self.settings["tick_seconds"])

    def tick(self) -> None:
        self.tick_count += 1
        self._roll_day()

        # 1. advance & settle markets
        mom = self.settings.get("market_momentum", 0.55)
        for mid, m in list(self.markets.items()):
            m.step(mom)
            if m.settled:
                self._settle_position(mid, m)
                # respawn a fresh market in the same slot
                self.markets[mid] = Market(
                    id=mid, name=m.name, prob=random.uniform(0.35, 0.65),
                    margin=self.settings["margin"],
                    lifetime=int(self.settings["market_lifetime"]),
                )

        # 2. act on live markets
        ema_slow = int(self.settings["ema_slow"])
        for mid, m in self.markets.items():
            if m.settled:
                continue
            sig = m.signal(ema_slow)
            pos = self.positions.get(mid)

            if pos is None:
                if (sig >= self.settings["entry_signal"] and self._can_trade()
                        and len(self.positions) < int(self.settings["max_open_positions"])):
                    self._open(m, sig)
            else:
                upnl = pos.unrealized(m.lay)
                stop = -self.settings["stop_loss_pct"] * pos.total_stake()
                take = self.settings["take_profit_pct"] * pos.total_stake()
                exit_now = (sig <= self.settings["exit_signal"]) or (upnl <= stop) or (upnl >= take)
                if exit_now:
                    self._exit(m, pos, upnl)
                elif (sig >= self.settings["add_signal"]
                      and len(pos.lots) <= int(self.settings["max_adds"])
                      and self._can_trade()):
                    self._add(m, pos, sig)

        # 3. daily loss circuit breaker — the loss allowance is a fraction of the
        #    ALLOCATED trade capital (not the full balance), so your reserved funds
        #    are never part of the risk budget.
        loss_pct_limit = self.settings["max_daily_loss_pct"]
        capital = self._capital()
        if (capital > 0
                and self.equity() <= self.account.day_start_balance - capital * loss_pct_limit
                and not self.account.halted):
            self.account.halted = True
            self.account.halt_reason = (
                f"Daily loss limit hit ({int(loss_pct_limit * 100)}% of trade capital). "
                f"Bot paused for the day."
            )
            self._emit("critical", "Bot halted", self.account.halt_reason)

        # 4. record equity + milestones
        self.equity_history.append({
            "tick": self.tick_count,
            "ts": time.strftime("%H:%M:%S"),
            "equity": _r2(self.equity()),
            "balance": _r2(self.account.balance),
        })
        self._check_milestone()

        # 5. decision desk — surface signals for your approval
        self._emit_proposals()

    def _settle_position(self, mid: str, m: Market) -> None:
        pos = self.positions.pop(mid, None)
        if pos:
            for lot in pos.lots:
                if m.result:
                    cash_back = lot.stake * lot.back
                    pnl = lot.stake * (lot.back - 1.0)
                else:
                    cash_back = 0.0
                    pnl = -lot.stake
                self.account.balance += cash_back
                self.account.realized_pnl += pnl
                self._log(Trade(self._next_id(), time.strftime("%H:%M:%S"), "SETTLE",
                                m.name, "BACK", lot.back, lot.stake, pnl, self.account.balance))
            self._emit("notice", "Position settled",
                       f"{m.name}: {'WON' if m.result else 'LOST'} — "
                       f"stake {pos.total_stake():,.2f}, P&L {pos.unrealized(1.0) if not m.result else 0:+,.2f}")
        # live shadow settle
        lpos = self._live_positions.pop(mid, None)
        if lpos and self.live_account:
            for lot in lpos.lots:
                if m.result:
                    gross = lot.stake * (lot.back - 1.0)
                    comm = self._live_comm(max(0.0, gross))
                    pnl = gross - comm
                    cash_back = lot.stake * lot.back - comm
                else:
                    pnl = -lot.stake
                    cash_back = 0.0
                self.live_account.balance += cash_back
                self.live_account.realized_pnl += pnl
                self.live_trades.append(Trade(self._next_id(), time.strftime("%H:%M:%S"), "SETTLE",
                                              m.name, "BACK", lot.back, lot.stake, pnl,
                                              self.live_account.balance))

    def _open(self, m: Market, sig: float) -> None:
        stake = self._stake()
        if stake <= 0:
            return
        pos = Position(m.id, m.name, [Lot(m.back, stake)], self.tick_count)
        self.positions[m.id] = pos
        self.account.balance -= stake
        self._log(Trade(self._next_id(), time.strftime("%H:%M:%S"), "OPEN",
                        m.name, "BACK", m.back, stake, 0.0, self.account.balance))
        self._emit("info", "Position opened", f"{m.name}: BACK @ {m.back} · stake {stake:,.2f}")
        self._live_open(m, stake)

    def _add(self, m: Market, pos: Position, sig: float) -> None:
        stake = self._stake()
        if stake <= 0:
            return
        pos.lots.append(Lot(m.back, stake))
        self.account.balance -= stake
        self._log(Trade(self._next_id(), time.strftime("%H:%M:%S"), "ADD",
                        m.name, "BACK", m.back, stake, 0.0, self.account.balance))
        self._emit("info", "Position scaled in", f"{m.name}: ADD @ {m.back} · stake {stake:,.2f}")
        self._live_open(m, stake)

    def _exit(self, m: Market, pos: Position, upnl: float) -> None:
        total = pos.total_stake()
        green_up = pos.unrealized(m.lay)
        self.positions.pop(m.id, None)
        self.account.balance += total + green_up
        self.account.realized_pnl += green_up
        self._log(Trade(self._next_id(), time.strftime("%H:%M:%S"), "EXIT",
                        m.name, "BACK", m.lay, total, green_up, self.account.balance))
        self._emit("notice", "Position closed",
                   f"{m.name}: trade out @ {m.lay} · P&L {green_up:+,.2f}")
        self._live_exit(m)

    # -- live (real) shadow ledger ---------------------------------------
    def _live_back(self, odds: float) -> float:
        return max(1.01, _r2(odds * (1.0 - self.settings["live_slippage"])))

    def _live_lay(self, odds: float) -> float:
        return max(1.01, _r2(odds * (1.0 + self.settings["live_slippage"])))

    def _live_comm(self, gross_profit: float) -> float:
        # exchange-style: fee only on net winnings
        return _r2(max(0.0, gross_profit) * self.settings["live_commission_pct"])

    def _live_open(self, m: Market, stake: float) -> None:
        if not self.live_account:
            return
        odds = self._live_back(m.back)
        lp = self._live_positions.get(m.id)
        if lp is None:
            lp = Position(m.id, m.name, [], self.tick_count)
            self._live_positions[m.id] = lp
        lp.lots.append(Lot(odds, stake))
        self.live_account.balance -= stake
        self.live_trades.append(Trade(self._next_id(), time.strftime("%H:%M:%S"), "OPEN",
                                      m.name, "BACK", odds, stake, 0.0, self.live_account.balance))

    def _live_exit(self, m: Market) -> None:
        lpos = self._live_positions.pop(m.id, None)
        if not lpos or not self.live_account:
            return
        lay = self._live_lay(m.lay)
        total = lpos.total_stake()
        gross = sum(l.stake * (l.back / lay - 1.0) for l in lpos.lots)
        comm = self._live_comm(gross)
        net = gross - comm
        self.live_account.balance += total + net
        self.live_account.realized_pnl += net
        self.live_trades.append(Trade(self._next_id(), time.strftime("%H:%M:%S"), "EXIT",
                                      m.name, "BACK", lay, total, net, self.live_account.balance))

    # -- helpers ----------------------------------------------------------
    def _capital(self) -> float:
        """Effective trade capital: the allocated amount (or full balance if 0)."""
        tc = self.settings.get("trade_capital", 0.0)
        if tc and tc > 0:
            return min(tc, self.account.balance)
        return self.account.balance

    def _total_open_stake(self) -> float:
        return sum(p.total_stake() for p in self.positions.values())

    def _stake(self) -> float:
        capital = self._capital()
        remaining = capital - self._total_open_stake()
        if remaining < self.settings["min_stake"]:
            return 0.0
        raw = max(self.settings["min_stake"], capital * self.settings["max_stake_pct"])
        return _r2(min(raw, remaining))

    def _can_trade(self) -> bool:
        if self.account.halted:
            return False
        if self.account.balance < self.settings["min_stake"]:
            return False
        return self._capital() - self._total_open_stake() >= self.settings["min_stake"]

    def _log(self, t: Trade) -> None:
        self.trades.append(t)

    def _next_id(self) -> int:
        self.trade_seq += 1
        return self.trade_seq

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.account.day != today:
            self.account.day = today
            self.account.day_start_balance = self.account.balance
            self.account.halted = False
            self.account.halt_reason = ""
            self._last_milestone_bucket = 0
        if self.live_account and self.live_account.day != today:
            self.live_account.day = today
            self.live_account.day_start_balance = self.live_account.balance

    # -- reporting --------------------------------------------------------
    def equity(self) -> float:
        # account value = cash + market value of open positions.
        # stake was deducted from cash at open, so add it back along with P&L.
        bal = self.account.balance
        for mid, pos in self.positions.items():
            bal += pos.total_stake() + pos.unrealized(self._lay(mid))
        return bal

    def live_equity(self) -> float:
        if not self.live_account:
            return 0.0
        bal = self.live_account.balance
        for mid, pos in self._live_positions.items():
            bal += pos.total_stake() + pos.unrealized(self._lay(mid))
        return bal

    def stats(self, trades: Optional[List[Trade]] = None, account: Optional[Account] = None) -> Dict[str, Any]:
        trades = trades if trades is not None else self.trades
        account = account if account is not None else self.account
        exits = [t for t in trades if t.action == "EXIT"]
        wins = [t for t in exits if t.pnl > 0]
        return {
            "trades": len(trades),
            "closed": len(exits),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(exits), 3) if exits else 0.0,
            "total_pnl": _r2(account.realized_pnl),
            "deposits": _r2(account.deposits),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            ema_slow = int(self.settings["ema_slow"])
            markets = []
            for m in self.markets.values():
                if m.settled:
                    continue
                sig = m.signal(ema_slow)
                markets.append({
                    **m.to_dict(),
                    "signal": round(sig, 4),
                    "confidence": round(_clamp(0.5 + sig * 20.0, 0.0, 1.0), 3),
                    "has_position": m.id in self.positions,
                })
            markets.sort(key=lambda x: -x["confidence"])
            positions = [p.to_dict(self._lay(p.market_id)) for p in self.positions.values()]
            live_positions = [p.to_dict(self._lay(p.market_id)) for p in self._live_positions.values()]
            return {
                "currency": self.settings["currency"],
                "running": self.running,
                "tick": self.tick_count,
                "account": self.account.to_dict(),
                "stats": self.stats(),
                "equity": _r2(self.equity()),
                "equity_history": self.equity_history[-400:],
                "capital": {
                    "allocated": _r2(self.settings.get("trade_capital", 0.0)),
                    "effective": _r2(self._capital()),
                    "in_play": _r2(self._total_open_stake()),
                    "available": _r2(max(0.0, self._capital() - self._total_open_stake())),
                    "reserved": _r2(max(0.0, self.account.balance - self._capital())),
                },
                "settings": self.settings,
                "markets": markets,
                "positions": positions,
                "trades": [t.to_dict() for t in self.trades[-100:]][::-1],
                "live": {
                    "mode": self.settings.get("live_mode", "shadow"),
                    "connected": False,  # no broker adapter yet — shadow only
                    "account": self.live_account.to_dict() if self.live_account else None,
                    "equity": _r2(self.live_equity()),
                    "stats": self.stats(self.live_trades, self.live_account) if self.live_account else None,
                    "positions": live_positions,
                    "trades": [t.to_dict() for t in self.live_trades[-100:]][::-1],
                },
                "alerts": self.alerts.status() if self.alerts else [],
                "proposals": list(reversed(self.proposals[-30:])),
                "decisions": list(reversed(self.decisions[-50:])),
            }
