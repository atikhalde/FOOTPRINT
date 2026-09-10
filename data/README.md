# data/ — CSV input for `--source csv`

Drop one file per symbol here: `<SYMBOL>.csv` (or `<SYMBOL_>.csv` with `.` replaced
by `_`), e.g. `RELIANCE.NS.csv` or `AAPL.csv`.

Format (header required, one row per daily bar):

```
date,open,high,low,close,volume
2026-09-09,2450.10,2462.50,2448.30,2459.90,1254300
2026-09-10,2459.90,2470.15,2451.35,2466.40,1512780
```

* `date` = `YYYY-MM-DD` (or `YYYYMMDD`)
* tick size is auto-detected from the prices (override in `config.yaml` →
  `data.tick_overrides` if needed, e.g. NSE stocks tick in 0.05)
* TradingView "Export raw data" (daily) works: rename the columns to the names above.

The files in this directory are git-ignored. For an offline demo, generate
deterministic *synthetic* CSVs (NOT real market data):

```bash
python -m fpfssl sample-data --bars 1200
python -m fpfssl backtest --source csv
python -m fpfssl scan --once --source csv --dry-run
```
