"""
Shared helpers for the GTM dashboard refresh pullers.

Standard library ONLY (urllib, json, os, datetime, time, argparse). No pip, no
third-party packages, so there is nothing to install in CI and nothing to break.

Every puller:
  * reads its API key from an environment variable (never hardcoded),
  * retries a failed request up to MAX_RETRIES with short backoff,
  * honours HTTP 429 Retry-After,
  * writes a per-leg temp JSON file with status ok|error + last_success,
  * on failure writes status "error" and exits 0 so the pipeline continues,
  * supports a --check mode that fails loudly (exit 1) if the source is empty,
  * never prints a secret to the logs.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# --- config ---------------------------------------------------------------

MAX_RETRIES = 3
BACKOFF_SECONDS = [2, 4, 8]        # short backoff between retries
DEFAULT_TIMEOUT = 30               # seconds per HTTP request

# AEST (Australia/Sydney standard offset, +10:00). The existing data.json and
# history.jsonl timestamps are written with a +10:00 offset, so we match that
# exactly rather than introducing a new timezone convention.
AEST = timezone(timedelta(hours=10))


def now_aest_iso():
    """ISO-8601 timestamp with the +10:00 offset used throughout the repo."""
    return datetime.now(AEST).replace(microsecond=0).isoformat()


def temp_dir():
    """Directory the pullers write their leg files into.

    The workflow sets REFRESH_TMP. Falls back to a local folder for manual runs.
    """
    d = os.environ.get("REFRESH_TMP") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_refresh_tmp"
    )
    os.makedirs(d, exist_ok=True)
    return d


def leg_path(leg):
    return os.path.join(temp_dir(), "leg_{}.json".format(leg))


def _redact(text):
    """Best-effort scrub of any known secret value out of a string before it
    could ever reach the logs."""
    if not text:
        return text
    for var in (
        "HUBSPOT_TOKEN",
        "REPLYIO_API_KEY",
        "WINDSOR_API_KEY",
        "CONNECTSAFELY_API_KEY",
    ):
        val = os.environ.get(var)
        if val and val in text:
            text = text.replace(val, "***REDACTED***")
    return text


def log(msg):
    """Print a log line, scrubbing any secret first."""
    print(_redact(str(msg)), flush=True)


class HttpError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__("HTTP {}".format(status))


def _do_request(url, method="GET", headers=None, body=None, timeout=DEFAULT_TIMEOUT):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            pass
        # surface Retry-After for 429 handling
        retry_after = e.headers.get("Retry-After") if e.headers else None
        err = HttpError(e.code, raw)
        err.retry_after = retry_after
        raise err


def request_json(url, method="GET", headers=None, body=None, timeout=DEFAULT_TIMEOUT):
    """HTTP request returning parsed JSON, with retry + backoff + 429 handling.

    Retries up to MAX_RETRIES on transient failures (5xx, timeouts, connection
    errors) and on 429, honouring Retry-After. Raises on final failure. Never
    logs the URL query string or headers (they may carry a key).
    """
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            status, raw = _do_request(url, method, headers, body, timeout)
            return json.loads(raw) if raw else {}
        except HttpError as e:
            last_exc = e
            # 429: honour Retry-After, else standard backoff
            if e.status == 429 and attempt < MAX_RETRIES:
                wait = _parse_retry_after(getattr(e, "retry_after", None), attempt)
                log("  rate limited (429), waiting {}s".format(wait))
                time.sleep(wait)
                continue
            # 5xx: retry with backoff
            if 500 <= e.status < 600 and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                continue
            # 4xx (other than 429): not retryable
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                continue
            raise
    if last_exc:
        raise last_exc


def _parse_retry_after(value, attempt):
    try:
        if value is not None:
            return max(1, int(float(value)))
    except (ValueError, TypeError):
        pass
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def require_env(var):
    """Return the secret from the environment, or raise a clear (secret-free)
    error if it is missing."""
    val = os.environ.get(var)
    if not val:
        raise RuntimeError(
            "Missing required environment variable {}. Set it as a GitHub "
            "repository secret.".format(var)
        )
    return val


def write_leg(leg, status, data, error=None, last_success=None):
    """Write the per-leg temp file the assembler reads."""
    payload = {
        "leg": leg,
        "status": status,                       # "ok" | "error"
        "last_success": last_success,           # iso string on ok, else None
        "error": _redact(error) if error else None,
        "data": data if data is not None else {},
    }
    path = leg_path(leg)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def run_puller(leg, fetch_fn, check_fn):
    """Standard puller lifecycle.

    fetch_fn()  -> returns the leg's data dict (may raise on failure)
    check_fn(d) -> returns True if the data looks non-empty / healthy

    --check mode: run the fetch, and exit 1 (loud failure) if check_fn says the
    source came back empty. Normal mode: on any error, write status "error" and
    exit 0 so the overall run continues.
    """
    check_mode = "--check" in sys.argv
    started = now_aest_iso()
    log("[{}] starting puller (check_mode={})".format(leg, check_mode))
    try:
        data = fetch_fn()
        ok = bool(check_fn(data))
        if not ok:
            msg = "source returned empty / no usable data"
            if check_mode:
                log("[{}] CHECK FAILED: {}".format(leg, msg))
                write_leg(leg, "error", data, error=msg, last_success=None)
                sys.exit(1)
            log("[{}] WARNING: {} -> marking leg error".format(leg, msg))
            write_leg(leg, "error", data, error=msg, last_success=None)
            log("[{}] status=error".format(leg))
            return
        write_leg(leg, "ok", data, last_success=now_aest_iso())
        log("[{}] status=ok".format(leg))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - we intentionally never crash the run
        err = "{}: {}".format(type(e).__name__, e)
        log("[{}] ERROR {}".format(leg, err))
        write_leg(leg, "error", None, error=err, last_success=None)
        if check_mode:
            sys.exit(1)
        # Non-check mode: exit 0 so the pipeline continues with carry-forward.
        log("[{}] status=error (continuing)".format(leg))
