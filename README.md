# Arena Sports Bot

An automated **sports-trading engine + web console**. It runs a risk-managed
strategy against a live *simulated* market feed, with a wallet you can top up
(starting balance **₦3,000**, editable anytime).

---

## ⚠️ Read this first — honest limitations

1. **This is a paper-trading / simulation tool.** It does **not** connect to any
   bookmaker or betting exchange, and it does **not** move real money. "Deposit"
   only records a top-up in a simulated wallet so you can rehearse a plan.

2. **No software can guarantee profit.** Sports outcomes are unpredictable, and
   every trade pays a spread. The objective of this bot is **risk-controlled
   execution** — small positions, hard stops, a daily loss circuit-breaker — not
   a guaranteed win. Any system that promises "absolute profits" is lying to you.

3. The strategy's profitability in the simulator depends on the assumed market
   model (`market_momentum`). Real markets may not behave the same way. The
   default parameters are a *starting point to test and tune*, not a proven edge.

4. **Going live for real** requires a platform that exposes a trading API and
   your own account/credentials. Most Nigerian bookmakers (Bet9ja, SportyBet,
   Betway, etc.) do **not** offer public trading APIs. Treat that as a separate,
   manual integration step — see [Going live](#going-live).

---

## What the bot does (maps to your request)

| Your requirement | How it is implemented |
| --- | --- |
| "Pull out when it calculates the outcome to fail" | `EXIT` when the momentum signal reverses below `exit_signal`, **or** a stop-loss / take-profit level is hit |
| "Add when it will succeed" | `ADD` — scale into a winning position when the signal strengthens above `add_signal` |
| "Little wins matter, not very big" | Small stake per lot (default 2% of bankroll) + a modest take-profit (`take_profit_pct = 25%` of stake) |
| "Start with ₦3,000, editable" | `starting_balance` (default 3000) + a deposit/top-up form |
| "Accessible any time" | A web dashboard served on port 8000; state persists to `data/state.json` |

### Two wallets: Paper vs Live (run side by side)

The engine keeps **two ledgers** so you can rehearse safely before ever risking
real money — exactly the "paper mode alongside the real deal" setup:

| | Paper wallet | Live (real) wallet |
| --- | --- | --- |
| Execution | Ideal (zero slippage, zero fees) | **Realistic** — slippage + commission |
| Money | Simulated | Simulated shadow — **broker NOT connected** |
| Purpose | Strategy / tuning | Preview of what real results would look like |

The live ledger mirrors every paper signal but applies `live_slippage` (worse
fill odds) and `live_commission_pct` (exchange-style fee on winnings), so its
results are realistically **slightly worse** than paper. It stays clearly marked
"shadow — broker NOT connected" until you plug in a real platform API (see
[Going live](#going-live)). Both are visible on the dashboard.

The strategy (see `bot/engine.py`) is a **momentum + scale-in** approach on
implied-probability momentum, with three hard risk controls in `RiskManager`:

- **Position sizing** — each lot is `max_stake_pct` of the bankroll (never the whole pot).
- **Stop-loss / take-profit** — every position is exited at a bounded loss or a small target win.
- **Daily loss circuit-breaker** — the bot halts for the day after losing `max_daily_loss_pct` of the bankroll.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Web dashboard (static/)                   │
│  equity curve · positions · market feed · history · settings │
└──────────────────────────────┬──────────────────────────────┘
                               │  GET/POST JSON  (poll every 1s)
┌──────────────────────────────▼──────────────────────────────┐
│                    bot/server.py  (HTTP API)                 │
│ /api/state · /api/control · /api/deposit · /api/settings     │
│ /api/backtest · /api/alerts[/config|/test]                   │
│ /api/payments[/config|/deposit|/withdraw|/webhook]           │
│ /api/crypto[/config|/deposit|/withdraw|/webhook]             │
│ /api/brokers[/config|/test|/live|/close]                      │
│ /api/sports/markets                                          │
│ /api/decisions/approve|reject                                │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                     bot/engine.py  (core)                    │
│  ┌────────────┐  ┌─────────────┐  ┌──────────────────────┐  │
│  │ MarketSim  │→ │  Strategy   │→ │  RiskManager         │  │
│  │ odds feed  │  │ momentum    │  │ sizing · stop · halt │  │
│  └────────────┘  │ enter/add/  │  └──────────────────────┘  │
│                  │ exit signals│                            │
│                  └─────────────┘                            │
│  Account (balance · deposits · realized P&L · trades)        │
└──────────────────────────────┬──────────────────────────────┘
                               │
                     data/state.json  (persistence)
```

The `MarketSim` is swappable: in production you replace it with a real
bookmaker/exchange adapter that implements the same interface
(`back_odds`, `lay_odds`, price updates).

---

## Run it

```bash
cd Softwares
python3 run.py          # serves on http://0.0.0.0:8000
```

No dependencies — Python 3.9+ standard library only.

Open the dashboard and you'll see the bot trading the simulated feed in real
time. Use **Start / Stop / Reset**, top up the wallet, and edit strategy/risk
settings live.

### Backtesting (many runs)

A backtest runs the strategy across many independent simulated histories and
aggregates the results into a report (JSON + HTML with a P&L histogram).

**From the dashboard:** open the **Backtest** panel, choose runs/ticks, click
*Run backtest*, then *Open full report*.

**From the CLI:**

```bash
python3 -m bot.backtest --runs 100 --ticks 500 --out data/backtest
```

Per-run metrics include realized P&L, return %, win rate, max drawdown, and
profit factor. The aggregate shows mean/median/best/worst P&L, % profitable runs,
and worst drawdown — so you can see the *distribution* of outcomes, not just one
lucky (or unlucky) path.

> ⚠️ Backtest results reflect the **simulated market model**, not real markets.
> Treat them as a sanity check of the strategy's mechanics and risk profile,
> never as a profit guarantee.

**An honest read of the default backtest:** with the default settings, the
momentum + scale-in strategy **loses a little on average** — typically only a
minority of runs end in profit, and the worst drawdown can exceed 25% of the
bankroll. The reason is real: sports prices mean-revert, and every entry/exit
pays a spread, so the transaction cost often eats the edge. This is *normal*
for naive scalping strategies, and it is precisely why the backtest exists —
use it to tune `entry_signal` / `add_signal` / `exit_signal` / `take_profit_pct`
and to see the full distribution of outcomes **before** you trust it with money.
There is no parameter combination that guarantees profit; treat anything that
looks too good as a sign you are over-fitting the simulator.

### Alerts (email · Telegram · webhook · log)

The bot can notify you when it opens/closes positions, settles bets, hits equity
milestones, or halts on the daily loss limit. Channels:

- **log** — appends to `data/alerts.log` (on by default, no setup).
- **email** — SMTP (Gmail/Outlook). Gmail requires an **App Password**
  (Google Account → Security → 2-Step Verification → App passwords).
- **telegram** — create a bot with [@BotFather](https://t.me/BotFather) to get a
  `bot_token`, then message the bot and read your `chat_id`
  (e.g. via [@userinfobot](https://t.me/userinfobot)).
- **webhook** — POST JSON to any URL (Slack, Discord, Zapier, your own server…).

Configure them from the dashboard **Alerts** panel, or edit
`data/alert_config.json` directly (see `alert_config.example.json` for the
format). Secrets are masked in the UI. Click **Send test alert** to verify each
channel before relying on it.

### Telegram commands (control the bot from your phone)

Once Telegram is configured (bot token + chat id), you can message your bot to
inspect it — no need to open the dashboard:

| Command | What it does |
| --- | --- |
| `/help` or `/start` | list commands |
| `/status` | wallet balance, equity, running/halted state, open positions, and **trade capital vs reserved (untouched)** |
| `/summary` | latest backtest summary (daily scheduled, else on-demand) |
| `/positions` | open positions and their unrealized P&L |
| `/run` | run a backtest now and return the summary |

It uses Telegram's `getUpdates` long-polling (no public webhook required), so it
works from anywhere the bot can reach api.telegram.org. **Security:** only the
configured chat id can command the bot; strangers are ignored. If no chat id is
set yet, the **first person to message the bot becomes the owner** (handy for
setup). The Alerts panel shows the command-bot status.

A quick headless single-run check:

```bash
python3 -c "
from bot.engine import Engine
import tempfile, os
p = os.path.join(tempfile.gettempdir(), 'bt.json')
import os as o; o.path.exists(p) and o.remove(p)
e = Engine(p)
for _ in range(500): e.tick()
print('realized P&L:', round(e.account.realized_pnl, 2))
print('win rate:', round(e.stats()['win_rate'], 3))
"
```

---

## Settings (`bot/engine.py` → `DEFAULT_SETTINGS`, editable in the UI)

| Key | Default | Meaning |
| --- | --- | --- |
| `starting_balance` | 3000 | Wallet start (₦), editable |
| `trade_capital` | 0 | ₦ the bot may trade (0 = full balance). You allocate this manually; the rest of your balance is **reserved** and never touched by the strategy |
| `max_stake_pct` | 0.005 | % of bankroll per entry lot (tiny = "little wins") |
| `max_open_positions` | 1 | one position at a time |
| `max_adds` | 0 | scale-in lots per position (off by default — see tuning note) |
| `stop_loss_pct` | 0.30 | pull out at −30% of a stake |
| `take_profit_pct` | 0.25 | small win target, +25% of stake |
| `max_daily_loss_pct` | 0.10 | halt the bot at −10% of bankroll/day |
| `entry_signal` / `add_signal` / `exit_signal` | 0.10 / 0.14 / 0.02 | momentum thresholds |
| `ema_slow` | 15 | trend window (ticks) |
| `tick_seconds` | 2 | engine speed |
| `market_momentum` | 0.55 | simulated market trending (see honesty note) |
| `live_mode` | shadow | `shadow` = mirror live ledger w/ fees · `off` = disable |
| `live_slippage` | 0.01 | odds degradation on real fills (1%) |
| `live_commission_pct` | 0.05 | exchange-style fee on net winnings (5%) |

### How the defaults were chosen (drawdown-first tuning)

`bot/tune.py` grid-searches the parameter space across many seeds, ranked by
drawdown. The search found that the biggest drawdown levers are:

1. **Position sizing** — tiny lots (`max_stake_pct = 0.005`, ~₦20) cut drawdown ~8×.
2. **One position at a time** (`max_open_positions = 1`) — no simultaneous losing trades.
3. **No scaling-in** (`max_adds = 0`) — the backtests consistently show that adding
   to a position *increases* drawdown without adding edge. The feature is still
   implemented and available; it is just off by default.
4. **Selective entries** (`entry_signal = 0.10`) and **prompt exits**
   (`exit_signal = 0.02`) — trade less often and get out when momentum stalls.

Result (50 runs × 600 ticks, default settings):

| Metric | Before (naive defaults) | After (tuned) |
| --- | --- | --- |
| Mean P&L | ~−136 | ~−4 (≈ break-even) |
| Median P&L | ~−312 | ~+2 (positive) |
| Profitable runs | ~22% | ~54% |
| Worst drawdown | ~27% | **~3.5%** |

So the tuned bot is roughly break-even in the simulator with a ~8× smaller
worst drawdown — exactly the "modest profit, low drawdown" goal. **This is not a
profit guarantee** (see the honest-limitations section): the remaining edge is
still negative on average, and real markets will differ. Re-run the search any
time:

```bash
python3 -m bot.tune --seeds 20 --ticks 600
```

### Daily scheduled backtest

The bot can **re-check the strategy once a day** and alert you with the result.
From the dashboard **Daily scheduled backtest** panel (or the
`daily_backtest_*` settings): enable it, pick a time, and it will run
`daily_backtest_runs` runs × `daily_backtest_ticks` ticks at that time daily,
save a report (`data/daily_backtest_report.html`), and send an alert with the
summary (mean P&L, % profitable, worst drawdown). You can also click **Run now**.

```bash
# schedule config lives in settings (persisted); example:
curl -X POST http://localhost:8000/api/settings -H 'Content-Type: application/json' \
  -d '{"daily_backtest_enabled": true, "daily_backtest_hour": 9, "daily_backtest_minute": 0}'
```

The schedule uses **server-local time** — make sure the server's clock/zone
matches where you are (Africa/Lagos).

When the daily backtest runs **on schedule**, the bot automatically posts a
**morning report** to your Telegram chat (no need to ask) — the backtest summary
plus your current wallet equity/P&L and open-position count. It only needs the
Telegram bot token + chat id to be configured (the same credentials used for
commands and alerts); it works even if the Telegram *alert* toggle is off.
Email/webhook/log still receive the shorter notification separately.

---

### Trade capital — you decide how much is at risk

Because **you** fund the wallet manually (funding is never automatic), you can
also decide how much of your balance the bot is allowed to trade. In the
**Wallet → Trade capital** control, set an amount (e.g. ₦2,000 of your ₦5,000):

- **Stake sizing** is based on the allocated capital, not the full balance.
- **The daily loss limit** is a fraction of the allocated capital — so the bot
  halts after losing e.g. 10% of ₦2,000 (₦200), leaving the other ₦3,000 **reserved
  and untouched**.
- `trade_capital = 0` means "use the full balance" (the default).

The Paper-wallet card shows **Trade capital**, **Reserved (untouched)**, in-play
and available amounts live.

---

## Payments — deposits & withdrawals

The **Payments** panel handles funding the bot and paying out to a bank account.
It is an **integration scaffold**, and by default it runs in **TEST / mock mode**
— it only touches the simulated wallet, and no real money moves.

> ⚠️ **This is the most important section on money.** Real deposits/withdrawals
> require a **licensed payment processor** (Paystack, Flutterwave, Monnify, …)
> with *your own* verified account, KYC, and API keys. You cannot "seamlessly"
> move real money any other way — anyone claiming to is the start of a scam.

How it works once connected to Paystack (the default provider):

- **Deposit** — you initialize a transaction; the payer completes it on
  Paystack's secure hosted page; Paystack POSTs a **signature-verified webhook**
  to your server; only then is the wallet credited.
- **Withdraw** — you call Paystack's Transfer API to pay out to a verified bank
  account. It is **manual, recorded, limited** (minimum amount + daily cap), and
  there is deliberately **no instant auto-payout**.

Provider config (`data/payment_config.json`, see `payment_config.example.json`):

```json
{
  "provider": "paystack",
  "mode": "live",
  "paystack": { "secret_key": "sk_live_…", "public_key": "pk_live_…" },
  "withdrawal": {
    "account_name": "YOUR NAME",
    "account_number": "0123456789",
    "bank_code": "058",
    "min_withdrawal": 500,
    "daily_withdrawal_limit": 50000,
    "auto_withdraw": false
  }
}
```

To go live: (1) create a Paystack account and complete KYC, (2) verify your bank
account, (3) set the keys above with `mode: live`, (4) expose the webhook URL
`/api/payments/webhook` publicly and register it in your Paystack dashboard.
Secrets are masked in the UI and stored in the git-ignored `data/` directory.

**Two warnings that cannot be overstated:**

1. The bot is ~break-even in simulation, so a "withdraw profits" button only ever
   moves **money you yourself deposited**. There is no guaranteed profit to cash out.
2. If the plan is to collect deposits from **other people** and pay them "profits,"
   that is a regulated financial activity — and if those payouts are funded by new
   deposits rather than genuine returns, it is a **Ponzi scheme**. This project is
   built for the owner's own funds only, and I will not build third-party
   deposit-taking, referral, or payout-to-strangers features.

---

## Crypto wallet (Trust Wallet / Telegram Wallet)

The **Crypto wallet** panel adds deposit/withdraw via crypto instead of bank.
It runs in **TEST / mock mode** by default (no real crypto moves), and ships
three providers:

| Provider | Deposit | Withdraw | Notes |
| --- | --- | --- | --- |
| `mock` (default) | instant, simulated | instant, simulated | for building/testing, clearly fake |
| `walletpay` | real — you pay inside Telegram via Wallet Pay | **not supported** | Wallet Pay is a merchant *payments* product; it has no payout API |
| `ton` | real — send USDT/TON to a deposit address (works with Trust Wallet) | scaffolded (needs your hot-wallet mnemonic + a TON signing library) | direct on-chain |

> ⚠️ **Critical honesty points:**
>
> 1. **There is no "auto-pull".** A server cannot silently take funds from your
>    Trust Wallet or Telegram Wallet — wallets require **you** to authorize every
>    outgoing transfer. Deposits are always: bot issues a request → you approve →
>    bot credits on confirmation. That is a security property, not a missing feature.
> 2. **Withdrawals require a hot wallet** (custodial keys on the server), which is
>    a real security responsibility: whoever holds the keys holds the funds.
>    Never commit a mnemonic; the signing step is deliberately left for you to wire
>    with a TON library (pytoniq/tonsdk) + your own keys.
> 3. **Telegram Wallet Pay is a merchant product** — check its Terms of Service
>    before using it to fund a trading service.
> 4. Still **no guaranteed profit** (the bot is ~break-even in simulation), so a
>    "withdraw profits" action only ever moves money you yourself deposited.

Config (`data/crypto_config.json`, see `crypto_config.example.json`):

```json
{
  "provider": "walletpay",
  "mode": "live",
  "asset": "USDT",
  "usd_ngn_rate": 1500,
  "walletpay": { "store_api_key": "…" },
  "ton": { "deposit_address": "…", "hot_wallet_address": "…" },
  "withdrawal": { "wallet_address": "EQ…your wallet…", "min_withdrawal": 500, "daily_withdrawal_limit": 50000 }
}
```

Endpoints: `GET /api/crypto`, `POST /api/crypto/config|deposit|withdraw|webhook`.
Secrets are masked in the UI and stored in the git-ignored `data/` directory.

---

## Trading platforms & broker connections

The **Trading platforms** panel stores your broker/exchange credentials (masked,
persisted to the git-ignored `data/broker_config.json`) and can **verify the
connection read-only**. It does **not** place orders.

| Platform | Linkable? | What's supported |
| --- | --- | --- |
| Binance / Bybit / OKX | ✅ yes | Store keys, verify the account, **and (Binance) place real orders** — testnet by default |
| Polymarket | ✅ yes | Live market data is public; orders need EIP-712 signing + API key |
| Betfair Exchange | ⚠️ partial | Real API-NG implemented (login, catalogue, book, place order) — needs app key + approval |
| MetaTrader 4/5 (forex) | ⚠️ partial | No REST API — needs a bridge (EA / copy-trade server) — scaffold only |
| Bet9ja, SportyBet, Betway, 1xBet NG, … | ❌ no | **No public trading API.** Nothing to link; no bot can auto-trade on them |
| Custom webhook | ✅ yes | POST signals to your own bridge |

**Sports markets** (see `bot/sports.py`): the bot can read live Polymarket markets
(public) and Betfair markets (with login), via the **Sports markets** panel — the
two real "sports trading" venues. Order placement on both requires your own keys
and is scaffolded honestly, never fabricated.

**Live broker positions** (Binance testnet): when you approve a positive-EV
proposal with Binance wired, the bot places a real BUY order (testnet), tracks it
as an open position, and lets you **Close (SELL)** it — the full
approve → order → exit loop with fake money.

> ⚠️ **The most important honest point.** You asked for the bot to "trade for you
> because you don't know how to trade." Connecting an account is only step one.
> Actually **trading on your behalf** requires (1) a per-platform adapter that
> translates the strategy's signals into real orders, and (2) your go-live
> decision. Neither is done here — and that's deliberate. The strategy is
> ~break-even in simulation, so handing real money to an untested adapter is how
> people lose everything. Treat this panel as "connect + verify", and keep
> auto-trading off until a specific platform is actually working.

## Decision desk — you decide every trade

The bot can **calculate** each opportunity and ask you to approve it, instead of
auto-trading. In the **Decision desk** panel:

1. When a market's momentum crosses the entry threshold, the bot creates a
   **proposal** showing the market, side, odds, implied probability, confidence,
   and — the honest part — the **expected value** (`prob × odds − 1`, in ₦ and %).
2. You click **Approve** or **Reject**. **Nothing is placed until you approve.**
3. On approval, the bot only places a real order when (a) the expectation is
   **positive** and (b) a broker with a wired order adapter is connected. Right
   now that adapter is **Binance only** (testnet by default — never real money
   unless you set it to live with your own keys).

> ⚠️ **The hard truth this surfaces:** with the bookmaker margin baked into the
> odds, the calculated expected value is usually **negative** (~−3%) — the spread
> eats the edge, exactly as the backtests showed. So the bot will honestly tell
> you "this trade has negative expectation" and refuse to auto-place it. That is
> the bot "calculating the outcome" correctly: most of the time the right answer
> is *don't trade*. No one can predict the actual result of a sporting event.

There is no genuinely wireable **sports** platform with a simple API (Nigerian
bookmakers have none; Betfair needs OAuth + approval). The real-order adapter
that ships here is **Binance spot** (`place_market_order`, HMAC-signed) — crypto,
not sports — so treat it as the first working example of "connect → verify →
approve → execute", to be extended to the platform you actually want.

To go live with a real platform you must connect a platform that provides a
trading API (e.g. a crypto exchange, or Betfair for sports) and build the
adapter. That is **not** included here and requires your own account, API keys,
and compliance with the platform's terms. The steps are:

1. Choose a platform with a documented, public trading API.
2. Implement its feed + order endpoints behind the same interface used by
   `MarketSim` (see `bot/engine.py`).
3. Keep the risk controls (`max_stake_pct`, stop-loss, daily halt) **on**.
4. Start with the smallest possible real stake until you trust the integration.

If you are unsure, **do not connect real money** — keep using paper mode.

---

*Nothing in this project is financial advice. Betting involves risk of loss;
only risk money you can afford to lose.*
