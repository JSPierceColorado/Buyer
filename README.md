# V2 Buyer — cash-only noon Denver scheduler

The buyer is a **perpetual Railway worker**. It schedules one run each Monday–Friday at **12:00 p.m. America/Denver**, following Mountain daylight saving time without Railway Cron. It does **not** place orders on startup or retries. The code remains separately reviewable at `JSPierceColorado/Buyer`.

## Strategy

Source: [original V2 Screener](https://docs.google.com/spreadsheets/d/14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ/edit), tab **Screener**, ticker column **A**, score column **G**.

- **Score must be >=95**; candidates are ranked by descending score then ticker.
- **Not already held long:** buy **2% of currently available actual cash** in that symbol, via a market DAY **dollar-notional** order.
- **Already held long:** instead buy **exactly $5** additional notional, once per symbol/day.
- Every subsequent new purchase uses the remaining usable cash after locally reserving all prior submissions. It is NOT 2% of equity or margin-inflated buying power.
- The source must contain at least 95 validated stock rows, valid original score headers, and a **same-day 9 a.m. V2 refresh timestamp plus recent completed-session date** in `Screener!I1:J2`. A missing or stale marker causes **zero buys**. The Screener publishes that marker atomically with its A:F update; G formulas remain intact.
- Only fractionable, tradable US equities; market must be open. A short position or a pending order blocks buying that symbol. No margin purchases, crypto, options, short selling, selling, or stop-loss orders.
- A deterministic `v2b-YYYYMMDD-SYMBOL` broker client ID prevents a restart from submitting a second same-day order for a symbol. On ambiguous order submission errors the run **aborts without retry**.
- The model does not impose an arbitrary maximum number of qualifying tickers; the cumulative amount depends on the number of signals and cash. This strategy can allocate significant cash across multiple symbols in one session. Holdings may receive new $5 purchases on subsequent qualifying days. None of these scores guarantee gains.

## Environment

Railway `Investing` → service `V2-Buyer`. Connect source `JSPierceColorado/Buyer` branch `main`, one replica, **no Railway cron**. Start `python -u main.py`, restart policy `ALWAYS`.

| Variable | Meaning |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Reference your Railway credential-holder service's JSON |
| `GOOGLE_SPREADSHEET_ID` | `14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ` |
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Alpaca paper or live account pair (never both) |
| `ALPACA_PAPER` | `true` for paper or `false` for live |
| `DRY_RUN` | `true` previews, `false` permits broker submissions |
| `LIVE_TRADING_ACK` | Required exact `I_ACCEPT_REAL_MONEY_BUYS` for live + DRY_RUN=false |
| `MINIMUM_SCORE` | `95` |
| `BUY_FRACTION` | `0.02` |
| `HELD_ADDITION_USD` | `5.00` |
| `LOG_LEVEL` | `INFO` |

To verify credentials, run `python main.py --check-now`; this forces **dry run**, even if the deployment's scheduled mode is live. It still enforces a valid same-day Screener marker and never submits orders. The first live order is only eligible on the following qualified scheduled noon, when market is open and the morning scan succeeded.

### Operational warning

This is a live-capable automated BUYER. Market orders do not guarantee an execution price and can immediately lose value. It never sells, and no assumption is made that a separate seller bot is deployed. Use dedicated credentials and restrict Railway secrets to the necessary service. Confirm source-sheet quality, broker cash, order journal and execution logs before relying on automated trading.
