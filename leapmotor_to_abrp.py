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


# Battery current (signal 1178) below this while parked means energy is
# flowing into the pack, i.e. charging. Observed: ~+0.1..0.4 A parked idle,
# +2.5 A driving, -9.6 A on a ~4 kW home AC charger. The margin keeps idle
# noise from flapping the state.
CHARGE_CURRENT_THRESHOLD_A = -1.0


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def snapshot_time(signal: dict) -> int | None:
    """Return when the car produced this snapshot, in epoch seconds.

    The API serves the cloud's last stored snapshot, not live data: once the
    car sleeps it stops reporting and every poll returns the same block.
    Signal "1" (and "sts" a moment later) is that snapshot's timestamp in ms.
    """
    for key in ("1", "sts"):
        ts = _to_float(signal.get(key))
        if ts:
            return int(ts // 1000)
    return None


def build_payload(data: dict) -> dict:
    """Map a vehicle summary response to an ABRP telemetry payload.

    `is_charging` comes from the battery current (signal 1178, negative when
    charging) and is only asserted while parked, since regen braking also
    drives the current negative. The raw signal 1939 that older code used is
    unreliable (reads 1 while parked and not charging). If the current is
    missing, `is_charging` is left out and fetch_and_push() falls back to the
    SoC trend across cycles.
    """
    summary = data.get("summary", {})
    status = summary.get("status", {})
    location = summary.get("location", {})
    signal = extract_signal(data)

    is_parked = status.get("is_parked")
    # Signal 100003 is the SoC with one decimal (1204 / battery_percent is
    # the same value rounded); prefer it for a finer ABRP charge curve.
    soc = _to_float(signal.get("100003"))
    if soc is None:
        soc = status.get("battery_percent")
    voltage = _to_float(signal.get("1177"))
    current = _to_float(signal.get("1178"))
    power = (
        round(voltage * current / 1000, 2)
        if voltage is not None and current is not None
        else None
    )
    is_charging = None
    if current is not None and is_parked is not None:
        is_charging = bool(is_parked) and current < CHARGE_CURRENT_THRESHOLD_A

    payload = {
        # Report when the car produced the data, so a stale snapshot is not
        # presented to ABRP as current. Fall back to now if it is missing.
        "utc": snapshot_time(signal) or int(time.time()),
        "soc": soc,
        "is_parked": is_parked,
        "is_charging": is_charging,
        # ABRP convention: positive = power out of the battery, negative =
        # charging, which matches the sign of signal 1178.
        "power": power,
        "voltage": voltage,
        "current": current,
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


def describe_location(raw_signal: dict, payload: dict) -> dict:
    """Describe the shape of the location signals without revealing them.

    Reports type, sign, integer digits and decimals of each GPS signal, and
    which ones share the same magnitude, so a wrong mapping (swapped axes,
    lost sign, scaling) can be diagnosed without exposing coordinates.
    """

    def shape(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return {"type": type(value).__name__, "numeric": False}
        text = str(value)
        decimals = len(text.split(".", 1)[1]) if "." in text else 0
        return {
            "type": type(value).__name__,
            "sign": "-" if number < 0 else ("0" if number == 0 else "+"),
            "int_digits": len(str(int(abs(number)))),
            "decimals": decimals,
        }

    def magnitude(key):
        try:
            return round(abs(float(raw_signal[key])), 4)
        except (KeyError, TypeError, ValueError):
            return None

    keys = sorted(LOCATION_SIGNALS, key=int)
    same_magnitude = [
        f"{a}~{b}"
        for i, a in enumerate(keys)
        for b in keys[i + 1:]
        if magnitude(a) is not None and magnitude(a) == magnitude(b)
    ]
    return {
        "signals": {k: shape(raw_signal.get(k)) for k in keys},
        "same_magnitude": same_magnitude,
        "sent_lat": shape(payload.get("lat")),
        "sent_lon": shape(payload.get("lon")),
    }


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
                "snapshot_utc (signal 1)": datetime.fromtimestamp(
                    payload["utc"], timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%SZ"),
                "is_parked (from signal 1298)": payload.get("is_parked"),
                "is_charging (parked and 1178 < "
                f"{CHARGE_CURRENT_THRESHOLD_A} A)": payload.get("is_charging"),
                "power_kw (1177 x 1178)": payload.get("power"),
                "battery_percent (1204)": raw_signal.get("1204"),
                "location_shape (no coordinates)": describe_location(
                    raw_signal, payload
                ),
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
    vin: str, abrp_token: str, prev_soc: float | None, prev_utc: int | None
) -> tuple[bool, str, float | None, int | None]:
    """Fetch one telemetry sample and push it to ABRP.

    Charging comes from the battery current (see build_payload). Only if that
    signal is missing do we fall back to the SoC trend: once we have a previous
    reading, a rising SoC means charging. A snapshot identical to the previous
    one (car asleep) is not pushed again. Returns (success, state, latest_soc,
    latest_utc) so the caller can throttle and keep the trend.
    """
    data = run_client("direct-login-vehicle-summary", "--vin", vin)
    if not data:
        return False, "unknown", prev_soc, prev_utc

    payload = build_payload(data)
    soc = payload.get("soc")
    signal = extract_signal(data)
    utc = payload["utc"] if snapshot_time(signal) else None

    if utc is not None and utc == prev_utc:
        state = classify_state(payload)
        since = datetime.fromtimestamp(utc, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        log(f"no new data since {since} (car asleep?); not pushing [{state}]")
        return True, state, prev_soc, prev_utc

    # Fallback: only assert a charging state from the SoC trend once we have a
    # baseline to compare against; otherwise leave it unknown (omitted) so
    # ABRP keeps its last value.
    if "is_charging" not in payload and soc is not None and prev_soc is not None:
        payload["is_charging"] = soc > prev_soc

    state = classify_state(payload)
    latest_soc = soc if soc is not None else prev_soc

    if soc is None:
        log("no SoC in response; skipping push this cycle")
        return False, state, latest_soc, prev_utc

    try:
        resp = requests.post(
            ABRP_URL,
            headers={"Authorization": f"APIKEY {ABRP_API_KEY}"},
            params={"token": abrp_token, "tlm": json.dumps(payload)},
            timeout=15,
        )
    except requests.RequestException as exc:
        log(f"ABRP request failed: {exc}")
        return False, state, latest_soc, prev_utc

    if resp.status_code == 200:
        charging = ""
        if payload.get("is_charging"):
            # Signal 1200 is only present while charging and counts down:
            # minutes until full (not an ABRP field, so log only).
            details = []
            if "power" in payload:
                details.append(f"{-payload['power']:.1f} kW")
            minutes_to_full = _to_float(signal.get("1200"))
            if minutes_to_full is not None:
                details.append(f"~{int(minutes_to_full)} min to full")
            charging = f" (charging{': ' + ', '.join(details) if details else ''})"
        log(f"pushed to ABRP: SoC {soc}% [{state}]{charging}")
        return True, state, latest_soc, utc
    log(f"ABRP rejected push: {resp.status_code} {resp.text.strip()}")
    return False, state, latest_soc, prev_utc


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

    prev_soc: float | None = None
    prev_utc: int | None = None
    failures = 0
    while True:
        ok, state, prev_soc, prev_utc = fetch_and_push(
            vin, abrp_token, prev_soc, prev_utc
        )
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
