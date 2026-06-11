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

## The dashboard

- **Watchlist table** — symbol, last scanned price with day change vs the
  previous close (green ▲ / red ▼), IV/HV (highlighted when elevated),
  put/call ratio, and a 24h signal-count badge (🔥 marks a confluence in
  the last 24h). Click a row to
  expand full metrics (ATM IV, HV, volumes, peak gamma strike, skew, net GEX)
  and filter the feed to that name. Negative numbers render finance-style in
  parentheses.
- **Signal feed** — split into **Today** and **Older** sections; a purple
  **daily wrap** card lands at 4:15 PM ET summarizing every name's day
  (`POST /api/wrap` regenerates it manually); entries
  younger than ~2 scan intervals get a brighter card and a `new` pill so the
  latest information stands out. Click a signal's kind label (e.g. GAMMA
  FLIP) to jump straight to its explanation in the help glossary.
- **Help glossary** — the `?` button explains every metric and signal in
  plain English, including how to read skew, net GEX, and pinning.
- **Settings** — collapsible panel for the scan interval and the most-used
  thresholds; the header shows market open/closed and last/next scan times.

## Implemented signals

| Signal | What it detects | Why it matters |
|---|---|---|
| `iv_premium` | ATM IV ≥ 1.25× 20-day HV | Options pricing in a bigger move than the stock has realized — possible event/catalyst, or rich premium to sell |
| `iv_spike` | ATM IV rising ≥10% vs the average of recent scans | Someone is bidding up vol *right now* — the message says whether the stock was flat (often precedes news), falling (reactive hedging), or rallying (upside chase) alongside |
| `unusual_volume` | Contract volume on pace for ≥ 2× open interest (min 500 lots at day pace) | New positioning today, not closing of old positions |
| `put_call_ratio` | P/C volume ratio > 2.0 or < 0.4 | One-sided directional flow |
| `gamma_build` | Net GEX up ≥25% scan-over-scan (same sign, ≥ $5M floor) | Dealer hedging pressure building — message says whether it stabilizes or destabilizes the tape, and names the driver strikes |
| `gamma_flip` | Net GEX changed sign since last scan (≥ $5M floor) | Regime change: hedging switches between dampening moves (positive) and amplifying them (negative) |
| `gamma_pin` | Peak gamma strike within 2% of spot | Pin/magnet risk into expiry |
| `skew_shift` | OTM put IV minus OTM call IV moved ≥4 vol pts vs baseline | Crash protection getting bid, or upside being chased |
| `confluence` | ≥3 distinct signal kinds on one symbol within 4h | Independent detectors agreeing — the strongest pattern here; flagged 🔥 in the watchlist |
| `squeeze_setup` | ≥10% short float + ≥3 of: call-heavy flow, fresh OTM call buying, inverted skew, price+IV rising | Short-cover and dealer-hedging feedback loops aligned; warns when the naive GEX sign is likely inverted |
| `vol_compression` | 10d HV ≤ 0.6× 20d HV with IV not pricing expansion | Coiled spring — compressed ranges resolve violently, direction unknown |
| `daily_wrap` | Generated 4:15 PM ET each market day | One card per day: every name's move, IV change, top signal, and what stayed elevated at the close |

Signals carry **catalyst context**: earnings within 7 days appends
"event premium likely", while IV signals with a known earnings date more
than 14 days out get flagged "vol bid without an obvious catalyst" — the
interesting case. Unknown dates get no tag (absence of data isn't absence
of a catalyst). Windows configurable; earnings dates are fetched once per
symbol per day and shown in the detail panel.

All thresholds are editable in the UI (the most-used ones) or via
`PUT /api/config` / `config.json` (all of them).

## Roadmap

The live roadmap is the [issue tracker](https://github.com/vikbht/sensi/issues)
— features are `enhancement` issues grouped into
[milestones](https://github.com/vikbht/sensi/milestones), bugs carry root
cause and resolution, and commits close them with `Fixes`/`Closes #N`. The
themes below feed that backlog.

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
  market_clock.py         ET session math: market hours, day-pace projection
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
- Snapshots are kept 30 days (they feed future detectors like IV rank) and
  signals 5 days (alerts get stale fast), both configurable; purged after
  each sweep. The DB runs in WAL mode for concurrent scanner/API access.
- Keep the watchlist modest (≲15 names at 5-min intervals) to avoid Yahoo
  rate-limiting; each symbol costs ~1 + `max_expirations` requests per scan.
- This flags *anomalies*, not trades. Everything here is a starting point for
  research, not financial advice.
