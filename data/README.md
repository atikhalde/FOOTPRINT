# data/ — CSV input for `--source csv`

Drop one file per symbol here: `<SYMBOL>.csv` (or `<SYMBOL_>.csv` with `.` replaced
by `_`), e.g. `RELIANCE.NS.csv` or `AAPL.csv`.

Format (header required, one row per bar — daily or intraday):

```
date,open,high,low,close,volume
2026-09-09 09:15,2450.10,2462.50,2448.30,2459.90,1254300
2026-09-09 09:30,2459.90,2470.15,2451.35,2466.40,1512780
```

* `date` = `YYYY-MM-DD` (daily) or `YYYY-MM-DD HH:MM` (intraday; or `YYYYMMDD`)
* NSE/BSE (`.NS`/`.BO`) resolve to the 0.05 tick automatically; override in
  `config.yaml` → `data.tick_overrides` only if needed
* TradingView "Export raw data" works on any TF: rename the columns to the names above.

The files in this directory are git-ignored. For an offline demo, generate
deterministic *synthetic* CSVs (NOT real market data):

```bash
python -m fpfssl sample-data --bars 1200
python -m fpfssl backtest --source csv
python -m fpfssl scan --once --source csv --dry-run
```
