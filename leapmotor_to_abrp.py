#!/usr/bin/env python3
"""Bridge Leapmotor vehicle telemetry to ABRP (A Better Routeplanner).

Reads live vehicle data through the reverse-engineered Leapmotor API
(via leapmotor_client.py) and pushes it to ABRP. Read-only: no remote
control of the vehicle is performed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

ABRP_URL = "https://api.iternio.com/1/tlm/send"
# Registered ABRP developer API key for this integration. It identifies the
# app to ABRP; the per-user token (ABRP_TOKEN) identifies the account.
ABRP_API_KEY = "7310445a-0947-4adc-82f5-29bb882c5926"

BASE_DIR = Path(__file__).resolve().parent
CERT_DIR = BASE_DIR / "custom_components" / "leapmotor"
CERT_FILE = CERT_DIR / "app_cert.pem"
KEY_FILE = CERT_DIR / "app_key.pem"
CLIENT = str(BASE_DIR / "leapmotor_client.py")

# Pinned upstream app certificates. This is the shared, non-secret client
# certificate the Leapmotor app ships; downloaded once and cached on disk.
CERT_URLS = {
    CERT_FILE: (
        "https://raw.githubusercontent.com/markoceri/leapmotor-certs/"
        "5d687448c11dd2253669d1c55710e688313d1e2b/app.crt"
    ),
    KEY_FILE: (
        "https://raw.githubusercontent.com/markoceri/leapmotor-certs/"
        "5d687448c11dd2253669d1c55710e688313d1e2b/app.key"
    ),
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def ensure_certs(retries: int = 5, base_delay: int = 5) -> None:
    """Download the shared app certificates once if they are missing.

    Writes to a temporary file and renames on success so an interrupted
    download never leaves a corrupt certificate behind.
    """
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    for path, url in CERT_URLS.items():
        if path.exists():
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        for attempt in range(1, retries + 1):
            try:
                log(f"Downloading {path.name} (attempt {attempt}/{retries})...")
                urllib.request.urlretrieve(url, tmp)
                tmp.replace(path)
                break
            except Exception as exc:  # noqa: BLE001 - report and retry any failure
                tmp.unlink(missing_ok=True)
                if attempt == retries:
                    raise
                delay = min(base_delay * attempt, 60)
                log(f"Download failed ({exc}); retrying in {delay}s")
                time.sleep(delay)


def run_client(*client_args: str) -> dict | None:
    """Run leapmotor_client.py with the static app cert; return parsed JSON."""
    cmd = [
        sys.executable,
        CLIENT,
        "--cert-file",
        str(CERT_FILE),
        "--key-file",
        str(KEY_FILE),
        *client_args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        log(f"client error ({client_args[0]}): {detail}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log(f"could not parse client output ({client_args[0]}): {exc}")
        return None


def detect_vin() -> str | None:
    """Return the first VIN bound to or shared with the account."""
    data = run_client("direct-login-vehicle-list")
    if not data:
        return None
    try:
        body = json.loads(data.get("vehicle_list", {}).get("body", "{}"))
        cars = body.get("data", {})
        for bucket in ("bindcars", "sharedcars"):
            for car in cars.get(bucket, []):
                if car.get("vin"):
                    return car["vin"]
    except (json.JSONDecodeError, AttributeError) as exc:
        log(f"could not parse vehicle list: {exc}")
    return None


def build_payload(data: dict) -> dict:
    """Map a vehicle summary response to an ABRP telemetry payload.

    `is_charging` is intentionally NOT derived here: the raw signal 1939 that
    older code used is unreliable (reads 1 while parked and not charging on at
    least some vehicles). The caller sets `is_charging` from the SoC trend
    across cycles, which is vehicle-agnostic. See fetch_and_push().
    """
    summary = data.get("summary", {})
    status = summary.get("status", {})
    location = summary.get("location", {})

    payload = {
        "utc": int(time.time()),
        "soc": status.get("battery_percent"),
        "is_parked": status.get("is_parked"),
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
        "odometer": status.get("odometer_km"),
        # ext_temp is intentionally NOT sent: Leapmotor only exposes interior
        # (cabin) temperature, and ABRP uses ext_temp for consumption/range
        # modelling. Feeding cabin temp as outside temp corrupts predictions,
        # so omitting it (ABRP falls back to weather data) is more accurate.
    }
    # Drop unknown fields so ABRP keeps its previous/estimated values.
    return {k: v for k, v in payload.items() if v is not None}


# Signals carrying location; redacted from diagnostic dumps so they can be
# shared safely. "2"/"3" are the raw lon/lat and must be redacted too.
LOCATION_SIGNALS = {"2", "3", "3724", "3725", "2190", "2191"}


def extract_signal(data: dict) -> dict:
    """Return the raw signal dict from a vehicle summary response."""
    try:
        status_body = (
            data.get("requests", {}).get("vehicle_status", {}).get("body", "{}")
        )
        return json.loads(status_body).get("data", {}).get("signal", {}) or {}
    except (json.JSONDecodeError, AttributeError):
        return {}


def dump_signals(vin: str) -> int:
    """Print the raw signals (GPS redacted) and the derived state, for debugging."""
    data = run_client("direct-login-vehicle-summary", "--vin", vin)
    if not data:
        log("no data from client; cannot dump")
        return 1
    payload = build_payload(data)
    raw_signal = extract_signal(data)
    redacted = {
        k: ("<redacted>" if k in LOCATION_SIGNALS else v)
        for k, v in sorted(raw_signal.items())
    }
    print(
        json.dumps(
            {
                "derived_state (snapshot)": classify_state(payload),
                "note": (
                    "charging is inferred from the SoC trend across cycles, "
                    "not from a single snapshot; signal 1939 is unreliable"
                ),
                "is_parked (from signal 1298)": payload.get("is_parked"),
                "signal_1939_raw": raw_signal.get("1939"),
                "battery_percent (1204)": raw_signal.get("1204"),
                "all_signals_gps_redacted": redacted,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def classify_state(payload: dict) -> str:
    """Derive the vehicle state used for adaptive throttling.

    Returns one of: 'driving', 'charging', 'parked', 'unknown'. Works off the
    already-filtered payload, where a missing key means the value was unknown.
    """
    if payload.get("is_charging"):
        return "charging"
    is_parked = payload.get("is_parked")
    if is_parked is False:
        return "driving"
    if is_parked is True:
        return "parked"
    return "unknown"


def fetch_and_push(
    vin: str, abrp_token: str, prev_soc: int | None
) -> tuple[bool, str, int | None]:
    """Fetch one telemetry sample and push it to ABRP.

    Charging is inferred from the SoC trend: once we have a previous reading,
    a rising SoC means charging, a flat/falling SoC means not charging. Returns
    (success, state, latest_soc) so the caller can throttle and keep the trend.
    """
    data = run_client("direct-login-vehicle-summary", "--vin", vin)
    if not data:
        return False, "unknown", prev_soc

    payload = build_payload(data)
    soc = payload.get("soc")

    # Only assert a charging state once we have a baseline to compare against;
    # otherwise leave it unknown (omitted) so ABRP keeps its last value.
    if soc is not None and prev_soc is not None:
        payload["is_charging"] = soc > prev_soc

    state = classify_state(payload)
    latest_soc = soc if soc is not None else prev_soc

    if soc is None:
        log("no SoC in response; skipping push this cycle")
        return False, state, latest_soc

    try:
        resp = requests.post(
            ABRP_URL,
            headers={"Authorization": f"APIKEY {ABRP_API_KEY}"},
            params={"token": abrp_token, "tlm": json.dumps(payload)},
            timeout=15,
        )
    except requests.RequestException as exc:
        log(f"ABRP request failed: {exc}")
        return False, state, latest_soc

    if resp.status_code == 200:
        charging = " (charging)" if payload.get("is_charging") else ""
        log(f"pushed to ABRP: SoC {soc}% [{state}]{charging}")
        return True, state, latest_soc
    log(f"ABRP rejected push: {resp.status_code} {resp.text.strip()}")
    return False, state, latest_soc


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Leapmotor telemetry to ABRP")
    parser.add_argument("--vin", help="Vehicle VIN (auto-detected if omitted)")
    parser.add_argument("--abrp-token", help="ABRP token (or ABRP_TOKEN env var)")
    parser.add_argument("--username", help="Leapmotor username (or LEAPMOTOR_USERNAME)")
    parser.add_argument("--password", help="Leapmotor password (or LEAPMOTOR_PASSWORD)")
    parser.add_argument(
        "--interval",
        type=int,
        default=900,
        help="Seconds between pushes while parked / when state is unknown (default 900)",
    )
    parser.add_argument(
        "--drive-interval",
        type=int,
        default=180,
        help="Seconds between pushes while the car is moving (default 180)",
    )
    parser.add_argument(
        "--charge-interval",
        type=int,
        default=300,
        help="Seconds between pushes while charging (default 300)",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single cycle and exit (cron/testing)"
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print raw signals (GPS redacted) and the derived state, then exit",
    )
    args = parser.parse_args()

    # Never poll faster than 30s: each cycle is a full login against an
    # unofficial API, so keep a floor even if the user asks for less.
    intervals = {
        "driving": max(30, args.drive_interval),
        "charging": max(30, args.charge_interval),
        "parked": max(30, args.interval),
        "unknown": max(30, args.interval),
    }

    # leapmotor_client.py reads credentials from the environment; export any
    # values passed on the CLI so the subprocess sees them.
    if args.username:
        os.environ["LEAPMOTOR_USERNAME"] = args.username
    if args.password:
        os.environ["LEAPMOTOR_PASSWORD"] = args.password
    abrp_token = args.abrp_token or os.environ.get("ABRP_TOKEN")

    if not os.environ.get("LEAPMOTOR_USERNAME") or not os.environ.get("LEAPMOTOR_PASSWORD"):
        parser.error("Leapmotor credentials required (--username/--password or env vars)")
    if not abrp_token and not args.dump:
        parser.error("--abrp-token or ABRP_TOKEN env var is required")

    ensure_certs()

    vin = args.vin
    if not vin:
        log("auto-detecting VIN from account...")
        for attempt in range(1, 6):
            vin = detect_vin()
            if vin:
                break
            if args.once:
                break
            delay = min(15 * attempt, 60)
            log(f"VIN detection failed; retry {attempt}/5 in {delay}s")
            time.sleep(delay)
        if not vin:
            log("no vehicle found in account; giving up")
            return 1
    if args.dump:
        return dump_signals(vin)

    log(
        f"using VIN ***{vin[-4:]} (adaptive: drive {intervals['driving']}s / "
        f"charge {intervals['charging']}s / parked {intervals['parked']}s)"
    )

    prev_soc: int | None = None
    failures = 0
    while True:
        ok, state, prev_soc = fetch_and_push(vin, abrp_token, prev_soc)
        if args.once:
            return 0 if ok else 1
        if ok:
            failures = 0
            delay = intervals[state]
            time.sleep(delay)
        else:
            failures += 1
            # Retry sooner than the normal interval on transient failures,
            # backing off up to the parked interval on repeated errors.
            delay = min(30 * failures, intervals["parked"])
            log(f"cycle failed ({failures} in a row); next attempt in {delay}s")
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
