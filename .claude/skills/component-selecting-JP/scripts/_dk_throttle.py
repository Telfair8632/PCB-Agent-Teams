"""Cross-process API throttle + 429/503 retry for DigiKey & Mouser.

Verified rate limits (May 2026, sources in references/api_rate_limits.md):
  - DigiKey API v4 Product Information: 120/min, 1000/day
    Errors: 429 + body 'BurstLimit exceeded' (per-minute)
            429 + body 'Daily Ratelimit exceeded' (per-day, no point retrying)
  - Mouser Search API:                    30/min, 1000/day
    Errors: 429 / 503 + Retry-After header

Why this module exists:
  1. Process-local locks were not enough — sub-agents run as separate
     Python processes that all hit the same client_id quota.
  2. Without daily-quota detection, exponential backoff would waste
     time and quota retrying a request that cannot recover today.

Public API:
  throttled_urlopen(req, timeout=15) -> urllib response
    Honors per-host minimum spacing across all processes on this machine
    via fcntl lock on /tmp/api_throttle.json. Detects daily-quota body
    and raises immediately (no retry). Detects burst-limit body and
    honors Retry-After. Captures the real urlopen at import to avoid
    recursion when callers monkey-patch urllib.request.urlopen.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

except ImportError:  # Windows: msvcrt-based byte-range lock (same semantics, best effort)
    import msvcrt

    def _lock(f) -> None:
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(f) -> None:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────
# Real urlopen captured at import. Required because callers monkey-patch
# urllib.request.urlopen globally — without this snapshot, throttled_urlopen
# would recurse into the patched symbol on every DK request.
# ─────────────────────────────────────────────────────────────────────────
_REAL_URLOPEN = urllib.request.urlopen


# Per-host minimum spacing in seconds (with 5-15% safety headroom on official).
# DK official: 120/min ⇒ 500ms; we use 600ms.
# Mouser official: 30/min ⇒ 2000ms; we use 2200ms.
# jlcsearch / wmsc.lcsc.com: no published quota; defensive 1.0s ≈ 60/min.
_HOST_MIN_INTERVAL: dict[str, float] = {
    "api.digikey.com": 0.6,
    "api.mouser.com": 2.2,
    "jlcsearch.tscircuit.com": 1.0,
    "wmsc.lcsc.com": 1.0,
    "api.frankfurter.app": 1.0,
}
_DEFAULT_INTERVAL = 0.6  # fallback for unknown hosts

# Retry schedule for transient burst limit / 503. Total max wait ~21s.
RETRY_DELAYS_S = (3.0, 6.0, 12.0)

# Cross-process state file. flock guarantees only one writer at a time.
# Survives across sub-agents started by the same user on the same machine.
_STATE_FILE = os.path.join(tempfile.gettempdir(), "api_throttle.json")

# In-process locks (cheap path before fcntl).
_THREAD_LOCK = threading.Lock()


class DailyQuotaExhausted(RuntimeError):
    """Raised when DK responds 'Daily Ratelimit exceeded'. Do not retry."""


def _host_of(req: urllib.request.Request) -> str:
    try:
        full = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    except Exception:
        return ""
    # Fast extract of host without full urlparse.
    if "://" in full:
        rest = full.split("://", 1)[1]
        return rest.split("/", 1)[0].split(":", 1)[0]
    return ""


def _interval_for(host: str) -> float:
    return _HOST_MIN_INTERVAL.get(host, _DEFAULT_INTERVAL)


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def _check_dead_until(state: dict, host: str) -> float:
    """Returns 0 if host is alive, else epoch time when host comes back."""
    return float(state.get("dead_until", {}).get(host, 0.0))


def _mark_dead_until(host: str, until_epoch: float, reason: str) -> None:
    """Record that this host won't accept calls until `until_epoch`. All
    subsequent throttled_urlopen calls to this host fail-fast with
    DailyQuotaExhausted until the time passes."""
    with _THREAD_LOCK:
        with open(_STATE_FILE, "a+") as f:
            _lock(f)
            try:
                f.seek(0)
                try:
                    state = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    state = {}
                dead = state.setdefault("dead_until", {})
                # Only extend, never shorten
                if until_epoch > float(dead.get(host, 0.0)):
                    dead[host] = until_epoch
                    state.setdefault("dead_reason", {})[host] = reason
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state))
            finally:
                _unlock(f)


def _wait_for_host_slot(host: str) -> None:
    """Cross-process spacing. Read last-call timestamp under flock, sleep
    until min_interval has elapsed, then write our new timestamp before
    releasing the lock."""
    interval = _interval_for(host)
    with _THREAD_LOCK:
        with open(_STATE_FILE, "a+") as f:
            _lock(f)
            try:
                f.seek(0)
                try:
                    state = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    state = {}

                # Fail-fast on daily quota.
                dead_until = _check_dead_until(state, host)
                now = time.time()
                if dead_until and now < dead_until:
                    reason = state.get("dead_reason", {}).get(host, "rate-limited")
                    wait_minutes = (dead_until - now) / 60
                    raise DailyQuotaExhausted(
                        f"{host} flagged dead until "
                        f"{time.strftime('%H:%M:%S', time.localtime(dead_until))} "
                        f"(~{wait_minutes:.0f} min from now): {reason}"
                    )

                last_calls = state.setdefault("last_call", {})
                last = float(last_calls.get(host, 0.0))
                gap = now - last
                if gap < interval:
                    time.sleep(interval - gap)
                last_calls[host] = time.time()

                f.seek(0)
                f.truncate()
                f.write(json.dumps(state))
            finally:
                _unlock(f)


def _classify_429(exc: urllib.error.HTTPError) -> tuple[str, float]:
    """Return ('daily', wait_s) | ('burst', wait_s) | ('unknown', 0)."""
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    retry_after = 0.0
    try:
        retry_after = float(exc.headers.get("Retry-After", "0") or 0)
    except (TypeError, ValueError):
        retry_after = 0.0

    body_lower = body.lower()
    if "daily ratelimit" in body_lower or "daily rate limit" in body_lower:
        # Honor Retry-After if present, otherwise default to 1 hour ahead.
        return ("daily", retry_after if retry_after > 0 else 3600.0)
    if "burstlimit" in body_lower or "burst limit" in body_lower:
        return ("burst", retry_after if retry_after > 0 else 60.0)
    return ("unknown", retry_after)


def throttled_urlopen(req: urllib.request.Request, timeout: float = 15.0):
    """urlopen with cross-process per-host throttle + smart 429/503 retry.

    - 429 'Daily Ratelimit exceeded' → mark host dead till retry-after,
      raise DailyQuotaExhausted immediately. No retry.
    - 429 'BurstLimit exceeded' or generic 429 → sleep Retry-After, retry up
      to len(RETRY_DELAYS_S) times.
    - 503 → exponential backoff via RETRY_DELAYS_S.
    - Other HTTPError / URLError → exponential backoff once, then raise.
    """
    host = _host_of(req)
    attempts = len(RETRY_DELAYS_S) + 1
    last_exc: Exception | None = None

    for i in range(attempts):
        _wait_for_host_slot(host)  # may raise DailyQuotaExhausted
        try:
            return _REAL_URLOPEN(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429:
                kind, wait_s = _classify_429(e)
                if kind == "daily":
                    _mark_dead_until(
                        host,
                        time.time() + wait_s,
                        f"daily_ratelimit_exceeded:retry_after={wait_s:.0f}s",
                    )
                    raise DailyQuotaExhausted(
                        f"{host}: Daily Ratelimit exceeded (Retry-After={wait_s:.0f}s). "
                        f"Subsequent calls will fail-fast until reset."
                    ) from e
                if i < attempts - 1:
                    sleep_s = max(wait_s, RETRY_DELAYS_S[i])
                    time.sleep(sleep_s)
                    continue
                raise
            if e.code == 503 and i < attempts - 1:
                time.sleep(RETRY_DELAYS_S[i])
                continue
            raise
        except urllib.error.URLError as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(RETRY_DELAYS_S[i])
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("throttled_urlopen: exhausted retries with no exception")


def reset_state() -> None:
    """Test helper / manual override: clear the shared state file."""
    try:
        os.unlink(_STATE_FILE)
    except FileNotFoundError:
        pass
