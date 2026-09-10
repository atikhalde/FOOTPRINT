# FPFSSL8.2 — Deep Analysis of the Pine Script

**Script:** `Footprint Source TAP + Fresh SSL + Developing Preview v8.2` (`FOOTPRINT ESSL.txt`, Pine v6, `overlay=true`)

This document is a complete, section-by-section analysis of the indicator, its state
machines, its exact arithmetic, and its guarantees — and how the Python port in this
repository reproduces it bar-for-bar on **any timeframe** (daily or intraday —
the Pine script is timeframe-agnostic and so is the port).

---

## 1. What the script actually is

Despite the "Footprint" name, the script **does not use real footprint / volume-at-price
data**. Its own header states it plainly:

> *OHLCV evidence is a proxy: institutional identity, hidden quantity, actual volume at
> a price, remaining orders and broker fills are NOT observed.*

It is a **liquidity & structure engine** built from ordinary candles:

1. **Primary output** — "footprint-supported" bullish **order blocks (OBs)**:
   zones created only when a high-volume absorption-style bar at prior support is later
   *price-confirmed* by a full sequence (departure → strong displacement → buffered
   break of a frozen structure high), with the zone's geometry frozen to the nearest
   bearish candle before that departure.
2. **LIVE trading reference** — for every confirmed OB, a *source-compatible* state
   machine that tracks: first **departure** (price leaves the zone up), **TAP**
   (price comes back down to a reference line), a **first-TAP reference adjustment**,
   **defence confirmation**, **timeout reset**, and **invalidation**.
3. **Context layer** — sell-side liquidity (SSL) references: **iSSL** (internal pivot
   lows), **eSSL** (external/major pivot lows), **EQL** clustering of equal lows,
   sweep/reclaim/break classification, a **FRESH confirmed low** registry, and a
   **developing-low preview** (explicitly unconfirmed).

Design invariants the script enforces (and that the port preserves):

* Zones are created **only at confirmed closes**; pending setups are never drawn or
  signalled.
* TAP/approach/invalidity run **live and provisional** on the still-forming bar.
* Defence is **close-confirmed** only.
* First-departure origin and zone geometry **never move** after confirmation.
* SSL is **context only** — it never gates footprint/OB creation.
* No tables, no strategy orders, no alerts in the source (the Python port *adds*
  alerts; everything else is a 1:1 port).

---

## 2. Top-level architecture

One pass per bar, in a **fixed order** (the port follows this order exactly — it is
load-bearing):

```
(A) if confirmed:  setup lifecycle stops (invalidation, deadline, grace expiries)
(B) if confirmed:  SSL pool lifecycle + external range bookkeeping + pivot emission
(C) if confirmed:  FRESH SSL registry + developing preview
(D) if confirmed:  confirmation state machine (newest setup first):
                    structure latch → break latch → departure/origin freeze →
                    displacement → eligibility → OB creation
(E) if confirmed:  structure-high publication (new pivot high / consumption)
(F) if confirmed:  footprint evidence registration (LAST) + record pruning
(G) every bar (incl. forming):  source-TAP state machine over all active zones
```

Why the order matters:

* **(A) before (B)** — failed evidence cannot be rescued by a later condition.
* **(B) before new pivots are published** — a newly recognised SSL reference cannot be
  swept on its own creation bar (`t > bornBar` guard).
* **(D) before (E)** — a setup can latch the structure high that is *about to be
  published this bar* only via its own latch rule (`knownStructureBar > f.knownBar`);
  registration-time latching in (F) uses the value published in (E) of the same bar.
* **(F) last** — a footprint observed on the current bar cannot retrospectively confirm
  the current bar's earlier structure event.
* **(G) last, ungated** — the TAP pass is the live layer: it runs on the forming bar
  too, which is what makes intraday TAP alerts possible.

The four record types (Pine `type`s) and their lifecycles:

| Type | Meaning | Created in | Terminated by |
|---|---|---|---|
| `FootprintSetup` | pending evidence base (hidden) | (F) | invalidation, 20-bar deadline, displacement/break grace expiry, OB creation, cap |
| `FootprintOB` | confirmed zone + source-TAP state | (D) | live-close stop, tap-count > max, record cap |
| `SSLPool` | iSSL/eSSL liquidity level (EQL-aware) | (B) | first full penetration (sweep/break), 250-bar expiry, range reclassification, EQL supersede, cap |
| `FreshSSLReference` | confirmed unswept low (FRESH registry) | (C) | penetration of ≥1 tick (`low ≤ price − tick`) |

---

## 3. Subsystem A — primary footprint evidence (`f_shape`)

Evidence = "institutional absorption/rejection" proxied from one candle (or a short
cluster) **at prior support**, with volume proof.

### The bar shape test `f_shape(offset, atr, support, meanVolume, minRVOL)`

Evaluated on the bar `offset` bars ago; all seven conditions must hold:

| # | Condition (defaults) | Purpose |
|---|---|---|
| 1 | `volume / meanVolume ≥ minRVOL` | relative volume proof (no zero-volume bars) |
| 2 | `\|close − open\| ≤ 0.50 × ATR` | small body = no conviction move against |
| 3 | `high − low ≤ 1.50 × ATR` | contained range = absorption, not distribution |
| 4 | `(close − low) / range ≥ 0.60` | close location in upper part of the bar |
| 5 | `(min(open,close) − low) / range ≥ 0.20` | a real lower wick reaching into the level |
| 6 | `max(close[prev] − close, 0) ≤ 0.50 × ATR` | **gap-down guard**: the close must not be far *below* the previous close even if the body is small (a gap-down with a tiny body is not "absorption") |
| 7 | `low ≤ support + 1.0 × ATR` and `close ≥ support` | the bar sits at (or just above) prior support and holds it |

Note condition 6 uses `close[offset+1]` — in Pine syntax that is the **previous**
close, not a future value.

### Single vs repeated evidence

* **Single**: prior 20-bar volume baseline is ready and `f_shape(0, priorATR,
  support[1], priorVolumeMean, RVOL ≥ 1.50)` on the current bar.
* **Repeated** (fallback): the *pre-window* baselines must be ready (`atr[−5]`,
  `volMA[−5]`, `support[−5]` — the base cannot inflate its own reference), the current
  bar passes `f_shape` with the looser per-bar RVOL `≥ 1.05`, then the last 5 bars are
  counted: need `≥ 2` matches, **mean RVOL ≥ 1.20**, and the union of the window's
  highs/lows must be `≤ 2.50 × baseATR` wide (a real base, not a trend leg).

On a hit, a `FootprintSetup` is registered with:

```
top/bottom  = rounded evidence high/low — for REPEATED evidence these are the
              UNION of ALL candles from the current bar back to the oldest
              matched bar (not just the current candle)
startBar    = evidenceStart: the current bar (single) or bar_index − firstOffset
              (repeated: the OLDEST matched bar of the cluster)
atr         = priorATR (single) or baseATR (repeated)
invalidation = floorTick(bottom − max(0.10 × atr, 1 tick))
structure   = latched to the known structure high if one exists and is unconsumed
```

`startBar` matters downstream: the structure latch requires
`knownStructureOrigin >= f.startBar`, the departure link check requires
`originBar >= f.startBar − 3`, and the duplicate check refuses to recycle
observations (`evidenceStart <= old.knownBar`).

Duplicate suppression: a new base overlapping `≥ 75%` of an active pending base, or
recycling observations already consumed by a used setup, is rejected.

---

## 4. Subsystem B — confirmation state machine (setup → OB)

A pending setup must pass, in order, **within 20 bars** of observation:

1. **Integrity** — each bar: `close < invalidation` kills it ("Footprint area
   failed"); also kills it if the frozen origin's invalidation is hit.
2. **Structure latch** — the setup needs a *frozen known high* to break. It latches
   the latest internal (3-bar) pivot high (config: `Internal`/`External`) that
   (a) was published **after** the setup was known, (b) originated at/after the base,
   and (c) is at or above the base top. Once latched it can **never be lowered or
   swapped**. A close above the known high *consumes* it (it must be re-latched from a
   fresh pivot).
3. **Buffered break** — `close > structure + 0.05 × ATR` latches `breakBar`. The
   setup must respond within `breakGrace = 2` bars and never close back ≤ structure.
4. **First departure → origin freeze** — the *first* bar with
   `close > baseTop + 0.10 × ATR` freezes everything:
   * the **nearest bearish candle within 12 bars** (scanning back from the departure
     bar, current bar excluded) is the opposing origin — a later, nicer candle can
     never be substituted;
   * the OB zone = that candle's **precision boundary** (default *lower half of
     candle*: `top = low + (high−low)/2`, `bottom = low`), tick-rounded;
   * zone invalidation = `floorTick(originLow − max(0.10 × ATR, 1 tick))`;
   * **link check**: origin ≥ `baseStart − 3` bars and ≥ 1 tick of price overlap with
     the base;
   * **validity check**: width in `[2 ticks, 2.5 × ATR]`, `0 < invalidation ≤ bottom`;
   * **leg-state replay** of every bar between origin and now (chronological): a bar
     closing above `zoneTop + 0.5 × ATR` *arms*; the next bar overlapping the zone
     counts a **contact**; ≥ 3 contacts, any close below zone invalidation, no link,
     or invalid geometry all **kill the setup** ("Initiating origin not valid /
     footprint-linked"). This is what prevents re-using a level that has already been
     traded through.
5. **Strong displacement** — within `2` bars of the departure: a bullish bar with
   `range ≥ 0.80 × priorATR`, `body ≥ 50% of range`, and close location `≥ 0.65`.
6. **Eligibility** — displacement known, break known and ≤ 2 bars old, and
   `close > max(baseTop, structure, obTop) + 0.05 × ATR`. Then: re-run the leg-state
   replay (contacts must still be < 3 and nothing broken), reject duplicates (same
   origin, or ≥ 75% overlap with a live zone), and **create the OB**.

Creation arithmetic (with defaults):

```
midpoint     = roundTick((top + bottom) / 2)
stop         = bottom − 14-bar ATR × 0.15          # fixed at creation
reference    = top + buffer                         # "Proximal" depth = 0
buffer       = min( max(2 ticks, 0.18 × ATR), 0.40 × width )
sslNote      = nearest prior sweep/reclaim event within 10 bars and 1.0 × ATR (context only)
```

---

## 5. Subsystem C — source-compatible TAP state machine (per OB)

Runs on **every bar** (forming bar included — provisional until close), in the exact
source order: **departure → approach → TAP/adjustment → defence → timeout →
invalidity**.

State: `0 ready → 1 tapped/pending → 2 defended → −1 invalid`.

1. **Departure latch** (high-based, as in the source): once
   `high ≥ top + 1.0 × ATR`, `departed := true`. It latches on the birth bar if the
   breakout is violent, and is **never reset**.
2. **Approach** (informational): `departed ∧ age ≥ 3 ∧ close > ref ∧
   low ≤ ref + 0.25 × ATR`.
3. **TAP** — the exact source condition:
   `state ≥ 0 ∧ departed ∧ age ≥ 3 ∧ low ≤ reference ∧ high ≥ zoneBottom`.
   It is a **reference-based price condition, not a verified fill** (the reference may
   sit above the zone; `high ≥ bottom` is a sanity floor, not overlap proof).
   On a qualifying bar (one per bar, tap count increments per qualifying bar):
   * `taps += 1`, state → 1;
   * **immediately on TAP 1** (before any defence, faithfully to the source):
     `reference := max(reference, low + 0.05 × ATR)` — the "next reference" the label
     shows; it only moves *up*, so a deeper wick that doesn't close strength arms a
     higher retry level.
4. **Defence** (close-confirmed only): within 3 bars of the latest tap —
   `bullish ∧ CLV ≥ 0.65 ∧ RVOL ≥ 1.3 ∧ close > zoneTop ∧ close > max(last 3 highs)`
   (rolling micro-BOS) → state 2 ("DEFENCE CONFIRMED"). Note CLV/RVOL here use
   **current-inclusive** ATR/volume averages, unlike the displacement test which uses
   prior-bar values — the script is deliberate about this split.
5. **Timeout reset** — state 1 with no defence within 3 bars → state 0. The tap
   count, reference, and departure latch are **not** reset (source behaviour).
6. **Invalidity** — checked *after* tap/defence, so at `maxTouches = 4` a 5th TAP can
   still be printed on the same bar that retires the zone:
   `state ≥ 0 ∧ (close < fixedStop ∨ taps > 4)` → state −1, zone retired
   ("Source live-close stop condition" / "Source tap count exceeded maximum").

The stop is **fixed at creation** (`bottom − 0.15 × ATR`); live invalidation uses
`close < stop`, not wicks.

---

## 6. Subsystem D — SSL (sell-side liquidity) engine

### Pivots and scales

* `iSSL`: 3-bar pivot lows **inside** the current external range.
* `eSSL`: 10-bar (major) pivot lows — the **external range boundary** itself.
* A pivot is confirmed `rightbars` bars after its origin (3 and 10 respectively); the
  script tracks the *latest* confirmed major high/low on the chart (no weekly request).

### External range bookkeeping

* `close < majorLow` → range **broken** (a new major low is needed to heal it; a
  mere close-recovery does not).
* A new confirmed major low resets `rangeBroken` and bumps a range key.
* `externalRangeReady = not broken ∧ majors known ∧ majorHigh > majorLow`.
* iSSL pools that fall **outside** `[majorLow, majorHigh]` are retired as
  `OLD_RANGE` — *not* labelled sweeps (old-range removal ≠ liquidity event).
* The same physical low may first be iSSL and **later** become the eSSL boundary: the
  internal record ends, a **new** eSSL reference starts at the later recognition.

### EQL clustering (equal lows)

A newly recognised low merges into an active same-scope pool when its distance to the
pool anchor ≤ pool tolerance **and** the merged band width stays ≤ tolerance.
Tolerance is frozen at the first member: `max(tick, 0.05 × creationATR)` — a band can
never chain itself wider. The old pool is retired as `CLUSTERED`; the composite pool
(starting *now*, with old first-origin and `members + 1`) replaces it. **Consumed**
(old, inactive) pools can never qualify a fresh EQL band.

### Pool lifecycle (first full penetration is terminal)

On a bar after birth:

* `low ≤ lower − 1 tick` → **full penetration**, classify:
  * `whole bar below` → `GAP_THROUGH` (if opened below from above) else `CLOSED_BELOW`;
  * else `close ≥ upper + 1 tick` (reclaim) → `GAP_RECLAIM` (gap open from above) /
    `SWEEP` (came from above, opened above) / `RECOVERY` (was already below);
  * else `close < lower` → `CLOSED_BELOW`; else `NO_RECLAIM` (breached, not reclaimed).
  * Only `SWEEP`/`GAP_RECLAIM` are counted as reclaim **events** (stored in a 60-event
    ring used for OB creation context).
* `age ≥ 250 bars` → `EXPIRED`.
* overlap without penetration → `PARTIAL` (low below the band) or `TOUCHED`.

### FRESH confirmed lows + developing preview

* Every confirmed pivot low (internal and/or external, per `freshLowSource`) enters
  the FRESH registry at its price (tick-rounded), with **origin bar** and first-known
  timestamp. A later **major** recognition of the *same origin* only flips
  `majorSeen` — it does **not** invent a new low or revive a breached one.
* A reference is **breached** when a later bar prints `low ≤ price − tick` (touching
  or equal lows do **not** consume it). "FRESH" = confirmed + never penetrated.
* **Developing preview**: a current bar making a new low within the last 3 bars shows
  an explicitly **UNCONFIRMED** line; it can move or vanish and never enters the
  registry. Native pivot confirmation (3 right-side bars) is the only gate.

---

## 7. Exact formula reference (defaults)

| Quantity | Formula |
|---|---|
| ATR (both) | RMA(TR, 14) — SMA-seeded Wilder |
| priorATR / priorVolMA | value at bar `t−1` (displacement & evidence baselines) |
| sourceRVOL / sourceCLV | current-inclusive: `vol / SMA(vol,20)`, `(close−low) / max(range, tick)` |
| strongDisplacement | `close>open ∧ range ≥ 0.8·priorATR ∧ body ≥ 0.5·range ∧ (close−low)/range ≥ 0.65` |
| support | `min(low, 10)` (incl. current); evidence uses `support[1]` |
| evidence RVOL (single / repeated) | `vol / volMA[1] ≥ 1.50` / per-bar `≥ 1.05`, mean `≥ 1.20` |
| base width | `≤ 2.50 × ATR` |
| zone precision (default) | `top = low + (high−low)/2`, `bottom = low` (tick-rounded) |
| zone stop | `bottom − 0.15 × ATR_birth` (fixed) |
| TAP reference | `top + min(max(2 ticks, 0.18 × ATR), 0.40 × width)` ("Proximal") |
| first-TAP adjustment | `ref := max(ref, low + 0.05 × ATR)` |
| TAP condition | `departed ∧ age ≥ 3 ∧ low ≤ ref ∧ high ≥ bottom` (sweep `low < min(low[1..5])` optional, default off) |
| defence | `pending ≤ 3 bars ∧ bullish ∧ CLV ≥ 0.65 ∧ RVOL ≥ 1.3 ∧ close > top ∧ close > max(high[1..3])` |
| eSSL/iSSL pivot | 10 / 3-bar `pivotlow(low, d, d)`, confirmed `d` bars later |
| EQL tolerance | `max(tick, 0.05 × ATR)` frozen at first member |
| sweep | full penetration (≥1 tick below lowest bound) + close ≥ highest bound + 1 tick |
| FRESH breach | `low ≤ price − tick` on any later bar |

---

## 8. What the Python port adds (and does NOT change)

Everything above is ported 1:1 (verified by the hand-crafted scenario suite in
`tests/test_engine.py`). The additions are all in a clearly-marked **group 8** layer:

1. **eSSL tap events** — the source has *no alerts*. The port emits `essl_tap`
   (touch / partial / full penetration, with the script's own penetration & reclaim
   definitions), `essl_sweep`, and `essl_break` so the scanner can alert.
2. **The composite ALL-RULES signal** (`essl_ob_tap`): on one bar, an active
   eSSL level is tapped **and** a confirmed FP-OB's source-TAP condition fires —
   i.e. *price taps the eSSL level with all the remaining rules matched*. This is the
   primary Telegram alert and the primary backtest strategy.
3. **`essl_created` / `fresh_essl` events** for tracking new eSSL levels as they form.
4. Scanner plumbing (polling, dedupe state, cooldowns, Telegram), backtest harness,
   and `report` introspection — none of which alters engine semantics.

Known-faithful edge behaviours preserved (each has a test or code comment):

* TAP 5 prints *before* retirement on the same bar (`> maxTouches`, not `≥`).
* First-TAP adjustment happens **before** defence, on TAP 1 only.
* Defence uses current-inclusive averages; displacement uses prior-bar ATR.
* A new SSL reference cannot be swept on its creation bar.
* Range break needs a *close* below the major low; recovery does not heal it.
* Consumed EQL members cannot chain a band; tolerance is frozen at first member.
* Pivot ties: a candidate qualifies when it equals the window min/max (value equality,
  as in Pine).

---

## 9. Honest limitations (inherited from the source)

* **Proxy evidence**: RVOL/CLV/shape is an *inference* of absorption, not observed
  quantity at price. Identity, resting quantity, and fills are unknown.
* **Reference-based TAP**: `low ≤ reference` is a price condition; the reference can
  sit above the zone and a "TAP" is not a verified OB overlap or fill.
* **Pivot confirmation lag**: eSSL recognition lags its origin by 10 bars;
  iSSL by 3. Levels are therefore always slightly stale by design.
* **OHLCV-only**: gaps, halts, corporate actions arrive as raw bars; the script has no
  adjustment logic beyond what the data feed provides.
* **Parameter sensitivity**: 40+ inputs; the defaults are the script author's, and the
  composite signal is deliberately rare (a deep pullback that *both* taps a major low
  *and* satisfies a confirmed OB's TAP reference on the same bar).
