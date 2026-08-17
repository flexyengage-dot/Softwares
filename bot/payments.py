"""
Payment / funding integration for the Arena Sports Bot.

READ THIS FIRST — honest limitations
------------------------------------
This module is an INTEGRATION SCAFFOLD. It does not move real money on its own,
and it cannot — moving real money requires a LICENSED payment processor with
YOUR OWN account, KYC, and API keys. Until you connect one, everything runs in
TEST/MOCK mode and only touches the simulated wallet.

How real money actually works (Nigeria):
  * Deposits: you initialize a transaction on a licensed processor (Paystack,
    Flutterwave, Monnify, ...). The processor hosts the checkout page; the
    payer pays there; the processor then POSTs a signed webhook to your server,
    and you credit the bot wallet only after verifying that signature.
  * Withdrawals: you call the processor's Transfer API to pay out to a verified
    bank account. It is a recorded, limited, reversible-by-you operation — NOT
    an instant "click and money appears" magic.

What ships here:
  * MockProvider      — default. Simulates the whole flow locally so you can
                        build and test without any credentials. Clearly fake.
  * PaystackProvider  — real Paystack API calls (works with their test or live
                        keys). Inactive until you add your own keys.

Two warnings:
  1. The bot does NOT guarantee profit (it is ~break-even in simulation), so a
     "withdraw profits" button only ever moves money you yourself deposited.
  2. If the plan is to collect deposits from OTHER people and pay them "profits",
     that is a regulated financial activity — and if those payouts are funded by
     new deposits rather than genuine returns, it is a Ponzi scheme. This module
     is written for the OWNER's own deposits and withdrawals only.
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

PAYSTACK_BASE = "https://api.paystack.co"

DEFAULT_CONFIG: Dict[str, Any] = {
    "provider": "mock",           # "mock" | "paystack" | "off"
    "mode": "test",               # "test" | "live"
    "paystack": {
        "secret_key": "",
        "public_key": "",
    },
    "withdrawal": {
        "account_name": "",       # owner's own verified bank account
        "account_number": "",
        "bank_code": "",
        "min_withdrawal": 500.0,
        "daily_withdrawal_limit": 50000.0,
        "auto_withdraw": False,   # never auto-withdraw — manual confirmation only
    },
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ref() -> str:
    return f"ASB-{int(time.time())}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class PaystackProvider:
    """Real Paystack API client (initialize / verify / transfer / webhook)."""

    def __init__(self, secret_key: str, public_key: str, mode: str = "test"):
        self.secret_key = secret_key
        self.public_key = public_key
        self.mode = mode

    def configured(self) -> bool:
        return bool(self.secret_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = PAYSTACK_BASE + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def initialize_deposit(self, amount_ngn: float, email: str, reference: str,
                           callback_url: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "email": email,
            "amount": int(round(amount_ngn * 100)),  # Paystack uses kobo
            "currency": "NGN",
            "reference": reference,
        }
        if callback_url:
            body["callback_url"] = callback_url
        resp = self._request("POST", "/transaction/initialize", body)
        return {"reference": reference, "authorization_url": resp["data"]["authorization_url"]}

    def verify_deposit(self, reference: str) -> Dict[str, Any]:
        resp = self._request("GET", f"/transaction/verify/{reference}")
        d = resp.get("data", {})
        return {
            "reference": reference,
            "status": d.get("status", "failed"),
            "amount_ngn": round(d.get("amount", 0) / 100.0, 2),
        }

    def create_transfer(self, amount_ngn: float, account_name: str,
                        account_number: str, bank_code: str, reference: str) -> Dict[str, Any]:
        rec = self._request("POST", "/transferrecipient", {
            "type": "nuban",
            "name": account_name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN",
        })
        recipient_code = rec["data"]["recipient_code"]
        tr = self._request("POST", "/transfer", {
            "source": "balance",
            "amount": int(round(amount_ngn * 100)),
            "recipient": recipient_code,
            "reference": reference,
            "currency": "NGN",
            "reason": "Arena Sports Bot withdrawal",
        })
        return {"reference": reference, "status": tr["data"].get("status", "pending")}

    def verify_webhook(self, raw_body: bytes, headers: Dict[str, str]) -> bool:
        sig = headers.get("x-paystack-signature", "")
        if not sig or not self.secret_key:
            return False
        digest = hmac.new(self.secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(digest, sig)


class MockProvider:
    """Local simulation of the payment flow — no real money, clearly fake."""

    def __init__(self):
        pass

    def configured(self) -> bool:
        return True

    def initialize_deposit(self, amount_ngn: float, email: str, reference: str,
                           callback_url: str = "") -> Dict[str, Any]:
        return {"reference": reference, "authorization_url": "", "mock": True}

    def verify_deposit(self, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "status": "success", "amount_ngn": 0.0}

    def create_transfer(self, amount_ngn: float, account_name: str,
                        account_number: str, bank_code: str, reference: str) -> Dict[str, Any]:
        return {"reference": reference, "status": "success", "mock": True}

    def verify_webhook(self, raw_body: bytes, headers: Dict[str, str]) -> bool:
        return True


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class PaymentManager:
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
                for k in ("provider", "mode"):
                    if k in stored:
                        cfg[k] = stored[k]
                if "paystack" in stored:
                    cfg["paystack"].update(stored["paystack"])
                if "withdrawal" in stored:
                    cfg["withdrawal"].update(stored["withdrawal"])
            except Exception:
                pass
        return cfg

    def save_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("provider", "mode"):
            if k in patch:
                self.config[k] = patch[k]
        if "paystack" in patch and isinstance(patch["paystack"], dict):
            self.config["paystack"].update(patch["paystack"])
        if "withdrawal" in patch and isinstance(patch["withdrawal"], dict):
            for k, v in patch["withdrawal"].items():
                if k in ("min_withdrawal", "daily_withdrawal_limit"):
                    try:
                        self.config["withdrawal"][k] = max(0.0, float(v))
                    except (TypeError, ValueError):
                        pass
                elif k == "auto_withdraw":
                    self.config["withdrawal"][k] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
                else:
                    self.config["withdrawal"][k] = str(v)
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
        pub["paystack"]["secret_key"] = "•••" + self._mask(pub["paystack"]["secret_key"]) if pub["paystack"]["secret_key"] else ""
        pub["withdrawal"]["account_number"] = self._mask(pub["withdrawal"]["account_number"])
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
        if p == "paystack":
            return PaystackProvider(self.config["paystack"]["secret_key"],
                                    self.config["paystack"]["public_key"],
                                    self.config.get("mode", "test"))
        return MockProvider()

    # -- status -----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.config.get("provider", "mock"),
            "mode": self.config.get("mode", "test"),
            "configured": self.provider.configured(),
            "is_live": (self.config.get("provider") == "paystack"
                        and self.config.get("mode") == "live"
                        and self.provider.configured()),
            "withdrawal": {
                "account_name": self.config["withdrawal"]["account_name"],
                "account_number": self._mask(self.config["withdrawal"]["account_number"]),
                "bank_code": self.config["withdrawal"]["bank_code"],
                "min_withdrawal": self.config["withdrawal"]["min_withdrawal"],
                "daily_withdrawal_limit": self.config["withdrawal"]["daily_withdrawal_limit"],
                "auto_withdraw": self.config["withdrawal"]["auto_withdraw"],
            },
            "withdrawn_total": round(self.state.get("withdrawn_total", 0.0), 2),
            "available_balance": round(self.engine.account.balance, 2),
            "history": self.state["history"][-20:][::-1],
        }

    # -- deposit ----------------------------------------------------------
    def deposit_init(self, amount_ngn: float, email: str) -> Dict[str, Any]:
        amount = round(max(1.0, amount_ngn), 2)
        reference = _ref()
        if self.config.get("provider") == "off":
            return {"error": "payments are disabled"}
        if self.config.get("provider") == "paystack" and not self.provider.configured():
            return {"error": "Paystack secret key not configured — add your own keys first"}
        result = self.provider.initialize_deposit(amount, email, reference)
        if result.get("mock"):
            # mock mode: simulate an immediate successful payment
            self.state["pending"][reference] = {"amount": amount, "email": email, "created": _now()}
            self._save_state()
            conf = self.deposit_confirm(reference)
            return {"mode": "mock", "reference": reference, "credited": conf.get("ok", False)}
        self.state["pending"][reference] = {"amount": amount, "email": email, "created": _now()}
        self._save_state()
        return {"mode": "paystack", "reference": reference,
                "authorization_url": result.get("authorization_url", "")}

    def deposit_confirm(self, reference: str) -> Dict[str, Any]:
        pend = self.state["pending"].pop(reference, None)
        if not pend:
            return {"ok": False, "error": "unknown or already-processed reference"}
        # verify with the provider (mock always succeeds)
        verified = self.provider.verify_deposit(reference)
        if verified.get("status") != "success":
            return {"ok": False, "error": f"payment not successful (status={verified.get('status')})"}
        amount = pend["amount"]
        self.engine.deposit(amount, target="both")
        self._log("deposit", amount, reference, "credited to paper + live wallets")
        return {"ok": True, "amount": amount}

    # -- withdrawal -------------------------------------------------------
    def withdraw(self, amount_ngn: float, account_name: str, account_number: str,
                 bank_code: str) -> Dict[str, Any]:
        w = self.config["withdrawal"]
        amount = round(max(0.0, amount_ngn), 2)
        if self.config.get("provider") == "off":
            return {"error": "payments are disabled"}
        if self.config.get("provider") == "paystack" and not self.provider.configured():
            return {"error": "Paystack secret key not configured — add your own keys first"}
        if amount < w["min_withdrawal"]:
            return {"error": f"amount below minimum withdrawal ({NAIRA}{w['min_withdrawal']:,.0f})"}
        if amount > self.engine.account.balance:
            return {"error": "amount exceeds available balance"}
        # daily limit (sum of withdrawals with a ts starting today)
        today = time.strftime("%Y-%m-%d")
        withdrawn_today = sum(h["amount"] for h in self.state["history"]
                              if h["kind"] == "withdrawal" and h["ts"].startswith(today))
        if withdrawn_today + amount > w["daily_withdrawal_limit"]:
            return {"error": f"daily withdrawal limit reached ({NAIRA}{w['daily_withdrawal_limit']:,.0f})"}
        if w["auto_withdraw"]:
            # never silently auto-pay; require explicit confirmation in the UI
            return {"error": "auto_withdraw is disabled by design — confirm manually"}
        if not account_name or not account_number or not bank_code:
            return {"error": "provide account name, number and bank code"}

        reference = _ref()
        result = self.provider.create_transfer(amount, account_name, account_number, bank_code, reference)
        self.engine.withdraw(amount, target="both")
        self.state["withdrawn_total"] = round(self.state.get("withdrawn_total", 0.0) + amount, 2)
        status = result.get("status", "pending")
        self._log("withdrawal", amount, reference, f"status={status}")
        return {"ok": True, "amount": amount, "reference": reference, "status": status,
                "note": "simulated payout" if result.get("mock") else "transfer initiated via Paystack"}

    # -- webhook (Paystack live) -----------------------------------------
    def webhook(self, raw_body: bytes, headers: Dict[str, str]) -> bool:
        if not self.provider.verify_webhook(raw_body, headers):
            return False
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return False
        if event.get("event") == "charge.success":
            data = event.get("data", {})
            ref = data.get("reference", "")
            if ref in self.state["pending"]:
                self.deposit_confirm(ref)
        return True
