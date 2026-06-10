# Options Sensi — options opportunity scanner

A web app that scans a configurable watchlist on an interval (default 5 min),
computes options metrics per ticker, stores snapshots in SQLite, and flags
unusual activity in a live signal feed.

## Run it

Requires [uv](https://docs.astral.sh/uv/).

```bash
./run.sh
```

That syncs the environment from `pyproject.toml`/`uv.lock`, picks the first
free port from 8000 upward (so it won't collide with anything else you're
running), starts the server — which hosts both the API and the dashboard —
and opens the browser. `PORT=9000 ./run.sh` forces a port; `NO_OPEN=1`
skips the browser; extra args pass through to uvicorn (e.g. `./run.sh --reload`).

Add tickers and you're done. The first scan runs at boot and whenever you add
a symbol; afterwards on the configured interval.

## Implemented signals

| Signal | What it detects | Why it matters |
|---|---|---|
| `iv_premium` | ATM IV ≥ 1.25× 20-day HV | Options pricing in a bigger move than the stock has realized — possible event/catalyst, or rich premium to sell |
| `iv_spike` | ATM IV rising ≥10% vs the average of recent scans | Someone is bidding up vol *right now* |
| `unusual_volume` | Contract volume on pace for ≥ 2× open interest (min 500 lots at day pace) | New positioning today, not closing of old positions |
| `put_call_ratio` | P/C volume ratio > 2.0 or < 0.4 | One-sided directional flow |
| `gamma_build` | Net gamma exposure up ≥25% scan-over-scan | Dealer hedging pressure building — amplifies or dampens moves |
| `gamma_pin` | Peak gamma strike within 2% of spot | Pin/magnet risk into expiry |
| `skew_shift` | OTM put IV minus OTM call IV moved ≥4 vol pts vs baseline | Crash protection getting bid, or upside being chased |

All thresholds are editable in the UI (the most-used ones) or via
`PUT /api/config` / `config.json` (all of them).

## More ideas worth adding

**Flow-quality signals** (need tick/trade-level data — Polygon, CBOE DataShop, or UW):
- **Sweeps vs blocks** — multi-exchange sweeps at the ask are aggressive, informed-looking buying; single-exchange blocks at mid are often institutional hedges.
- **Premium-weighted flow** — $5M of OTM calls in a quiet mid-cap matters more than $5M in SPY. Rank by premium / average daily premium.
- **Repeated hits** — the same strike/expiry getting bought across multiple scans is far more meaningful than one print.

**Vol-surface signals** (need a fuller chain history):
- **IV rank / IV percentile** — where today's IV sits in its own 52-week range; 90th percentile IV means rich premium, 10th means cheap optionality. (The snapshots table already accumulates the history you need for this.)
- **Term-structure inversion** — front-month IV above back-month = event premium; quantify the kink around a specific expiry to infer the event date the market expects.
- **Implied earnings move vs history** — straddle price around earnings vs the stock's average realized earnings move.

**Positioning signals**:
- **OI delta day-over-day** — volume tells you today's trading; *change in OI* tells you what stuck. Big new OI at a strike is a footprint.
- **Vanna/charm exposure** — beyond gamma: how dealer deltas shift as IV falls or time passes (drives post-event drift and OpEx flows).
- **Max pain drift** — strike minimizing option-holder value, and whether spot gravitates toward it into expiry.

**Cross-signals**:
- **IV up + price flat** — vol bid without a move is one of the cleanest "someone knows something" patterns.
- **Catalyst overlay** — join signals against an earnings/FDA/macro calendar; an IV spike *with no scheduled catalyst* is the interesting one.
- **Sector-relative IV** — a name's IV jumping while its sector ETF's IV is flat isolates idiosyncratic anticipation.

## Architecture

```
app/
  main.py                 FastAPI routes + APScheduler (interval rescheduled live)
  scanner.py              fetch → compute snapshot → persist → run detectors
  config.py               config.json load/save with defaults
  db.py                   SQLite: watchlist, snapshots, signals
  analytics/
    signals.py            one detector per signal kind
    black_scholes.py      greeks (Yahoo supplies IV, no solver needed)
    historical_vol.py     annualized close-to-close HV
  providers/
    base.py               OptionsDataProvider interface
    yfinance_provider.py  free, ~15-min delayed data
static/                   vanilla-JS dashboard (no build step)
```

## Caveats

- **Yahoo data is ~15 min delayed** and per-contract IV can be stale on illiquid
  strikes. For real edge, swap in Polygon/Tradier/ORATS via
  `providers/base.py` — the rest of the app doesn't change.
- Scheduled scans only run 9:30–16:00 ET Mon–Fri (`market_hours_only` in
  config). After hours, the manual **Scan now** button always works, and
  adding a symbol triggers an immediate scan of just that symbol so its row
  fills in right away. Exchange holidays are not modeled — holiday scans see
  stale data, and the signal cooldown absorbs the repeats.
- Volume-based signals are projected to full-day pace, and comparison signals
  (IV spike, gamma build, skew shift) baseline only against the current
  session, so they stay quiet for the first couple of scans each morning.
- Snapshots are kept 30 days and signals 90 (configurable), purged after each
  sweep; the DB runs in WAL mode for concurrent scanner/API access.
- Keep the watchlist modest (≲15 names at 5-min intervals) to avoid Yahoo
  rate-limiting; each symbol costs ~1 + `max_expirations` requests per scan.
- This flags *anomalies*, not trades. Everything here is a starting point for
  research, not financial advice.
