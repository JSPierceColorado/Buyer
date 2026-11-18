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


## Deposit-funded Core Compounder (VT / SGOV)

The V2-Buyer Railway worker also owns a **separate persistent deposit watcher** running every 10 minutes during New York market hours (weekday 09:05–15:55 Eastern; actual buy orders only when the Alpaca market clock is open). The same account credentials and existing /data Railway persistent volume are reused; there is **no additional buyer service or second brokerage account**.

**Source of funds:** read Alpaca Trading API \`CSD\` (positive cash-deposit activity) records with strict pagination, schema and stable-ID validation. The very first successful watcher run establishes a historical deposit baseline **without trading**. Deposits already in the account are not retroactively reallocated. Subsequent newly observed deposit IDs are persisted exactly once.

**Allocation:** for each new credited cash deposit, reserve **25% of the original deposit for VT** and **25% for SGOV**, rounded down to cents. The other 50% remains outside the Core Compounder for the existing automated strategy. For a $1,000 deposit, this creates a $250 VT order and a $250 SGOV order, subject to available unborrowed cash. Each order is submitted no more than once per durable deterministic client order ID (\`v2core-...\`).

**No-margin constraint:** require account \`cash\`, \`non_marginable_buying_power\`, and \`buying_power\` each to cover the order, accounting conservatively for pending outbound transfers. The buyer waits if another broker order is open, the market is closed, asset fractional eligibility cannot be verified, or a previous core order is unresolved. It never substitutes margin buying power for cash; no API transfer or borrowing action exists in this module. This is a **cash-check safeguard in a shared margin account**, not a legally segregated account: other bots may trade concurrently, margin financing and broker liquidation can still affect aggregate collateral.

**Idempotency:** SQLite \`/data/v2-core.sqlite3\` holds account identity, original historical-deposit boundary, new deposit IDs, each ETF entitlement, pending/filled order IDs, fill reconciliation and audit events. Network-uncertain, canceled or unconfirmed orders stop further core buys until reviewed; never blindly retry. Contributions whose 25% per-ETF share is less than Alpaca's $1 minimum are recorded as deferred and not automatically aggregated into a later buy. A pre-existing externally acquired VT/SGOV position blocks initial tracking to avoid incorrect attribution.

**No seller:** \`JSPierceColorado/alpaca-seller-bot\` independently excludes VT and SGOV from **all exits** (both loss and profit). Other sellers/broker-originated liquidations are beyond the scope of this safeguard. The AI Recommendations Buyer also excludes these two symbols from any regular research orders, keeping origin attribution clean. V2 score entries remain 2% of available margin buying power, and S&P100 low-water holdings retain six-step rebuy rules.

**Daily audit:** \`CORE_DEPOSIT_CYCLE_COMPLETE\`, \`CORE_NEW_CASH_DEPOSIT\`, \`CORE_BUY_SUBMITTED\`, \`CORE_ORDER_FILLED\`, \`CORE_OUTSTANDING_ORDER_REVIEW\` and the other \`CORE_...\` logs expose contribution amounts and delayed actions. Read-only snapshot/performance reconciliation is separate; no orders or prices are considered fills until Alpaca confirms them.

**Testing:** \`python -m unittest discover -s tests -v\` includes cash-only, duplicate-order, old-deposit, broker-timeout, stable-account-identity and scheduler checks.

