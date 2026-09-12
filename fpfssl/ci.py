"""Self re-arm for the GitHub Actions scanner (session coverage without cron).

The live scanner's only job is to be *running while the market is*. GitHub's
`schedule` trigger is the classic way to arrange that and it is the weakest link
in the chain: the delivery is best-effort, ticks are late by minutes-to-hours
and whole hours of them are dropped (this repo: 2 usable ticks in ~80 expected
on a session day, both landing after the NSE close). Any design that maps
"one cron tick -> one pass" therefore spends the session scanning nothing.

This module closes the hole from the inside: a run that stops **before the
market day is over** dispatches exactly one successor, so the chain — not the
cron — is what covers the session. It is:

* inert outside Actions (no token / no repository in the environment),
* inert on dry runs (a preview must never re-arm the live scanner),
* capped at `scanner.reschedule_max_runs_per_day` per market day, and
* never re-armed after a user cancel (SIGINT/SIGTERM) or a finished session —
  those are exactly the two ways a chain is *supposed* to end.

The successor carries the inputs the current run was launched with (minus
`once`, which would defeat the point), so a manual "scan this subset" dispatch
keeps its own scope for the rest of the session.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

from .config import AppConfig

log = logging.getLogger("fpfssl.ci")

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

#: stop reasons that mean "I quit", not "the market day is over"
_USER_STOPS = ("received sigint", "received sigterm", "cancelled", "interrupted")
#: ... and these mean the run ended exactly as planned: nothing left to cover
_END_REASONS = ("session finished", "market day over")
#: A run that gave up because the FEED produced no data may re-arm only this
#: many times a day: a successor started against a dead yahoo burns ~25 minutes
#: (its own polls) before it gives up too, and an unbounded chain of those would
#: hold every runner while the market is open.
FAILURE_REARMS_PER_DAY = 3


def ci_context() -> dict | None:
    """Actions context needed to dispatch a successor run, or None outside CI."""
    token = os.environ.get("FPFSSL_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or "/" not in repo:
        return None
    return {
        "token": token,
        "repository": repo.strip(),
        "ref": os.environ.get("FPFSSL_REF") or os.environ.get("GITHUB_REF_NAME") or "main",
        "workflow": os.environ.get("FPFSSL_WORKFLOW") or "scanner.yml",
        "inputs": _env_inputs(),
        "run_id": os.environ.get("GITHUB_RUN_ID") or "",
    }


def _env_inputs() -> dict:
    """`workflow_dispatch` inputs for the successor (from FPFSSL_REARM_INPUTS)."""
    raw = os.environ.get("FPFSSL_REARM_INPUTS") or ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    inputs = dict(data) if isinstance(data, dict) else {}
    # a re-armed run is never a one-shot, and never a dry run
    inputs["once"] = False
    inputs["dry_run"] = False
    return {k: v for k, v in inputs.items()
            if k in ("dry_run", "once", "poll_session", "symbols", "interval")}


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def session_is_live(cfg: AppConfig, now: datetime | None = None) -> bool:
    """Is there still something to scan today?

    True from three hours before the bell (the pre-open wait is a real part of a
    session run) until `stop_after_close_minutes` past the close; after that the
    market day is over and a successor run would burn a runner for nothing.
    """
    from datetime import timedelta

    from .scanner import _session_bounds, market_now

    now = now or market_now(cfg)
    if now.weekday() >= 5:
        return False
    settle = abs(float(getattr(cfg.scanner, "stop_after_close_minutes", 15.0) or 15.0))
    open_dt, close_dt = _session_bounds(cfg, now)
    return open_dt - timedelta(hours=3) <= now <= close_dt + timedelta(minutes=settle)


def _chain_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _chain(state: dict, day: str) -> dict:
    chain = (state or {}).get("chain") or {}
    return dict(chain) if str(chain.get("date", "")) == day else {}


def chain_count(state: dict, day: str) -> int:
    """How many successor runs have already been dispatched for `day`."""
    return int(_chain(state, day).get("count", 0) or 0)


def failure_count(state: dict, day: str) -> int:
    """Successors dispatched because the feed produced no data (today)."""
    return int(_chain(state, day).get("fails", 0) or 0)


def bump_chain(state: dict, day: str, feed_failure: bool = False) -> int:
    chain = _chain(state, day)
    chain["date"] = day
    chain["count"] = int(chain.get("count", 0) or 0) + 1
    if feed_failure:
        chain["fails"] = int(chain.get("fails", 0) or 0) + 1
    chain["last_dispatch"] = datetime.now().isoformat(timespec="seconds")
    state["chain"] = chain
    return chain["count"]


def should_reschedule(reason: str, cfg: AppConfig, state: dict | None = None,
                      now: datetime | None = None,
                      feed_failure: bool = False) -> tuple[bool, str]:
    """`(yes, why)` — is a successor run warranted for this stop reason?"""
    from .scanner import market_now

    sc = cfg.scanner
    state = state or {}
    now = now or market_now(cfg)
    if not _as_bool(getattr(sc, "reschedule_in_ci", True)):
        return False, "scanner.reschedule_in_ci is false"
    if cfg.telegram.dry_run or not cfg.telegram.enabled:
        return False, "dry run (nothing was really delivered)"
    if ci_context() is None:
        return False, "not running in GitHub Actions (no GITHUB_TOKEN/GITHUB_REPOSITORY)"
    r = (reason or "").strip().lower()
    if any(u in r for u in _USER_STOPS):
        return False, f"stopped by request ({reason}) — a cancel must end the chain"
    if any(u in r for u in _END_REASONS):
        return False, f"the run ended on schedule ({reason})"
    if not session_is_live(cfg, now):
        return False, "market day over (nothing left to cover)"
    cap = int(getattr(sc, "reschedule_max_runs_per_day", 40) or 0)
    n = chain_count(state, _chain_bucket(now))
    if cap > 0 and n >= cap:
        return False, f"re-arm cap reached ({n}/{cap} runs today)"
    if feed_failure:
        f = failure_count(state, _chain_bucket(now))
        if f >= FAILURE_REARMS_PER_DAY:
            return False, (f"the feed is down and {f}/{FAILURE_REARMS_PER_DAY} "
                           "re-arms already failed on it today")
        why = f"feed produced no data; retrying once ({f}/{FAILURE_REARMS_PER_DAY})"
        return True, why
    return True, f"session still live after \"{reason or 'pass complete'}\" ({n}/{cap} re-arms used)"


def dispatch_next(ctx: dict | None = None, post=None) -> tuple[bool, str]:
    """POST workflow_dispatch for the successor. `(ok, detail)`; never raises."""
    ctx = ctx if ctx is not None else ci_context()
    if not ctx:
        return False, "no Actions context"
    url = f"{API}/repos/{ctx['repository']}/actions/workflows/{ctx['workflow']}/dispatches"
    body = {"ref": ctx["ref"], "inputs": ctx.get("inputs") or {}}
    headers = {
        "Authorization": f"Bearer {ctx['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fpfssl-scanner",
    }
    try:
        r = (post or requests.post)(url, json=body, headers=headers, timeout=20)
        status = getattr(r, "status_code", 204)
        ok = 200 <= status < 300
        detail = f"HTTP {status}" if not ok else f"queued {ctx['workflow']} on {ctx['ref']}"
        if not ok:
            detail += f" — {getattr(r, 'text', '')[:180]}"
        return ok, detail
    except Exception as e:  # noqa: BLE001 - a failed re-arm must never break a scan
        return False, f"{type(e).__name__}: {e}"


def maybe_reschedule(scanner, reason: str, now: datetime | None = None) -> tuple[bool, str]:
    """Decide + act. Returns `(dispatched, detail)`; safe to call anywhere."""
    from .scanner import market_now

    cfg = scanner.cfg
    state = getattr(scanner, "state", {}) or {}
    now = now or market_now(cfg)
    feed_failure = bool(getattr(scanner, "_pass_failed", False))
    ok, why = should_reschedule(reason, cfg, state, now, feed_failure=feed_failure)
    if not ok:
        log.info("re-arm: not rescheduling — %s", why)
        return False, why
    ctx = ci_context()
    sent, detail = dispatch_next(ctx)
    if sent:
        n = bump_chain(state, _chain_bucket(now), feed_failure=feed_failure)
        try:
            scanner._save_state()
        except Exception as e:  # noqa: BLE001
            log.warning("could not persist the re-arm counter: %s", e)
        log.info("re-armed the scanner for the rest of the session: %s (%d re-arm(s) today)",
                 detail, n)
        return True, f"{detail} ({n}/{int(getattr(cfg.scanner, 'reschedule_max_runs_per_day', 0) or 0)} today)"
    log.error("re-arm FAILED (%s) — the session is now covered only by GitHub's "
              "best-effort schedule. If the workflow token may not create "
              "workflow runs, set Settings → Actions → General → Workflow "
              "permissions to 'Read and write'.", detail)
    return False, f"dispatch failed: {detail}"


__all__ = ["API", "bump_chain", "chain_count", "ci_context", "dispatch_next",
           "maybe_reschedule", "session_is_live", "should_reschedule"]
