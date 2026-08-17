"""
Sports / prediction-market adapters (Polymarket + Betfair).

These are the only two "sports" venues with a genuine programmatic API. This
module implements their real HTTP shapes so the bot can:

  * Polymarket — read live markets, order books, and prices (public, no key).
                 Placing an order needs a private-key-signed order (py-clob-client)
                 + an API key — scaffolded honestly.
  * Betfair    — interactive login, list market catalogue, read order books, and
                 place orders via the real API-NG endpoints.

Honest limitations:
  * Betfair's login is now INTERACTIVE (browser-based) for retail keys, and order
    placement needs Betfair developer approval + an app key. The API shapes below
    are the real ones (cert-login + API-NG), but you cannot go live without a
    Betfair account that has API access enabled.
  * Neither is a Nigerian bookmaker — Bet9ja/SportyBet/Betway have no API at all.
  * Order placement on both requires secrets I cannot (and will not) fabricate.
    Treat the read-only market data as live; treat order placement as a scaffold
    to finish with your own keys.

No keys = no real money risk. Everything here is read-only unless you add keys.
"""

import json
import time
import urllib.request
from typing import Any, Dict, List, Optional

POLYMARKET_CLOB = "https://clob.polymarket.com"
BETFAIR_SSO = "https://identitysso-cert.betfair.com/api"
BETFAIR_API = "https://api.betfair.com/exchange/betting/rest/v1.0"


def _get_json(url: str, headers: Optional[dict] = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout: int = 15) -> Any:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Polymarket (prediction market — the closest thing to "sports trading" with a
# public API)
# ---------------------------------------------------------------------------

class PolymarketClient:
    def __init__(self, base: str = POLYMARKET_CLOB):
        self.base = base

    def get_markets(self, limit: int = 20, active: bool = True, closed: bool = False) -> Dict[str, Any]:
        """List markets (public). Returns a lightweight, bot-friendly shape."""
        url = f"{self.base}/markets?limit={limit}&active={'true' if active else 'false'}&closed={'false' if closed else 'true'}"
        data = _get_json(url)
        markets = []
        for m in data.get("data", [])[:limit]:
            tokens = m.get("tokens") or []
            markets.append({
                "condition_id": m.get("conditionId"),
                "question": m.get("question"),
                "outcomes": m.get("outcomes", ""),
                "token_ids": tokens,
                "volume": m.get("volumeNum"),
                "active": m.get("active"),
            })
        return {"count": len(markets), "markets": markets}

    def get_book(self, token_id: str) -> Dict[str, Any]:
        """Order book for one outcome token (public)."""
        return _get_json(f"{self.base}/book?token_id={token_id}")

    def get_price(self, token_id: str, side: str = "buy") -> Dict[str, Any]:
        """Best available price (public)."""
        return _get_json(f"{self.base}/price?token_id={token_id}&side={side}")

    def place_order(self, token_id: str, price: float, size: float, side: str) -> Dict[str, Any]:
        """
        Place a REAL order. Requires an API key (POLY_API_KEY) and a private-key
        EIP-712 signature via py-clob-client. This is deliberately not implemented
        with fake keys — it returns the exact requirements instead.
        """
        return {
            "ok": False,
            "status": "not_configured",
            "error": "Polymarket orders require py-clob-client EIP-712 signing with your "
                     "own private key + API credentials. Read-only market data works today.",
            "required": ["clob api key", "private key (mnemonic)", "token_id", "price", "size", "side"],
        }


# ---------------------------------------------------------------------------
# Betfair Exchange (real sports exchange with API-NG)
# ---------------------------------------------------------------------------

class BetfairClient:
    def __init__(self, app_key: str = "", username: str = "", password: str = ""):
        self.app_key = app_key
        self.username = username
        self.password = password
        self.session_token: Optional[str] = None

    def login(self) -> Dict[str, Any]:
        """Interactive/cert login. Betfair now requires browser-based interactive
        login for most retail accounts; this implements the classic cert-login
        shape and reports clearly when it can't proceed."""
        if not (self.username and self.password):
            return {"ok": False, "status": "not_configured",
                    "error": "Betfair username + password required (and an app key)."}
        try:
            resp = _post_json(
                f"{BETFAIR_SSO}/login",
                {"username": self.username, "password": self.password},
                headers={"X-Application": self.app_key, "Content-Type": "application/x-www-form-urlencoded"},
            )
            token = resp.get("token") or resp.get("sessionToken")
            if token:
                self.session_token = token
                return {"ok": True, "status": "ok", "detail": "logged in"}
            status = resp.get("status")
            return {"ok": False, "status": "error",
                    "detail": f"login failed: {status} — Betfair now requires interactive (browser) login for most accounts"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "error", "detail": str(e)[:200]}

    def _headers(self) -> Dict[str, str]:
        return {"X-Application": self.app_key, "X-Authentication": self.session_token or "",
                "Content-Type": "application/json", "Accept": "application/json"}

    def list_market_catalogue(self, query: str = "football", max_results: int = 10) -> Dict[str, Any]:
        """List events/markets matching a query (e.g. a football match)."""
        try:
            resp = _post_json(
                f"{BETFAIR_API}/listMarketCatalogue/",
                {"filter": {"textQuery": query, "marketTypeCodes": ["MATCH_ODDS"]},
                 "maxResults": max_results},
                headers=self._headers(),
            )
            markets = []
            for m in resp:
                markets.append({
                    "market_id": m.get("marketId"),
                    "name": m.get("marketName"),
                    "event": (m.get("event") or {}).get("name"),
                    "runners": [(r.get("runnerName"), r.get("selectionId"))
                                for r in m.get("runners", [])],
                })
            return {"count": len(markets), "markets": markets}
        except Exception as e:  # noqa: BLE001
            return {"count": 0, "markets": [], "error": str(e)[:200]}

    def list_market_book(self, market_id: str) -> Dict[str, Any]:
        """Live prices (back/lay) for a market."""
        try:
            resp = _post_json(
                f"{BETFAIR_API}/listMarketBook/",
                {"marketIds": [market_id], "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}},
                headers=self._headers(),
            )
            runners = []
            for r in resp[0].get("runners", []):
                runners.append({
                    "selectionId": r.get("selectionId"),
                    "status": r.get("status"),
                    "best_back": (r.get("ex", {}) or {}).get("availableToBack", [{}])[0].get("price") if (r.get("ex") or {}).get("availableToBack") else None,
                    "best_lay": (r.get("ex", {}) or {}).get("availableToLay", [{}])[0].get("price") if (r.get("ex") or {}).get("availableToLay") else None,
                })
            return {"market_id": market_id, "runners": runners}
        except Exception as e:  # noqa: BLE001
            return {"market_id": market_id, "runners": [], "error": str(e)[:200]}

    def place_order(self, market_id: str, selection_id: int, side: str, size: float, price: float) -> Dict[str, Any]:
        """Place a back/lay order on Betfair. Requires login + developer approval."""
        if not self.session_token:
            return {"ok": False, "status": "not_logged_in", "error": "log in first"}
        try:
            resp = _post_json(
                f"{BETFAIR_API}/placeOrders/",
                {"marketId": market_id,
                 "instructions": [{"selectionId": selection_id, "side": side.upper(),
                                   "orderType": "LIMIT", "limitOrder": {"size": size, "price": price, "persistenceType": "LAPSE"}}]},
                headers=self._headers(),
            )
            status = resp.get("status")
            return {"ok": status == "SUCCESS", "status": status, "detail": str(resp)[:200]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "error", "detail": str(e)[:200]}
