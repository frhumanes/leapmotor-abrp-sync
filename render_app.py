"""Render.com / Docker entrypoint.

Runs the ABRP sync loop in a background thread and serves a tiny HTTP
endpoint so Render's health check (and Docker's HEALTHCHECK) sees the
service as up. For a native install (systemd), run leapmotor_to_abrp.py
directly instead -- this web server is only needed by Render's free tier.
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from leapmotor_to_abrp import ensure_certs


def run_sync():
    ensure_certs()
    print("Starting ABRP sync loop (5-minute interval)...", flush=True)
    while True:
        # leapmotor_to_abrp.py runs its own resilient interval loop; if it
        # ever exits, supervise it by restarting after a short pause.
        subprocess.run(
            [sys.executable, "leapmotor_to_abrp.py", "--interval", "300"],
            env=os.environ.copy(),
        )
        print("Sync process exited unexpectedly; restarting in 30s...", flush=True)
        time.sleep(30)


class DummyHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_HEAD(self):
        self._ok()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Leapmotor ABRP Sync is active!")

    def log_message(self, *args):  # silence per-request access logs
        pass


if __name__ == "__main__":
    threading.Thread(target=run_sync, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    print(f"Starting keep-alive web server on port {port}...", flush=True)
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()
