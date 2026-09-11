"""Event model + Telegram message formatting for FPFSSL8.2 alerts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# Event kinds emitted by the engine
K_FOOTPRINT = "footprint"        # confirmed footprint-supported OB created
K_TAP = "tap"                    # source-compatible TAP (reference touched, live)
K_APPROACH = "approach"          # price approaching a TAP reference (informational)
K_DEFENCE = "defence"            # source defence confirmed after a TAP
K_ZONE_INVALID = "zone_invalid"  # zone invalid (stop / tap limit / cap)
K_SSL_CREATED = "essl_created"   # new eSSL reference published
K_ISSL_CREATED = "issl_created"  # new iSSL reference published
K_ESSL_TAP = "essl_tap"          # price tapped an active eSSL level (live + confirmed)
K_ESSL_SWEEP = "essl_sweep"      # confirmed eSSL penetration WITH close reclaim (liquidity grab)
K_ESSL_BREAK = "essl_break"      # confirmed eSSL penetration WITHOUT reclaim
K_FRESH_ESSL = "fresh_essl"      # new FRESH confirmed (unswept) eSSL-scale low reference


@dataclass
class Event:
    kind: str
    bar: int                       # 0-based bar index
    date: str                      # bar date YYYY-MM-DD
    confirmed: bool                # False -> intraday (provisional) observation
    symbol: str = ""
    zone_id: int | None = None
    pool_id: int | None = None
    price: float | None = None     # primary price (tap low / level / close)
    price2: float | None = None    # secondary price (reference / stop / level)
    extra: dict = field(default_factory=dict)

    @property
    def date_dt(self) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(self.date, fmt)
            except ValueError:
                continue
        return datetime.min


# ---------------------------------------------------------------------------
# Message formatting (Telegram HTML)
# ---------------------------------------------------------------------------
def _p(x, nd=2) -> str:
    if x is None:
        return "--"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v == 0:
        return "0"
    s = f"{v:,.{nd}f}".rstrip("0").rstrip(".")
    return s


def _zone_line(ev: Event) -> str:
    e = ev.extra
    rvol = e.get("rvol")
    rvol_s = f"{float(rvol):.2f}" if rvol is not None else "--"
    adj = e.get("new_reference")
    show_adj = adj is not None and abs(float(adj) - float(e.get("reference", adj))) > 1e-9
    return (
        f"🧱 FP-OB #{ev.zone_id} <b>{_p(e.get('bottom'))}-{_p(e.get('top'))}</b> "
        f"(TAP {e.get('taps', 0)}/{e.get('max_taps', 4)}, {e.get('state', '')})\n"
        f"   ref <b>{_p(e.get('reference'))}</b>"
        + (f" ← adj <b>{_p(adj)}</b>" if show_adj else "")
        + f" | stop <b>{_p(e.get('invalidation'))}</b>\n"
        f"   {e.get('evidence', '')} | RVOL {rvol_s} | obs {e.get('observations', '--')}\n"
        f"   born {e.get('born_date', '--')} | structure {_p(e.get('structure'))} | "
        f"displaced {e.get('displacement_date', '--')}"
        + (f"\n   💧 SSL ctx: {e['ssl_note']}" if e.get("ssl_note") else "")
    )


def _essl_line(ev: Event) -> str:
    e = ev.extra
    members = e.get("members", 1)
    eq = "EQL x%d" % members if members > 1 else "single"
    depth = e.get("depth", 0.0) or 0.0
    pen = e.get("penetrated", False)
    re = e.get("reclaimed", None)
    line = (
        f"💧 eSSL level <b>{_p(ev.price)}</b> ({eq}, age {e.get('age_bars', '--')} bars, "
        f"origin {e.get('origin_date', '--')})\n"
        f"   tap low <b>{_p(e.get('low'))}</b>"
    )
    if pen:
        line += f" | penetrated <b>{_p(depth)}</b> below level"
    else:
        line += " | touched the level"
    if re is True:
        line += "\n   close <b>%s</b> back above level → <b>RECLAIMED ✅</b>" % _p(e.get("close"))
    elif re is False:
        line += "\n   close <b>%s</b> below level → NOT reclaimed ⚠️" % _p(e.get("close"))
    return line


def _head(symbol: str, tf: str, ev: Event, title: str, emoji: str) -> str:
    live = "" if ev.confirmed else " • <b>LIVE (intraday bar — provisional)</b>"
    return f"{emoji} {title}\n📈 <b>{symbol}</b> ({tf}) {ev.date}{live}\n"


def format_composite(symbol: str, tf: str, tap: Event, essl: Event) -> str:
    """Primary user alert: price TAPPed an eSSL level on a bar where a
    footprint-source TAP also fired (all remaining rules matched)."""
    head = _head(symbol, tf, tap, "eSSL TAP + Footprint TAP — ALL RULES MATCH", "🚨")
    parts = [head, _essl_line(essl), "", _zone_line(tap)]
    if tap.extra.get("sweep"):
        parts.append("   🔻 lower-low sweep of prior %d-bar low" % tap.extra.get("sweep_length", 5))
    parts.append("")
    parts.append("Action: source-compatible long reference at TAP. Stop below OB invalidation.")
    return "\n".join(parts)


def format_event(symbol: str, tf: str, ev: Event) -> str:
    e = ev.extra
    if ev.kind == K_TAP:
        head = _head(symbol, tf, ev, f"Footprint TAP {e.get('taps', '')} — source reference touched", "🔻")
        parts = [head, _zone_line(ev)]
        if e.get("adjusted"):
            parts.append("   ⚙️ first-TAP reference adjustment applied (raised ref, source-identical)")
        if e.get("sweep"):
            parts.append(f"   🔻 lower-low sweep of prior {e.get('sweep_length', 5)}-bar low")
        return "\n".join(parts)
    if ev.kind == K_APPROACH:
        head = _head(symbol, tf, ev, "Footprint approach (getting near TAP reference)", "👀")
        return "\n".join([head, _zone_line(ev)])
    if ev.kind == K_DEFENCE:
        head = _head(symbol, tf, ev, "Source DEFENCE CONFIRMED after TAP", "✅")
        rvol_def = e.get("rvol_def")
        clv = e.get("clv")
        parts = [
            head,
            f"   close <b>{_p(ev.price)}</b> | RVOL {f'{float(rvol_def):.2f}' if rvol_def is not None else '--'}"
            f" | CLV {f'{float(clv):.2f}' if clv is not None else '--'}\n",
            _zone_line(ev),
        ]
        return "\n".join(parts)
    if ev.kind == K_ZONE_INVALID:
        emoji = "🛑" if e.get("reason", "").startswith("Source") else "🧹"
        head = _head(symbol, tf, ev, f"FP-OB #{ev.zone_id} INVALID — {e.get('reason', '')}", emoji)
        parts = [head, f"   close {e.get('close') and _p(e.get('close')) or '--'} | fixed stop was {_p(e.get('stop'))}"]
        return "\n".join(parts)
    if ev.kind == K_FOOTPRINT:
        head = _head(symbol, tf, ev, f"Footprint OB #{ev.zone_id} confirmed (all formation rules met)", "🏗️")
        parts = [head, _zone_line(ev)]
        parts.append(f"   entry ref {_p(e.get('reference'))} → TAP armed when price returns to it")
        return "\n".join(parts)
    if ev.kind == K_ESSL_TAP:
        head = _head(symbol, tf, ev, "eSSL TAP — price touched the eSSL level", "💧")
        parts = [head, _essl_line(ev)]
        if e.get("tap_filtered"):
            parts.append("   ℹ️ a footprint TAP fired on this bar but was filtered "
                         f"({e['tap_filtered']}) — eSSL touch alert only")
        else:
            parts.append("   ℹ️ eSSL level touch — fires on every touch of an active "
                         "eSSL level (fresh or old, no footprint TAP required)")
        return "\n".join(parts)
    if ev.kind == K_ESSL_SWEEP:
        head = _head(symbol, tf, ev, "eSSL SWEEP + RECLAIM — liquidity grabbed at external low", "🌀")
        parts = [head, _essl_line(ev)]
        return "\n".join(parts)
    if ev.kind == K_ESSL_BREAK:
        head = _head(symbol, tf, ev, "eSSL BREAK — close below external liquidity level (not reclaimed)", "⚠️")
        parts = [head, _essl_line(ev)]
        return "\n".join(parts)
    if ev.kind in (K_SSL_CREATED, K_ISSL_CREATED, K_FRESH_ESSL):
        scale = "eSSL" if ev.kind in (K_SSL_CREATED, K_FRESH_ESSL) else "iSSL"
        head = _head(symbol, tf, ev, f"New {scale} reference published", "🆕")
        e2 = ev.extra
        members = e2.get("members", 1)
        eq = f" (EQL x{members})" if members > 1 else ""
        return "\n".join([head, f"   {scale} level <b>{_p(ev.price)}</b>{eq} • origin {e2.get('origin_date', '--')}"])
    # generic fallback
    return f"{symbol} {ev.date} {ev.kind} {ev.price}"
