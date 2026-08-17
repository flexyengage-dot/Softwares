"""
Crypto wallet integration for the Arena Sports Bot (Trust Wallet / Telegram Wallet).

READ THIS FIRST — what is and isn't possible
--------------------------------------------
1. NO AUTO-PULL. A server cannot silently "pick funds from" your Trust Wallet or
   Telegram Wallet. Wallets require YOU to authorize every outgoing transfer
   (that's their security model). So deposits are always: the bot issues a
   payment request, YOU approve/send, the bot credits you after confirmation.

2. This module is an INTEGRATION SCAFFOLD. By default it runs in MOCK mode and
   touches only the simulated wallet. Real crypto requires YOUR OWN keys:
     * Telegram Wallet Pay — register as a merchant at wallet.tg/pay to get a
       store API key. NOTE: Wallet Pay is a merchant payments product; check its
       Terms of Service before using it to fund a trading service.
     * TON direct deposits / withdrawals — you provide a deposit address and a
       HOT WALLET (custodial keys on the server). Hot wallets carry real risk:
       whoever holds the keys holds the funds. Never commit a mnemonic.

3. There is STILL no guaranteed profit (the bot is ~break-even in simulation),
   so "withdraw profits" only ever moves money you yourself deposited.

Providers shipped:
  * MockProvider                — full flow, no keys, clearly fake (default).
  * TelegramWalletPayProvider   — real deposit via Wallet Pay orders + webhook.
                                  (Wallet Pay does NOT do payouts — withdrawals
                                   go via a TON hot wallet, see TonProvider.)
  * TonProvider                 — direct TON/USDT address deposits (works with
                                  Trust Wallet) + a withdrawal path that must be
                                  signed with a TON library + your mnemonic.

Wallet Pay API (documented):  POST https://pay.wallet.tg/wpay/store-api/v1/order
  header  Wpay-Store-Api-Key: <key>
  body    {amount:{currencyCode, amount}, externalId, timeoutSeconds,
           description, returnUrl, failReturnUrl, customData,
           customerTelegramUserId, autoConversionCurrency}
  → returns payLink / directPayLink; webhook events ORDER_PAID / ORDER_FAILED.

TON chain API (toncenter):  https://toncenter.com/api/v2/getAddressBalance etc.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
import urllib.request
from typing import Any, Dict, Optional

NAIRA = "\u20a6"  # ₦

WALLETPAY_BASE = "https://pay.wallet.tg"
TONCENTER_BASE = "https://toncenter.com/api/v2"

DEFAULT_CONFIG: Dict[str, Any] = {
    "provider": "mock",          # "mock" | "walletpay" | "ton" | "off"
    "mode": "test",              # "test" | "live"
    "asset": "USDT",             # "USDT" (on TON) | "TON"
    "usd_ngn_rate": 1500.0,      # conversion for display/order creation
    "walletpay": {
        "store_api_key": "",
        "customer_telegram_user_id": "",   # optional: restrict payer
    },
    "ton": {
        "api_key": "",           # optional toncenter key (higher rate limit)
        "deposit_address": "",   # where you send crypto IN (works with Trust Wallet)
        "hot_wallet_address": "",
        "hot_wallet_mnemonic": "",  # NEVER commit — local signing only
        "network": "mainnet",    # "mainnet" | "testnet"
    },
    "withdrawal": {
        "wallet_address": "",    # your payout address (e.g. your Trust Wallet)
        "min_withdrawal": 500.0,
        "daily_withdrawal_limit": 50000.0,
        "auto_withdraw": False,
    },
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ref() -> str:
    return f"CR-{int(time.time())}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class MockProvider:
    def configured(self) -> bool:
        return True

    def create_deposit(self, amount_ngn: float, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "pay_link": "", "mock": True}

    def check_deposit(self, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "status": "paid", "amount_ngn": 0.0}

    def send_withdrawal(self, amount_ngn: float, address: str, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "status": "success", "mock": True}

    def verify_webhook(self, raw: bytes, headers: Dict[str, str]) -> bool:
        return True


class TelegramWalletPayProvider:
    """Real Wallet Pay deposits. Payouts are NOT supported by Wallet Pay."""

    def __init__(self, store_api_key: str, customer_id: str = "", usd_ngn_rate: float = 1500.0):
        self.key = store_api_key
        self.customer_id = customer_id
        self.usd_ngn_rate = usd_ngn_rate

    def configured(self) -> bool:
        return bool(self.key)

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = WALLETPAY_BASE + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Wpay-Store-Api-Key": self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def create_deposit(self, amount_ngn: float, reference: str) -> Dict[str, Any]:
        usd = round(amount_ngn / max(1.0, self.usd_ngn_rate), 2)
        body: Dict[str, Any] = {
            "amount": {"currencyCode": "USD", "amount": f"{usd:.2f}"},
            "externalId": reference,
            "timeoutSeconds": 3600,
            "description": "Arena Sports Bot top-up",
            "customData": f"ref={reference}",
            "autoConversionCurrency": "USDT",
        }
        if self.customer_id:
            try:
                body["customerTelegramUserId"] = int(self.customer_id)
            except (TypeError, ValueError):
                pass
        resp = self._request("POST", "/wpay/store-api/v1/order", body)
        data = resp.get("data", {})
        return {"reference": reference, "pay_link": data.get("directPayLink") or data.get("payLink", "")}

    def check_deposit(self, reference: str) -> Dict[str, Any]:
        # Wallet Pay notifies via webhook; polling is not the intended flow.
        return {"reference": reference, "status": "unknown"}

    def send_withdrawal(self, amount_ngn: float, address: str, reference: str) -> Dict[str, Any]:
        # Wallet Pay has no payout API — use a TON hot wallet (TonProvider).
        return {"reference": reference, "status": "unsupported",
                "error": "Wallet Pay does not do payouts; use the TON provider for withdrawals"}

    def verify_webhook(self, raw: bytes, headers: Dict[str, str]) -> bool:
        # Wallet Pay signs webhooks; confirm the exact canonical string + header
        # name against docs.wallet.tg/pay before going live.
        sig = headers.get("x-wallet-pay-signature") or headers.get("x-webhook-signature", "")
        if not sig or not self.key:
            return False
        digest = hmac.new(self.key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, sig)


class TonProvider:
    """Direct TON/USDT address deposits + (scaffolded) hot-wallet withdrawals."""

    def __init__(self, api_key: str, deposit_address: str, hot_wallet_address: str,
                 mnemonic: str, network: str = "mainnet"):
        self.api_key = api_key
        self.deposit_address = deposit_address
        self.hot_wallet_address = hot_wallet_address
        self.mnemonic = mnemonic
        self.network = network
        self._last_balance = None

    def configured(self) -> bool:
        return bool(self.deposit_address)

    def _get_balance(self) -> Optional[float]:
        base = "https://testnet.toncenter.com/api/v2" if self.network == "testnet" else TONCENTER_BASE
        url = f"{base}/getAddressBalance?address={self.deposit_address}"
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return int(data["result"]) / 1e9  # nanoton -> TON
        return None

    def create_deposit(self, amount_ngn: float, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "address": self.deposit_address,
                "note": "Send the amount to this address; the bot credits on confirmation."}

    def check_deposit(self, reference: str) -> Dict[str, Any]:
        bal = self._get_balance()
        if bal is None:
            return {"reference": reference, "status": "unknown"}
        if self._last_balance is None:
            self._last_balance = bal
            return {"reference": reference, "status": "pending"}
        delta = bal - self._last_balance
        if delta > 1e-6:
            self._last_balance = bal
            return {"reference": reference, "status": "paid", "amount_ton": round(delta, 6)}
        return {"reference": reference, "status": "pending"}

    def send_withdrawal(self, amount_ngn: float, address: str, reference: str) -> Dict[str, Any]:
        # Real TON transfers require offline-signing a message with a TON library
        # (pytoniq / tonsdk) using the hot-wallet mnemonic, then broadcasting the
        # BOC via toncenter /sendBoc. This is intentionally NOT auto-implemented —
        # signing must happen with keys you control. Here we surface exactly what
        # is needed rather than pretend.
        if not self.mnemonic:
            return {"reference": reference, "status": "not_configured",
                    "error": "hot_wallet_mnemonic required to sign withdrawals"}
        return {"reference": reference, "status": "manual",
                "error": "TON signing not wired — integrate pytoniq/tonsdk + broadcast via toncenter /sendBoc"}

    def verify_webhook(self, raw: bytes, headers: Dict[str, str]) -> bool:
        return False  # direct-address deposits are detected by polling, not webhook


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CryptoWalletManager:
    def __init__(self, engine, config_path: str, state_path: str):
        self.engine = engine
        self.config_path = config_path
        self.state_path = state_path
        self.config = self._load_config()
        self.state = self._load_state()
        self.provider = self._build_provider()

    # -- config -----------------------------------------------------------
    def _load_config(self) -> Dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    stored = json.load(f)
                for k in ("provider", "mode", "asset", "usd_ngn_rate"):
                    if k in stored:
                        cfg[k] = stored[k]
                for section in ("walletpay", "ton", "withdrawal"):
                    if section in stored and isinstance(stored[section], dict):
                        cfg[section].update(stored[section])
            except Exception:
                pass
        return cfg

    def save_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("provider", "mode", "asset"):
            if k in patch:
                self.config[k] = str(patch[k])
        if "usd_ngn_rate" in patch:
            try:
                self.config["usd_ngn_rate"] = max(1.0, float(patch["usd_ngn_rate"]))
            except (TypeError, ValueError):
                pass
        for section in ("walletpay", "ton", "withdrawal"):
            if section in patch and isinstance(patch[section], dict):
                for k, v in patch[section].items():
                    if k in ("min_withdrawal", "daily_withdrawal_limit"):
                        try:
                            self.config[section][k] = max(0.0, float(v))
                        except (TypeError, ValueError):
                            pass
                    elif k == "auto_withdraw":
                        self.config[section][k] = (bool(v) if not isinstance(v, str)
                                                   else v.lower() in ("1", "true", "yes", "on"))
                    else:
                        self.config[section][k] = str(v)
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)
        self.provider = self._build_provider()
        return self.status()

    def _mask(self, s: str, keep: int = 4) -> str:
        s = str(s)
        if not s:
            return ""
        return ("*" * max(0, len(s) - keep)) + s[-keep:]

    def get_public_config(self) -> Dict[str, Any]:
        pub = json.loads(json.dumps(self.config))
        pub["walletpay"]["store_api_key"] = ("•••" + self._mask(pub["walletpay"]["store_api_key"])
                                             if pub["walletpay"]["store_api_key"] else "")
        pub["ton"]["api_key"] = ("•••" + self._mask(pub["ton"]["api_key"]) if pub["ton"]["api_key"] else "")
        pub["ton"]["hot_wallet_mnemonic"] = ("•••" if pub["ton"]["hot_wallet_mnemonic"] else "")
        pub["withdrawal"]["wallet_address"] = self._mask(pub["withdrawal"]["wallet_address"], keep=6)
        return pub

    # -- state ------------------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"pending": {}, "history": [], "withdrawn_total": 0.0}

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self.state["history"] = self.state["history"][-100:]
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    def _log(self, kind: str, amount: float, ref: str, extra: str = "") -> None:
        self.state["history"].append({
            "ts": _now(), "kind": kind, "amount": round(amount, 2), "ref": ref, "extra": extra,
        })
        self._save_state()

    # -- provider ---------------------------------------------------------
    def _build_provider(self):
        p = self.config.get("provider", "mock")
        if p == "walletpay":
            return TelegramWalletPayProvider(
                self.config["walletpay"]["store_api_key"],
                self.config["walletpay"]["customer_telegram_user_id"],
                self.config.get("usd_ngn_rate", 1500.0))
        if p == "ton":
            t = self.config["ton"]
            return TonProvider(t["api_key"], t["deposit_address"], t["hot_wallet_address"],
                               t["hot_wallet_mnemonic"], t.get("network", "mainnet"))
        return MockProvider()

    # -- status -----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.config.get("provider", "mock"),
            "mode": self.config.get("mode", "test"),
            "asset": self.config.get("asset", "USDT"),
            "configured": self.provider.configured(),
            "is_live": (self.config.get("provider") in ("walletpay", "ton")
                        and self.config.get("mode") == "live"
                        and self.provider.configured()),
            "usd_ngn_rate": self.config.get("usd_ngn_rate", 1500.0),
            "withdrawal": {
                "wallet_address": self._mask(self.config["withdrawal"]["wallet_address"], keep=6),
                "min_withdrawal": self.config["withdrawal"]["min_withdrawal"],
                "daily_withdrawal_limit": self.config["withdrawal"]["daily_withdrawal_limit"],
                "auto_withdraw": self.config["withdrawal"]["auto_withdraw"],
            },
            "withdrawn_total": round(self.state.get("withdrawn_total", 0.0), 2),
            "available_balance": round(self.engine.account.balance, 2),
            "history": self.state["history"][-20:][::-1],
        }

    # -- deposit ----------------------------------------------------------
    def deposit_init(self, amount_ngn: float) -> Dict[str, Any]:
        amount = round(max(1.0, amount_ngn), 2)
        reference = _ref()
        if self.config.get("provider") == "off":
            return {"error": "crypto payments are disabled"}
        if self.config.get("provider") != "mock" and not self.provider.configured():
            return {"error": "provider not configured — add your own keys/addresses first"}
        result = self.provider.create_deposit(amount, reference)
        self.state["pending"][reference] = {"amount": amount, "created": _now()}
        self._save_state()
        if result.get("mock"):
            self.deposit_confirm(reference)
            return {"mode": "mock", "reference": reference, "credited": True}
        return {"mode": self.config["provider"], "reference": reference,
                "pay_link": result.get("pay_link", ""),
                "address": result.get("address", ""),
                "note": result.get("note", "")}

    def deposit_confirm(self, reference: str) -> Dict[str, Any]:
        pend = self.state["pending"].pop(reference, None)
        if not pend:
            return {"ok": False, "error": "unknown or already-processed reference"}
        verified = self.provider.check_deposit(reference)
        if verified.get("status") not in ("paid", "success"):
            self.state["pending"][reference] = pend
            self._save_state()
            return {"ok": False, "error": f"payment not confirmed (status={verified.get('status')})"}
        amount = pend["amount"]
        self.engine.deposit(amount, target="both")
        self._log("deposit", amount, reference, "credited to paper + live wallets")
        return {"ok": True, "amount": amount}

    # -- withdrawal -------------------------------------------------------
    def withdraw(self, amount_ngn: float, address: str) -> Dict[str, Any]:
        w = self.config["withdrawal"]
        amount = round(max(0.0, amount_ngn), 2)
        if self.config.get("provider") == "off":
            return {"error": "crypto payments are disabled"}
        if amount < w["min_withdrawal"]:
            return {"error": f"amount below minimum withdrawal ({NAIRA}{w['min_withdrawal']:,.0f})"}
        if amount > self.engine.account.balance:
            return {"error": "amount exceeds available balance"}
        today = time.strftime("%Y-%m-%d")
        withdrawn_today = sum(h["amount"] for h in self.state["history"]
                              if h["kind"] == "withdrawal" and h["ts"].startswith(today))
        if withdrawn_today + amount > w["daily_withdrawal_limit"]:
            return {"error": f"daily withdrawal limit reached ({NAIRA}{w['daily_withdrawal_limit']:,.0f})"}
        if w["auto_withdraw"]:
            return {"error": "auto_withdraw is disabled by design — confirm manually"}
        if not address:
            address = w["wallet_address"]
        if not address:
            return {"error": "provide your wallet address"}

        reference = _ref()
        result = self.provider.send_withdrawal(amount, address, reference)
        status = result.get("status", "pending")
        if status in ("unsupported", "not_configured", "manual"):
            return {"error": result.get("error", "withdrawal unavailable with this provider")}

        self.engine.withdraw(amount, target="both")
        self.state["withdrawn_total"] = round(self.state.get("withdrawn_total", 0.0) + amount, 2)
        self._log("withdrawal", amount, reference, f"status={status} -> {self._mask(address, 6)}")
        return {"ok": True, "amount": amount, "reference": reference, "status": status,
                "note": "simulated payout" if result.get("mock") else "transfer sent from hot wallet"}

    # -- webhook (Wallet Pay) --------------------------------------------
    def webhook(self, raw: bytes, headers: Dict[str, str]) -> bool:
        if not self.provider.verify_webhook(raw, headers):
            return False
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            return False
        # Wallet Pay sends events; ORDER_PAID carries externalId = our reference
        etype = event.get("type") or event.get("event")
        payload = event.get("payload", event)
        ref = payload.get("externalId") or payload.get("customData", "")
        if "ref=" in str(ref):
            ref = str(ref).split("ref=")[-1].split("&")[0]
        if etype in ("ORDER_PAID", "charge.success") and ref in self.state["pending"]:
            self.deposit_confirm(ref)
        return True
