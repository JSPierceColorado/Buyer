# V2 Buyer — single-writer S&P100 low-water ladder + noon Denver scheduler

The buyer is a **perpetual Railway worker**. It schedules one run each Monday–Friday at **12:00 p.m. America/Denver**, following Mountain daylight saving time without Railway Cron. It does **not** place orders on startup or retries. The code remains separately reviewable at `JSPierceColorado/Buyer`.

## Strategy

Source: [original V2 Screener](https://docs.google.com/spreadsheets/d/14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ/edit), tab **Screener**, ticker column **A**, score column **G**.

- **Score must be >=95**; candidates are ranked by descending score then ticker.
- **Not already held long:** buy **2% of current Alpaca margin buying power** in that symbol, via a market DAY **dollar-notional** order.
- **S&P100 held positions:** the persistent low-water ledger samples current broker price **once at noon each NY trading day**. First observation seeds the reference, with no purchase. Future steps require a strictly new observed low at least **1% below the previous filled step price** and price below average entry. All held S&P100 stocks are eligible, even when their Screener score is below 95.
- **Six steps:** 5%, 10%, 20%, 40%, 80%, then 100% of initial recorded position cost. A $100 initial position receives $5, $10, $20, $40, $80, then $100: $255 added, $355 total before gains/losses. Every step is at least the $1 Alpaca notional minimum, but no step may exceed the original position amount. For a $5 S&P100 holding the rungs become **$1, $1, $1, $2, $4, $5**, so the total would be $19 instead of $17.75. Six fills maximum, sufficient buying power required, no seventh purchase.
- **Review flag:** after six confirmed fills set persistent `CAP_REACHED_REVIEW_REQUIRED` and log `LOWWATER_REVIEW_REQUIRED`. Unfilled, uncertain, or externally increased positions also pause for review. A step only advances after Alpaca confirms a filled order.
- **Non-S&P100 held positions:** no top-ups. Their existing conditional -5% stop remains unchanged. New holdings can still be purchased when they qualify.
- **One owner:** V2-Buyer alone writes the low-water ledger and adds to held stocks. AI Recommendations Buyer may open new positions, but skips all held stocks even when high-confidence.
- **Persistence:** Railway volume named `v2-lowwater` mounted at `/data` stores active position episodes, observed daily lows, fill steps, order IDs and flags. Initial basis for an existing holding is its aggregate cost at first observation; no inaccurate historical order reconstruction. An external held-quantity increase triggers review.
- **Empty-portfolio reliability:** if Alpaca returns zero positions, the buyer immediately fetches the entire portfolio a second time. Even if both responses are empty, existing ladder records remain intact until a second distinct trading-day observation also confirms no holdings. A nonempty response resets the empty streak. Log `LOWWATER_EMPTY_PORTFOLIO_RETAIN_HISTORY` or `LOWWATER_TRANSIENT_EMPTY_PORTFOLIO` when these protections activate.
- **Daily health:** `LOWWATER_DAILY_AUDIT` reports tracked positions, counts at steps 0–6, unresolved pending orders and persistent review flags; the external oversight task should alert only on meaningful anomalies.
- **Risk:** the ladder can reach 3.55x initial position basis; this is a per-position ladder cap, not a guaranteed account-wide leverage limit.
- **Re-entry cooldown after a confirmed stop-loss only:** a positive filled quantity on a `v2sl-YYYYMMDD-TICKER` sell order this trading week blocks ALL new/additional buys of that symbol through Sunday; it is eligible again the next Monday (New York trading calendar). A Monday stop blocks through the weekend; a Friday stop expires the coming Monday. Profitable `v2sg-` exits never impose a cooldown. All order history must reconcile, otherwise that buying cycle fails closed. Legacy `v2s-` IDs cannot be classified by exit reason and are not retroactively blocked.
- Every subsequent purchase uses the lesser of remaining locally reserved buying power and fresh broker-reported buying power. This is **not** 2% of cash or account equity. Margin buying power may reflect leveraged purchasing capacity and can create margin debt/interest.
- The source must contain at least 95 validated stock rows, valid original score headers, and a **same-day 9 a.m. V2 refresh timestamp plus recent completed-session date** in `Screener!I1:J2`. A missing or stale marker causes **zero buys**. The Screener publishes that marker atomically with its A:F update; G formulas remain intact.
- Only fractionable, tradable US equities; market must be open. A short position or a pending order blocks buying that symbol. No crypto, options, short selling, selling, or stop-loss orders.
- A deterministic `v2b-YYYYMMDD-SYMBOL` broker client ID prevents a restart from submitting a second same-day order for a symbol. On ambiguous order submission errors the run **aborts without retry**.
- The model does not impose an arbitrary maximum number of qualifying tickers; the cumulative amount depends on the number of signals and available margin buying power. This strategy can allocate significant cash across multiple symbols in one session. S&P100 holdings may receive ladder purchases after qualifying daily lows; non-S&P100 holdings receive no additions. None of these scores guarantee gains.

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
| `HELD_ADDITION_USD` | Legacy only: ignored; held additions use the persistent six-step S&P100 low-water ladder. |
| `LOG_LEVEL` | `INFO` |

To verify credentials, run `python main.py --check-now`; this forces **dry run**, even if the deployment's scheduled mode is live. It still enforces a valid same-day Screener marker and never submits orders. The first live order is only eligible on the following qualified scheduled noon, when market is open and the morning scan succeeded.

### Operational warning

This is a live-capable automated BUYER. Market orders do not guarantee an execution price and can immediately lose value. It never sells, and no assumption is made that a separate seller bot is deployed. Use dedicated credentials and restrict Railway secrets to the necessary service. Confirm source-sheet quality, broker buying power and margin risk, order journal and execution logs before relying on automated trading.
