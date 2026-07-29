"""
Cloud Run HTTP entrypoint.

GET  /health  -> liveness
POST /sync    -> ABD Erstellung sync calistir (Cloud Scheduler burayi cagirir)
GET  /sync    -> ayni (manuel test icin)

Koruma: SYNC_API_KEY env varsa Header `X-API-Key` veya `?key=` zorunlu.
"""

from __future__ import annotations

import logging
import os
import traceback
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("abd-sync")

app = Flask(__name__)


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.getenv("SYNC_API_KEY", "").strip().strip("'").strip('"')
        if expected:
            provided = (
                request.headers.get("X-API-Key")
                or request.args.get("key")
                or ""
            ).strip()
            if provided != expected:
                return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.get("/")
@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "abd-erstellung-sync"}), 200


@app.route("/sync", methods=["GET", "POST"])
@require_api_key
def sync():
    log.info("Sync basladi (remote=%s)", request.remote_addr)
    try:
        # Import burada: cold start'ta env kontrolu sync aninda yapilsin
        from abd_sync import run_sync

        result = run_sync()
        log.info("Sync bitti: %s", result)
        return jsonify({"ok": True, **result}), 200
    except Exception as exc:  # noqa: BLE001
        log.error("Sync hata: %s\n%s", exc, traceback.format_exc())
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
