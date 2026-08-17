"""Zero-dependency HTTP server + JSON API for the Arena Sports Bot.

Serves the dashboard (../static) and exposes:
  GET  /api/state      -> full engine snapshot
  POST /api/control    -> {"action": "start"|"stop"|"reset"|"tick"}
  POST /api/deposit    -> {"amount": 5000}  (simulated wallet top-up)
  POST /api/settings   -> partial settings update

Runs on 0.0.0.0 so it is reachable from the live-preview proxy.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bot.engine import Engine
from bot.scheduler import DailyBacktestScheduler
from bot.telegram_bot import TelegramCommander
from bot.payments import PaymentManager
from bot.crypto_wallet import CryptoWalletManager
from bot.brokers import BrokerConnections

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "..", "static")
DATA_DIR = os.path.join(HERE, "..", "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
BT_JSON = os.path.join(DATA_DIR, "backtest_report.json")
BT_HTML = os.path.join(DATA_DIR, "backtest_report.html")
DAILY_HTML = os.path.join(DATA_DIR, "daily_backtest_report.html")
PAYMENT_CONFIG = os.path.join(DATA_DIR, "payment_config.json")
PAYMENT_STATE = os.path.join(DATA_DIR, "payment_state.json")
CRYPTO_CONFIG = os.path.join(DATA_DIR, "crypto_config.json")
CRYPTO_STATE = os.path.join(DATA_DIR, "crypto_state.json")
BROKER_CONFIG = os.path.join(DATA_DIR, "broker_config.json")

# backtest job state (shared by the background worker)
_BT = {"running": False, "progress": 0, "total": 0, "done": False, "error": None, "summary": None}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def make_app(engine: Engine):
    def _run_backtest(num_runs, ticks):
        import bot.backtest as bt
        try:
            _BT["running"] = True
            _BT["done"] = False
            _BT["error"] = None
            _BT["progress"] = 0
            _BT["total"] = num_runs
            _BT["summary"] = None

            def on_progress(done, total):
                _BT["progress"] = done
                _BT["total"] = total

            report = bt.run_report(num_runs, ticks,
                                   json_path=BT_JSON, html_path=BT_HTML,
                                   on_progress=on_progress)
            agg = report["aggregate"]
            _BT["summary"] = {
                "num_runs": agg["num_runs"],
                "ticks": agg["ticks"],
                "mean_pnl": agg["pnl"]["mean"],
                "median_pnl": agg["pnl"]["median"],
                "min_pnl": agg["pnl"]["min"],
                "max_pnl": agg["pnl"]["max"],
                "profitable_pct": agg["profitable_pct"],
                "mean_return_pct": agg["mean_return_pct"],
                "worst_drawdown": agg["worst_drawdown"],
                "report_url": "/backtest-report",
            }
            _BT["done"] = True
        except Exception as e:  # noqa: BLE001
            _BT["error"] = str(e)
            _BT["done"] = True
        finally:
            _BT["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default logging
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                return {}

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json(200, engine.snapshot())
                return
            if path == "/api/backtest":
                self._json(200, _BT)
                return
            if path == "/backtest-report":
                if os.path.isfile(BT_HTML):
                    with open(BT_HTML) as f:
                        self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, b"No report yet - run a backtest first.", "text/plain")
                return
            if path == "/api/alerts":
                al = engine.alerts
                self._json(200, {
                    "config": al.get_public_config() if al else {},
                    "status": al.status() if al else [],
                    "recent": al.recent() if al else [],
                    "telegram_commands": TELEGRAM.status() if TELEGRAM else None,
                })
                return
            if path == "/api/schedule":
                self._json(200, SCHEDULER.status() if SCHEDULER else {"error": "scheduler unavailable"})
                return
            if path == "/api/payments":
                self._json(200, PAYMENTS.status() if PAYMENTS else {"error": "payments unavailable"})
                return
            if path == "/api/crypto":
                self._json(200, CRYPTO.status() if CRYPTO else {"error": "crypto wallet unavailable"})
                return
            if path == "/api/brokers":
                self._json(200, BROKERS.status() if BROKERS else {"error": "brokers unavailable"})
                return
            if path == "/api/brokers/live":
                self._json(200, BROKERS.live_positions() if BROKERS else {"error": "brokers unavailable"})
                return
            if path == "/daily-backtest-report":
                if os.path.isfile(DAILY_HTML):
                    with open(DAILY_HTML) as f:
                        self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, b"No daily backtest yet.", "text/plain")
                return
            self._serve_static(path)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/control":
                data = self._body()
                action = data.get("action")
                if action == "start":
                    engine.start()
                elif action == "stop":
                    engine.stop()
                elif action == "reset":
                    engine.reset()
                    engine.start()
                elif action == "tick":
                    engine.tick()
                    engine.save()
                self._json(200, engine.snapshot())
                return
            if path == "/api/deposit":
                data = self._body()
                amount = float(data.get("amount", 0) or 0)
                target = data.get("target", "paper")
                self._json(200, engine.deposit(amount, target))
                return
            if path == "/api/settings":
                data = self._body()
                self._json(200, engine.update_settings(data))
                return
            if path == "/api/backtest":
                data = self._body()
                if _BT["running"]:
                    self._json(409, {"error": "backtest already running"})
                    return
                num_runs = max(1, min(200, int(data.get("num_runs", 50) or 50)))
                ticks = max(50, min(5000, int(data.get("ticks", 500) or 500)))
                threading.Thread(target=_run_backtest, args=(num_runs, ticks), daemon=True).start()
                self._json(202, {"started": True, "num_runs": num_runs, "ticks": ticks})
                return
            if path == "/api/alerts/config":
                data = self._body()
                if engine.alerts:
                    pub = engine.alerts.save_config(data)
                    self._json(200, {"config": pub, "status": engine.alerts.status()})
                else:
                    self._json(500, {"error": "alerts disabled"})
                return
            if path == "/api/alerts/test":
                if engine.alerts:
                    self._json(200, {"results": engine.alerts.test()})
                else:
                    self._json(500, {"error": "alerts disabled"})
                return
            if path == "/api/schedule/run":
                if SCHEDULER:
                    self._json(200, SCHEDULER.run_now())
                else:
                    self._json(500, {"error": "scheduler unavailable"})
                return
            if path == "/api/payments/config":
                if PAYMENTS:
                    self._json(200, PAYMENTS.save_config(self._body()))
                else:
                    self._json(500, {"error": "payments unavailable"})
                return
            if path == "/api/payments/deposit":
                data = self._body()
                if PAYMENTS:
                    self._json(200, PAYMENTS.deposit_init(float(data.get("amount", 0) or 0),
                                                          data.get("email", "")))
                else:
                    self._json(500, {"error": "payments unavailable"})
                return
            if path == "/api/payments/withdraw":
                data = self._body()
                if PAYMENTS:
                    self._json(200, PAYMENTS.withdraw(
                        float(data.get("amount", 0) or 0),
                        data.get("account_name", ""),
                        data.get("account_number", ""),
                        data.get("bank_code", "")))
                else:
                    self._json(500, {"error": "payments unavailable"})
                return
            if path == "/api/payments/webhook":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                ok = PAYMENTS.webhook(raw, dict(self.headers)) if PAYMENTS else False
                self._json(200, {"ok": ok})
                return
            if path == "/api/crypto/config":
                if CRYPTO:
                    self._json(200, CRYPTO.save_config(self._body()))
                else:
                    self._json(500, {"error": "crypto wallet unavailable"})
                return
            if path == "/api/crypto/deposit":
                data = self._body()
                if CRYPTO:
                    self._json(200, CRYPTO.deposit_init(float(data.get("amount", 0) or 0)))
                else:
                    self._json(500, {"error": "crypto wallet unavailable"})
                return
            if path == "/api/crypto/withdraw":
                data = self._body()
                if CRYPTO:
                    self._json(200, CRYPTO.withdraw(float(data.get("amount", 0) or 0),
                                                    data.get("address", "")))
                else:
                    self._json(500, {"error": "crypto wallet unavailable"})
                return
            if path == "/api/crypto/webhook":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                ok = CRYPTO.webhook(raw, dict(self.headers)) if CRYPTO else False
                self._json(200, {"ok": ok})
                return
            if path == "/api/brokers/config":
                if BROKERS:
                    self._json(200, BROKERS.save_config(self._body()))
                else:
                    self._json(500, {"error": "brokers unavailable"})
                return
            if path == "/api/brokers/test":
                data = self._body()
                if BROKERS:
                    self._json(200, BROKERS.test(data.get("platform", "")))
                else:
                    self._json(500, {"error": "brokers unavailable"})
                return
            if path == "/api/brokers/live":
                if BROKERS:
                    self._json(200, BROKERS.live_positions())
                else:
                    self._json(500, {"error": "brokers unavailable"})
                return
            if path == "/api/brokers/close":
                data = self._body()
                if BROKERS:
                    self._json(200, BROKERS.close_position(data.get("symbol", "")))
                else:
                    self._json(500, {"error": "brokers unavailable"})
                return
            if path == "/api/sports/markets":
                data = self._body()
                platform = data.get("platform", "polymarket")
                try:
                    if platform == "polymarket":
                        from bot.sports import PolymarketClient
                        self._json(200, PolymarketClient().get_markets(limit=int(data.get("limit", 10) or 10)))
                    elif platform == "betfair":
                        if BROKERS:
                            cfg = BROKERS.config.get("betfair", {})
                            from bot.sports import BetfairClient
                            cl = BetfairClient(cfg.get("app_key", ""), cfg.get("username", ""), cfg.get("password", ""))
                            self._json(200, cl.list_market_catalogue(data.get("query", "football"), int(data.get("max", 10) or 10)))
                        else:
                            self._json(500, {"error": "brokers unavailable"})
                    else:
                        self._json(400, {"error": "unknown platform"})
                except Exception as e:  # noqa: BLE001
                    self._json(200, {"error": f"unable to reach {platform}: {str(e)[:160]}"})
                return
            if path == "/api/decisions/approve":
                data = self._body()
                pid = int(data.get("id", 0) or 0)
                p = engine.approve_proposal(pid)
                if p is None:
                    self._json(404, {"error": "proposal not found or already decided"})
                    return
                # Execute only on explicit approval AND positive expectation AND a
                # wired broker. Negative-EV proposals are never auto-placed.
                if p["ev"] > 0 and BROKERS:
                    ready = BROKERS.execution_ready("binance")
                    if ready.get("ready"):
                        side = "BUY"  # momentum-up signal maps to BUY in crypto terms
                        qty = p["stake"] / float(BROKERS.config["binance"].get("usd_ngn_rate", 1500.0))
                        result = BROKERS.place_market_order("binance", side, qty)
                        engine.mark_executed(pid, result)
                    else:
                        engine.mark_executed(pid, {"ok": False,
                                                   "error": "no broker wired: " + ready.get("reason", "unknown")})
                else:
                    note = ("negative expectation — not placed" if p["ev"] <= 0
                            else "no broker connected")
                    engine.mark_executed(pid, {"ok": False, "error": note})
                self._json(200, engine.snapshot())
                return
            if path == "/api/decisions/reject":
                data = self._body()
                pid = int(data.get("id", 0) or 0)
                p = engine.reject_proposal(pid)
                if p is None:
                    self._json(404, {"error": "proposal not found or already decided"})
                    return
                self._json(200, engine.snapshot())
                return
            self._json(404, {"error": "not found"})

        def _serve_static(self, path):
            if path == "/":
                path = "/index.html"
            # prevent path traversal
            rel = path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.normpath(STATIC_DIR)):
                self._send(403, b"Forbidden", "text/plain")
                return
            if not os.path.isfile(full):
                self._send(404, b"Not found", "text/plain")
                return
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as f:
                self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    return Handler


SCHEDULER = None
TELEGRAM = None
PAYMENTS = None
CRYPTO = None
BROKERS = None


def run(host="0.0.0.0", port=8000):
    global SCHEDULER, TELEGRAM, PAYMENTS, CRYPTO, BROKERS
    os.makedirs(DATA_DIR, exist_ok=True)
    engine = Engine(STATE_PATH)
    engine.start()
    SCHEDULER = DailyBacktestScheduler(engine, DATA_DIR)
    SCHEDULER.start()
    TELEGRAM = TelegramCommander(engine, SCHEDULER, engine.alerts, DATA_DIR)
    TELEGRAM.start()
    SCHEDULER.telegram = TELEGRAM
    PAYMENTS = PaymentManager(engine, PAYMENT_CONFIG, PAYMENT_STATE)
    CRYPTO = CryptoWalletManager(engine, CRYPTO_CONFIG, CRYPTO_STATE)
    BROKERS = BrokerConnections(BROKER_CONFIG, os.path.join(DATA_DIR, "broker_state.json"))
    server = ThreadingHTTPServer((host, port), make_app(engine))
    print(f"Arena Sports Bot serving on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        TELEGRAM.stop()
        SCHEDULER.stop()
        engine.stop()
        engine.save()
        server.server_close()


if __name__ == "__main__":
    run()
