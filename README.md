# V2 buyer bot — noon Denver, Railway

This is an **always-on** Python service. At **12:00 PM Denver time (`America/Denver`) every Monday–Friday**, it checks whether the Alpaca stock market is open. If open, it reads your V2 Google Sheet's `Screener` tab, selects tickers scoring **95 or higher in column G**, and places a **DAY market buy** for **2% of current Alpaca buying power** per eligible symbol. Buying power can include margin, and the amount is recalculated before every order. If the market is closed (including holidays or early-close afternoons), it skips the day.

It does not write to the spreadsheet or modify column G. It **does not** check sheet freshness, universe size, old tail rows, or S&P 100 membership. Your **separate screener bot** must refresh the intended universe before noon. The buyer only reads rows 1–500 (more than enough for the S&P 100 after your row cleanup). If the sheet still contains old scores, those are eligible to trade; run the screener first.

## Rules

- Score in `G` must be at least **95**; symbol in `A` must be valid.
- Skip symbols already held in Alpaca, including fractional positions.
- Skip symbols with open orders, or already attempted under this bot's deterministic daily Alpaca order ID. These checks protect against duplicate purchases after restarts.
- Skip stocks not tradable/fractionable, and orders under $1.
- Trade during an open market only; 2% is of **current Alpaca `buying_power`**, which includes available margin, *not* cash or equity. Margin brings borrowing costs and forced-liquidation risk.
- One cycle per weekday at noon; the service remains running between cycles. Railway **Cron should be left off**. A deployment started **after noon** waits until the next weekday noon; it does not immediately buy.
- The bot never sells, limits overall portfolio exposure, or guarantees that a technical score predicts positive returns. A separate seller/risk-management strategy is required.

## Project files

- `main.py` — scheduling, Google Sheets reads, buy logic
- `requirements.txt` — Python dependencies
- `Dockerfile` — Railway service image
- `railway.json` — always-restart, no cron
- `tests/test_main.py` — offline tests

## GitHub / Railway

1. Create a private GitHub repository and upload the files to its root.
2. In Railway, create a new service **Deploy from GitHub Repo**, one replica, always on. Do **not** configure a cron expression.
3. Enable Google Sheets API, create a Google service-account JSON key, and share your V2 sheet with its `client_email` as a **Viewer**.
4. Set the variables in Railway (never check secrets into the repository):

| Variable | Default | What it does |
| --- | --- | --- |
| `APCA_API_KEY_ID` | Required | Alpaca key |
| `APCA_API_SECRET_KEY` | Required | Alpaca secret |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Required | Entire Google service-account JSON |
| `GOOGLE_SPREADSHEET_ID` | `14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ` | V2 Sheet |
| `GOOGLE_SHEET_TAB` | `Screener` | Source tab |
| `MINIMUM_SCORE` | `95` | Minimum G score |
| `BUY_FRACTION` | `0.02` | 2% of current buying power (maximum 0.02) |
| `ALPACA_PAPER` | `true` | Paper account by default |
| `DRY_RUN` | `true` | Log, but don't submit orders |
| `LIVE_TRADING_ACK` | unset | Must be `I_ACCEPT_LIVE_MARGIN_RISK` to enable live trades |

First test with **paper keys and `DRY_RUN=true`**. Set `DRY_RUN=false` to test paper orders. For live orders, set `ALPACA_PAPER=false`, `DRY_RUN=false`, `LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK`, and use live keys. Market orders and margin use real buying power and carry risk.

## Testing

```sh
python -m unittest discover -s tests -v
```
