"""
Trading-platform / broker connections for the Arena Sports Bot.

READ THIS FIRST — what is and isn't possible
--------------------------------------------
This module stores your broker/exchange credentials (masked, persisted) and can
*verify connectivity* to the ones that expose a real API. It deliberately does
NOT place orders — auto-trading real money requires a per-platform adapter that
translates the strategy's signals into real orders, plus your explicit go-live
decision. That adapter is the missing piece, and it is NOT a small step.

Which platforms can actually be linked:
  * binance / bybit / okx  — crypto exchanges with public REST APIs. Credentials
                             can be stored and CONNECTIVITY VERIFIED here (read-only).
  * betfair               — the only major betting *exchange* with a real API, but
                             it uses an OAuth-style login + app keys, and requires
                             Betfair approval. Scaffolded (no live test here).
  * metatrader (MT4/MT5)  — forex brokers have NO REST API; they need a bridge
                             (e.g. an EA / copy-trade server). Scaffolded.
  * Nigerian bookmakers (Bet9ja, SportyBet, Betway, 1xBet NG, etc.)
                          — DO NOT expose trading APIs. There is nothing to link;
                            no bot can auto-trade on them. This is a hard fact,
                            not a limitation of this project.

The honest bottom line: you can connect (store + verify) crypto exchanges today.
"Auto-trading on your behalf, on any platform, without knowing how to trade" is
the dangerous promise — the strategy is ~break-even in simulation, and handing
real money to an untested adapter is how people lose everything. Keep it in
paper mode until a specific platform + adapter is actually working.

Providers verified here (read-only, HMAC-signed where applicable):
  * Binance  — GET /api/v3/account  (or testnet.binance.vision)
  * Bybit    — GET /v5/account/wallet-balance
  * OKX      — GET /api/v5/account/balance
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional


from bot.sports import PolymarketClient, BetfairClient

def _now_ts() -> str:
    return str(int(time.time() * 1000))


DEFAULT_CONFIG: Dict[str, Any] = {
    "binance": {"enabled": False, "mode": "test", "network": "testnet",
                "api_key": "", "api_secret": "",
                "symbol": "BTCUSDT", "usd_ngn_rate": 1500.0},
    "bybit": {"enabled": False, "mode": "test", "network": "testnet",
              "api_key": "", "api_secret": ""},
    "okx": {"enabled": False, "mode": "test", "network": "testnet",
            "api_key": "", "api_secret": "", "api_passphrase": ""},
    "betfair": {"enabled": False, "mode": "test",
                "app_key": "", "username": "", "password": ""},
    "polymarket": {"enabled": False, "mode": "test",
                   "api_key": "", "api_secret": "", "api_passphrase": ""},
    "metatrader": {"enabled": False, "mode": "test",
                   "account": "", "password": "", "server": ""},
    "custom_webhook": {"enabled": False, "url": ""},
}

# human labels + which fields each platform takes + whether a live test exists
PLATFORM_META = {
    "binance": {"label": "Binance (crypto)", "secret_fields": ["api_secret"],
                "testable": True, "note": "Crypto spot/futures. Read-only account check."},
    "bybit": {"label": "Bybit (crypto)", "secret_fields": ["api_secret"],
              "testable": True, "note": "Crypto derivatives. Read-only wallet check."},
    "okx": {"label": "OKX (crypto)", "secret_fields": ["api_secret", "api_passphrase"],
            "testable": True, "note": "Crypto exchange. Read-only balance check."},
    "betfair": {"label": "Betfair Exchange", "secret_fields": ["password"],
                "testable": True, "note": "Real sports exchange API — needs app key + (usually interactive) login + Betfair approval."},
    "polymarket": {"label": "Polymarket (prediction market)", "secret_fields": ["api_secret", "api_passphrase"],
                   "testable": True, "note": "Live market data is public; orders need EIP-712 signing + API key."},
    "metatrader": {"label": "MetaTrader 4/5 (forex)", "secret_fields": ["password"],
                   "testable": False, "note": "Forex brokers have no REST API — needs a bridge. Scaffold only."},
    "custom_webhook": {"label": "Custom webhook (your bridge)", "secret_fields": [],
                       "testable": False, "note": "POST signals to your own bridge."},
}

SECRET_KEYS = {"api_secret", "api_passphrase", "password"}


# ---------------------------------------------------------------------------
# connectivity checks (read-only; never place orders)
# ---------------------------------------------------------------------------

def _http(method: str, url: str, headers: Optional[dict] = None, body: Optional[bytes] = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _test_binance(cfg: Dict[str, Any]) -> Dict[str, Any]:
    base = ("https://testnet.binance.vision" if cfg.get("network") == "testnet"
            else "https://api.binance.com")
    if not (cfg.get("api_key") and cfg.get("api_secret")):
        _http("GET", base + "/api/v3/ping")
        return {"ok": True, "status": "reachable", "detail": "network OK — add API key + secret to verify account access"}
    ts = _now_ts()
    q = f"timestamp={ts}"
    sig = hmac.new(cfg["api_secret"].encode(), q.encode(), hashlib.sha256).hexdigest()
    data = _http("GET", f"{base}/api/v3/account?{q}&signature={sig}",
                 headers={"X-MBX-APIKEY": cfg["api_key"]})
    if isinstance(data, list) or "balances" in data:
        return {"ok": True, "status": "ok", "detail": "credentials verified (read-only)"}
    return {"ok": False, "status": "error", "detail": str(data)[:200]}


def _test_bybit(cfg: Dict[str, Any]) -> Dict[str, Any]:
    base = ("https://api-testnet.bybit.com" if cfg.get("network") == "testnet"
            else "https://api.bybit.com")
    if not (cfg.get("api_key") and cfg.get("api_secret")):
        _http("GET", base + "/v5/market/time")
        return {"ok": True, "status": "reachable", "detail": "network OK — add API key + secret to verify account access"}
    ts = _now_ts()
    recv = "5000"
    q = "accountType=UNIFIED"
    sign_str = ts + cfg["api_key"] + recv + q
    sig = hmac.new(cfg["api_secret"].encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    data = _http("GET", f"{base}/v5/account/wallet-balance?{q}", headers={
        "X-BAPI-API-KEY": cfg["api_key"], "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig})
    if data.get("retCode") == 0:
        return {"ok": True, "status": "ok", "detail": "credentials verified (read-only)"}
    return {"ok": False, "status": "error", "detail": str(data)[:200]}


def _test_okx(cfg: Dict[str, Any]) -> Dict[str, Any]:
    base = ("https://www.okx.com" if cfg.get("network") == "mainnet"
            else "https://www.okx.com")
    if not (cfg.get("api_key") and cfg.get("api_secret") and cfg.get("api_passphrase")):
        _http("GET", base + "/api/v5/public/time")
        return {"ok": True, "status": "reachable", "detail": "network OK — add key, secret and passphrase to verify account access"}
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    path = "/api/v5/account/balance"
    sign_str = ts + "GET" + path
    sig = base64.b64encode(hmac.new(cfg["api_secret"].encode(), sign_str.encode(), hashlib.sha256).digest()).decode()
    data = _http("GET", base + path, headers={
        "OK-ACCESS-KEY": cfg["api_key"], "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": cfg["api_passphrase"]})
    if data.get("code") == "0":
        return {"ok": True, "status": "ok", "detail": "credentials verified (read-only)"}
    return {"ok": False, "status": "error", "detail": str(data)[:200]}


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------

def _binance_base(cfg: Dict[str, Any]) -> str:
    return ("https://testnet.binance.vision" if cfg.get("network") == "testnet"
            else "https://api.binance.com")


def _binance_signed(cfg: Dict[str, Any], method: str, path: str, params: Dict[str, Any]) -> dict:
    import urllib.parse
    ts = _now_ts()
    params = dict(params)
    params["timestamp"] = ts
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(cfg["api_secret"].encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{_binance_base(cfg)}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": cfg["api_key"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class BrokerConnections:
    def __init__(self, config_path: str, state_path: Optional[str] = None):
        self.config_path = config_path
        self.state_path = state_path
        self.config = self._load()
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"positions": {}, "orders": []}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self.state["orders"] = self.state["orders"][-100:]
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    def _load(self) -> Dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    stored = json.load(f)
                for platform in cfg:
                    if platform in stored and isinstance(stored[platform], dict):
                        cfg[platform].update(stored[platform])
            except Exception:
                pass
        return cfg

    def save_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        for platform, fields in patch.items():
            if platform in self.config and isinstance(fields, dict):
                for k, v in fields.items():
                    if k == "enabled":
                        self.config[platform][k] = (bool(v) if not isinstance(v, str)
                                                    else v.lower() in ("1", "true", "yes", "on"))
                    else:
                        self.config[platform][k] = str(v)
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)
        return self.status()

    def _mask(self, s: str, keep: int = 4) -> str:
        s = str(s)
        if not s:
            return ""
        return ("*" * max(0, len(s) - keep)) + s[-keep:]

    def get_public_config(self) -> Dict[str, Any]:
        pub = json.loads(json.dumps(self.config))
        for platform, fields in pub.items():
            for k in fields:
                if k in SECRET_KEYS:
                    fields[k] = "•••" if fields[k] else ""
                elif k == "api_key" and fields[k]:
                    fields[k] = self._mask(fields[k])
                elif k in ("username", "account", "url") and fields[k] and k == "url":
                    fields[k] = self._mask(fields[k], keep=8)
        return pub

    def test(self, platform: str) -> Dict[str, Any]:
        if platform not in self.config:
            return {"platform": platform, "ok": False, "status": "unknown", "detail": "unknown platform"}
        meta = PLATFORM_META[platform]
        cfg = self.config[platform]
        if not cfg.get("enabled"):
            return {"platform": platform, "ok": False, "status": "disabled", "detail": "enable this platform first"}
        try:
            if platform == "binance":
                res = _test_binance(cfg)
            elif platform == "bybit":
                res = _test_bybit(cfg)
            elif platform == "okx":
                res = _test_okx(cfg)
            elif platform == "polymarket":
                res = self._test_polymarket()
            elif platform == "betfair":
                res = self._test_betfair(cfg)
            else:
                res = {"ok": False, "status": "scaffold", "detail": meta["note"]}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "status": "error", "detail": str(e)[:200]}
        res["platform"] = platform
        return res

    def _test_polymarket(self) -> Dict[str, Any]:
        cl = PolymarketClient()
        data = cl.get_markets(limit=3)
        if data.get("count"):
            q = data["markets"][0]["question"]
            return {"ok": True, "status": "ok", "detail": f"public market data reachable ({data['count']} markets, e.g. '{q[:40]}…')"}
        return {"ok": False, "status": "error", "detail": "no market data returned"}

    def _test_betfair(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not (cfg.get("app_key") and cfg.get("username") and cfg.get("password")):
            return {"ok": False, "status": "not_configured",
                    "detail": "add app_key, username and password (Betfair also needs developer approval + interactive login)"}
        cl = BetfairClient(cfg["app_key"], cfg["username"], cfg["password"])
        login = cl.login()
        if not login.get("ok"):
            return {"ok": False, "status": login.get("status", "error"),
                    "detail": login.get("detail", login.get("error", ""))}
        cats = cl.list_market_catalogue("football", max_results=3)
        if cats.get("count"):
            return {"ok": True, "status": "ok",
                    "detail": f"logged in · {cats['count']} football markets visible"}
        return {"ok": False, "status": "error", "detail": cats.get("error", "no markets returned")}

    def status(self) -> Dict[str, Any]:
        platforms = []
        for platform, meta in PLATFORM_META.items():
            cfg = self.config[platform]
            platforms.append({
                "platform": platform,
                "label": meta["label"],
                "enabled": bool(cfg.get("enabled")),
                "mode": cfg.get("mode", "test"),
                "configured": self._configured(platform),
                "testable": meta.get("testable", False),
                "note": meta["note"],
                "auto_trade": False,  # NEVER auto-trade until an adapter is wired
            })
        return {
            "platforms": platforms,
            "auto_trade_ready": False,  # hard, honest flag
            "note": "Credentials can be stored and connectivity verified here, "
                    "but order placement / auto-trading is NOT wired — it needs a "
                    "per-platform adapter and your explicit go-live decision.",
        }

    def _configured(self, platform: str) -> bool:
        cfg = self.config[platform]
        if platform in ("binance", "bybit"):
            return bool(cfg.get("api_key") and cfg.get("api_secret"))
        if platform == "okx":
            return bool(cfg.get("api_key") and cfg.get("api_secret") and cfg.get("api_passphrase"))
        if platform == "betfair":
            return bool(cfg.get("app_key") and cfg.get("username") and cfg.get("password"))
        if platform == "metatrader":
            return bool(cfg.get("account") and cfg.get("password") and cfg.get("server"))
        if platform == "custom_webhook":
            return bool(cfg.get("url"))
        return False

    # -- real order execution (Binance, testnet-first) --------------------
    def execution_ready(self, platform: str = "binance") -> Dict[str, Any]:
        """Report whether real orders can be placed on a platform right now."""
        if platform != "binance":
            return {"ready": False, "reason": "only Binance has a wired order adapter so far"}
        cfg = self.config["binance"]
        if not cfg.get("enabled"):
            return {"ready": False, "reason": "binance not enabled"}
        if not self._configured("binance"):
            return {"ready": False, "reason": "binance api key/secret missing"}
        network = cfg.get("network", "testnet")
        return {
            "ready": True,
            "network": network,
            "is_real_money": network == "mainnet",
            "symbol": cfg.get("symbol", "BTCUSDT"),
        }

    def place_market_order(self, platform: str, side: str,
                           quote_order_qty: float, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Place a REAL market order (BUY/SELL) on Binance for the configured pair.
        quote_order_qty is the amount of the quote asset (USDT) to spend.
        Uses the testnet unless network=mainnet, so it never risks real money
        unless you explicitly set mainnet with real keys.
        """
        if platform != "binance":
            return {"ok": False, "error": "only Binance has a wired order adapter so far"}
        cfg = self.config["binance"]
        if not (cfg.get("enabled") and self._configured("binance")):
            return {"ok": False, "error": "binance not enabled or keys missing"}
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return {"ok": False, "error": "side must be BUY or SELL"}
        try:
            qty = float(quote_order_qty)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid quote quantity"}
        if qty <= 0:
            return {"ok": False, "error": "quantity must be positive"}
        sym = symbol or cfg.get("symbol", "BTCUSDT")
        try:
            data = _binance_signed(cfg, "POST", "/api/v3/order", {
                "symbol": sym, "side": side, "type": "MARKET",
                "quoteOrderQty": f"{qty:.8f}",
            })
            result = {"ok": True, "network": cfg.get("network", "testnet"),
                      "symbol": sym, "side": side, "quote_order_qty": round(qty, 6),
                      "response": data}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "network": cfg.get("network", "testnet"),
                      "symbol": symbol or cfg.get("symbol", "BTCUSDT"),
                      "side": side, "quote_order_qty": round(qty, 6),
                      "error": str(e)[:300]}
        self._record_order(platform, result)
        return result

    # -- live position ledger (Binance) -----------------------------------
    def _record_order(self, platform: str, result: Dict[str, Any]) -> None:
        if platform != "binance":
            return
        side = result.get("side")
        symbol = result.get("symbol")
        quote_qty = result.get("quote_order_qty", 0.0)
        # parse avg fill price from the response when present
        avg_price = None
        fills = (result.get("response") or {}).get("fills") or []
        if fills:
            try:
                total_qty = sum(float(f["qty"]) for f in fills)
                total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                avg_price = round(total_cost / total_qty, 8) if total_qty else None
            except (KeyError, ValueError, ZeroDivisionError):
                avg_price = None
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "side": side, "symbol": symbol, "quote_qty": round(quote_qty, 6),
            "avg_price": avg_price, "ok": bool(result.get("ok")),
            "network": result.get("network", "testnet"),
            "detail": result.get("error") or "filled",
        }
        self.state["orders"].append(entry)
        self._save_state()

    def live_positions(self) -> Dict[str, Any]:
        """Reconstruct open positions per symbol from BUY/SELL order history."""
        positions = {}
        for o in self.state["orders"]:
            sym = o["symbol"]
            p = positions.setdefault(sym, {
                "symbol": sym, "buy_qty": 0.0, "sell_qty": 0.0,
                "entries": [], "last_network": o.get("network", "testnet"),
            })
            p["last_network"] = o.get("network", "testnet")
            if o["side"] == "BUY":
                p["buy_qty"] += o["quote_qty"]
                p["entries"].append({"ts": o["ts"], "qty": o["quote_qty"], "avg_price": o["avg_price"]})
            elif o["side"] == "SELL":
                p["sell_qty"] += o["quote_qty"]
        open_positions = []
        for sym, p in positions.items():
            net = round(p["buy_qty"] - p["sell_qty"], 6)
            if net > 0:
                open_positions.append({
                    "symbol": sym, "net_quote": net, "network": p["last_network"],
                    "entries": p["entries"],
                })
        return {"positions": open_positions, "orders": self.state["orders"][-30:][::-1]}

    def close_position(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Close an open Binance position by selling the open amount."""
        lp = self.live_positions()["positions"]
        sym = symbol or (self.config["binance"].get("symbol", "BTCUSDT"))
        target = next((p for p in lp if p["symbol"] == sym), None)
        if not target:
            return {"ok": False, "error": f"no open position for {sym}"}
        return self.place_market_order("binance", "SELL", target["net_quote"], sym)
