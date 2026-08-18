# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Bridge that reads live telemetry from a Leapmotor vehicle (via the reverse-engineered
Leapmotor International app API) and pushes it to ABRP (A Better Routeplanner) every 5
minutes. Read-only by design: all remote-control capabilities were deliberately removed.

## Running locally

```bash
pip install -r requirements.txt

# One-shot push to ABRP (auto-detects VIN from the account)
LEAPMOTOR_USERNAME=... LEAPMOTOR_PASSWORD=... ABRP_TOKEN=... \
  python leapmotor_to_abrp.py --once

# Continuous loop (default 300s interval)
python leapmotor_to_abrp.py --interval 300
```

`leapmotor_to_abrp.py` shells out to `leapmotor_client.py` and needs the static app
certificate at `custom_components/leapmotor/app_cert.pem` and `.../app_key.pem`. These are
**not** in the repo (blocked by `.gitignore`); `ensure_certs()` in `leapmotor_to_abrp.py`
downloads them once from the `markoceri/leapmotor-certs` GitHub repo (pinned commit),
writes atomically via a `.part` file, and caches them on disk. `render_app.py` reuses the
same function. `leapmotor_client.py` additionally requires the `openssl` and `curl`
**binaries** at runtime (PKCS#12 extraction uses `openssl pkcs12 -legacy`; some requests
shell out to `curl`).

### Exercising the client directly

`leapmotor_client.py` is a multi-command CLI (`python leapmotor_client.py <command> ...`).
The commands the bridge actually uses:

```bash
python leapmotor_client.py \
  --cert-file custom_components/leapmotor/app_cert.pem \
  --key-file  custom_components/leapmotor/app_key.pem \
  direct-login-vehicle-list          # → JSON with bindcars / sharedcars

python leapmotor_client.py ... direct-login-vehicle-summary --vin <VIN>
```

Most other subcommands (`login`, `*-replay`, `*-generated`, `sign-sample`, `decode-jwt`)
are reverse-engineering / debugging helpers, not part of the production path.

### Container

```bash
docker compose up -d                    # uses ghcr.io/kerniger/leapmotor-abrp-sync:latest
docker build -t leapmotor-abrp-sync .
sh scripts/container_smoke_test.sh      # runtime checks (non-root, curl/openssl, p12 roundtrip)
```

CI (`.github/workflows/container.yml`) builds a multi-arch (amd64/arm64) image, pushes to
GHCR on `main`/tags, then runs the smoke test against the published image.

There is no test suite, linter, or formatter configured.

## Deployment model

Three supported targets:
- **Render.com free tier** — `render_app.py` runs the sync loop in a background thread and a
  dummy HTTP server on `$PORT` (default 10000) so Render's health check passes. UptimeRobot
  pings the URL to prevent the free instance from sleeping.
- **Self-hosted Docker** — same `render_app.py` entrypoint; the healthcheck curls the dummy
  server.
- **Native (systemd)** — `deploy/install.sh` sets up a venv under `/opt/leapmotor-abrp-sync`
  and installs `deploy/leapmotor-abrp.service`, which runs `leapmotor_to_abrp.py
  --interval 300` directly (no web server). This is the recommended target for an
  always-on small box (Raspberry Pi / DietPi); `leapmotor_to_abrp.py`'s own loop handles
  retries/backoff, so systemd only needs `Restart=always` as a backstop. Least privilege:
  code and venv stay root-owned; only the cert cache dir (`custom_components/leapmotor`) and
  `/etc/leapmotor-abrp.env` are owned by the `leapmotor` service user. Updates run via
  `deploy/update.sh` (idempotent pull + dep/unit sync + restart; auto-detects the checkout
  owner so it works whether the tree is root- or user-owned). `deploy/install.sh` also
  installs `leapmotor-abrp-update.{service,timer}` for opt-in daily auto-updates (left
  disabled).

## Architecture notes

The value in this repo is the Leapmotor API client (`leapmotor_client.py`, ~2700 lines).
The auth flow is intricate and undocumented upstream:

1. **Static app cert login** — the app ships a shared client TLS cert (`app_cert.pem`/
   `app_key.pem`) used as an mTLS client cert for the login call. TLS server verification is
   disabled (`verify_tls=False`) because the gateway presents an untrusted chain.
2. **Signed headers** — every request carries an HMAC-SHA256 `sign` header over a specific
   concatenation of `acceptLanguage`, `channel`, `deviceId`, `deviceType`, `nonce`, `source`,
   `timestamp`, `version` (+ optional VIN). See `LeapmotorClient.build_sign_input`. The sign
   key is either a hardcoded default (`DEFAULT_SIGN_KEY_HEX`) or, post-login, HKDF-derived
   from `signIkm`/`signSalt`/`signInfo` returned by the login response.
3. **Per-account cert** — login returns an encrypted PKCS#12 blob. Its password is derived
   from account fields (`account_id`, `uid`) via a bespoke MD5 + SHA-256 + **SM4** scheme
   implemented from scratch in `custom_components/leapmotor/p12.py`
   (`derive_account_p12_password`). `KNOWN_ACCOUNT_P12_PASSWORDS` are fallback candidates.
4. **Account-scoped client** — a second `LeapmotorClient` is built with the extracted
   per-account cert/key, the derived sign key, and a `deviceId` derived from the JWT
   (`derive_session_device_id`). This client makes the vehicle list/status/summary calls.

`custom_components/leapmotor/p12.py` is loaded dynamically by `leapmotor_client.py` via
`importlib` (not a normal import) — the `custom_components/` path mirrors a Home Assistant
layout but nothing here runs inside HA.

Telemetry mapping (`build_payload` in `leapmotor_to_abrp.py`): the client's
`direct-login-vehicle-summary` returns a normalized `summary` block plus raw `requests`.
The ABRP payload strips `None` fields before POSTing to
`https://api.iternio.com/1/tlm/send`, so unknown values are simply not sent (ABRP keeps its
last/estimated value). `ext_temp` is deliberately **not** sent: Leapmotor only exposes
interior (cabin) temperature, and feeding that as outside temp corrupts ABRP's range model.

Charging is **inferred from the SoC trend across cycles** (rising SoC = charging), not from
a raw signal: the old signal `1939` guess reads `1` while parked and not charging on at
least some vehicles, so it is unused. `fetch_and_push` threads the previous SoC through the
loop and sets `is_charging`; because that needs a baseline, `--once` never reports charging.
`--dump` prints the raw signal block (GPS signals `2`/`3`/`3724`/`3725`/`2190`/`2191`
redacted) to help identify the real charging signal.

`classify_state()` turns `is_parked`/`is_charging` into driving/charging/parked/unknown,
which selects an adaptive push interval (`--drive-interval` 180s / `--charge-interval` 300s /
`--interval` 900s for parked & unknown), floored at 30s. The state is derived from the
telemetry already fetched each cycle, so throttling costs no extra API calls. The systemd
unit passes no interval flags, so these defaults apply unless overridden in `ExecStart`.

## Conventions & constraints

- Never re-introduce remote-control (unlock, climate, etc.) functionality — read-only is a
  stated security guarantee of the project.
- Code comments and user-facing CLI errors are a mix of German and English; match the
  surrounding language of the file/function you're editing.
- `.gitignore` blocks credentials and all cert/key material (`*.pem`, `*.p12`, `LOCAL_ACCESS.md`,
  `findings/`). Never commit any of these. Credentials come from env vars
  (`LEAPMOTOR_USERNAME`, `LEAPMOTOR_PASSWORD`, `ABRP_TOKEN`), CLI flags, or a local
  `LOCAL_ACCESS.md`.
- `abrp_api_key` in `leapmotor_to_abrp.py` is the project's registered ABRP developer key
  (identifies the integration), distinct from the per-user `ABRP_TOKEN`.
