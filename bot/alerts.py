"""
Alerting for the Arena Sports Bot.

Channels:
  * log      — appends to a local file (always safe, no credentials needed)
  * email    — SMTP (Gmail/Outlook/etc.); typically needs an app-password
  * telegram — Telegram Bot API (bot token + chat id)
  * webhook  — generic JSON POST to any URL

Events are emitted with a level:
  * info     — position opened / scaled in  -> log only
  * notice   — position closed / settled / equity milestone -> all channels
  * critical — daily halt / errors          -> all channels

All sends happen on a background worker thread, so the trading loop is never
blocked, and any channel failure is caught and logged (never crashes the bot).

Secrets are stored in data/alert_config.json (git-ignored) and masked when the
config is read back over the API.
"""

import json
import os
import queue
import ssl
import threading
import time
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "log": {"enabled": True, "file": "data/alerts.log"},
    "email": {
        "enabled": False,
        "host": "smtp.gmail.com",
        "port": 587,
        "use_ssl": False,
        "starttls": True,
        "username": "",
        "password": "",
        "from": "",
        "to": [],
    },
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "webhook": {"enabled": False, "url": ""},
}

_SECRET_KEYS = {"password", "bot_token"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AlertManager:
    def __init__(self, config_path: Optional[str]):
        self.config_path = config_path
        self.config = self._load()
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._recent: deque = deque(maxlen=200)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # -- config -----------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.config_path and os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    stored = json.load(f)
                for ch in cfg:
                    if ch in stored and isinstance(stored[ch], dict):
                        cfg[ch].update(stored[ch])
            except Exception:
                pass
        return cfg

    def save_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        for ch, fields in patch.items():
            if ch in self.config and isinstance(fields, dict):
                if ch not in self.config:
                    self.config[ch] = {}
                for k, v in fields.items():
                    self.config[ch][k] = v
        if self.config_path:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=2)
        return self.get_public_config()

    def get_public_config(self) -> Dict[str, Any]:
        pub = json.loads(json.dumps(self.config))
        for ch in pub.values():
            if isinstance(ch, dict):
                for k in list(ch.keys()):
                    if k in _SECRET_KEYS:
                        ch[k] = "•••" if ch[k] else ""
        return pub

    def status(self) -> List[Dict[str, Any]]:
        out = []
        for ch, cfg in self.config.items():
            out.append({
                "channel": ch,
                "enabled": bool(cfg.get("enabled")),
                "configured": self._configured(ch),
            })
        return out

    def _configured(self, ch: str) -> bool:
        cfg = self.config.get(ch, {})
        if ch == "log":
            return True
        if ch == "email":
            return bool(cfg.get("username") and cfg.get("password") and cfg.get("to"))
        if ch == "telegram":
            return bool(cfg.get("bot_token") and cfg.get("chat_id"))
        if ch == "webhook":
            return bool(cfg.get("url"))
        return False

    # -- emit -------------------------------------------------------------
    def emit(self, level: str, title: str, message: str, skip=None) -> None:
        self.q.put({"level": level, "title": title, "message": message,
                    "ts": _now(), "skip": set(skip or [])})

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            self._deliver(item)

    def _deliver(self, item: Dict[str, Any]) -> None:
        level = item["level"]
        skip = item.get("skip") or set()
        line = f"[{item['ts']}] {level.upper():8s} {item['title']}: {item['message']}"
        self._recent.append(line)

        results = []
        # log channel gets everything
        if self.config["log"].get("enabled"):
            results.append(("log", self._send_log(line)))
        # others get notice/critical only (avoid spamming on every open/add)
        if level in ("notice", "critical"):
            if self.config["email"].get("enabled") and "email" not in skip:
                results.append(("email", self._send_email(item)))
            if self.config["telegram"].get("enabled") and "telegram" not in skip:
                results.append(("telegram", self._send_telegram(item)))
            if self.config["webhook"].get("enabled") and "webhook" not in skip:
                results.append(("webhook", self._send_webhook(item)))
        for ch, err in results:
            if err:
                self._recent.append(f"[{item['ts']}] ERROR {ch}: {err}")

    # -- channel senders (each returns None on success, str error otherwise)
    def _send_log(self, line: str) -> Optional[str]:
        try:
            path = self.config["log"].get("file") or "data/alerts.log"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a") as f:
                f.write(line + "\n")
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _send_email(self, item: Dict[str, Any]) -> Optional[str]:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        cfg = self.config["email"]
        if not self._configured("email"):
            return "email not fully configured"
        try:
            msg = MIMEText(f"{item['message']}\n\n— Arena Sports Bot", "plain", "utf-8")
            msg["Subject"] = Header(f"[ArenaBot] {item['title']}", "utf-8")
            msg["From"] = cfg["from"]
            msg["To"] = ", ".join(cfg["to"])
            ctx = ssl.create_default_context()
            if cfg.get("use_ssl"):
                server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
                if cfg.get("starttls"):
                    server.starttls(context=ctx)
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from"], cfg["to"], msg.as_string())
            server.quit()
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _send_telegram(self, item: Dict[str, Any]) -> Optional[str]:
        cfg = self.config["telegram"]
        if not self._configured("telegram"):
            return "telegram not fully configured"
        try:
            url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
            text = f"🤖 <b>{item['title']}</b>\n{item['message']}"
            data = json.dumps({
                "chat_id": cfg["chat_id"], "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _send_webhook(self, item: Dict[str, Any]) -> Optional[str]:
        cfg = self.config["webhook"]
        if not self._configured("webhook"):
            return "webhook not fully configured"
        try:
            data = json.dumps(item).encode("utf-8")
            req = urllib.request.Request(cfg["url"], data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)

    # -- test -------------------------------------------------------------
    def test(self) -> List[Dict[str, Any]]:
        """Send a test message to every ENABLED channel and report results."""
        item = {"level": "notice", "title": "Test alert",
                "message": "This is a test alert from your Arena Sports Bot. 🎉", "ts": _now()}
        out = []
        for ch, sender in [
            ("log", lambda: self._send_log(f"[{item['ts']}] TEST     {item['message']}")),
            ("email", lambda: self._send_email(item)),
            ("telegram", lambda: self._send_telegram(item)),
            ("webhook", lambda: self._send_webhook(item)),
        ]:
            cfg = self.config.get(ch, {})
            if not cfg.get("enabled"):
                continue
            err = None
            try:
                err = sender()
            except Exception as e:  # noqa: BLE001
                err = str(e)
            out.append({"channel": ch, "ok": err is None, "error": err})
        return out

    def recent(self) -> List[str]:
        return list(self._recent)

    def stop(self) -> None:
        self._stop.set()
