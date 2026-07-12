"""Demo storefront Flask app — the middle tier.

Endpoints (per README):
  GET /        -> products from the DB as JSON
  GET /health  -> {"status": "ok"} iff the app can reach the DB

The status codes are chosen so the injected faults surface as the symptoms the
eval asserts on (ai-agent/evals/incidents.py):

  - DB container stopped        -> can't connect        -> 503
  - DB password rotated (stale) -> auth fails            -> 500

A fresh connection is opened per request (no pool) so a password rotation or a
stopped DB takes effect immediately on the next request — important for the
chaos demo.
"""

import os

import psycopg2
from flask import Flask, jsonify

app = Flask(__name__)

DB = dict(
    host=os.environ.get("DB_HOST", "db"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "storefront"),
    user=os.environ.get("DB_USER", "appuser"),
    password=os.environ.get("DB_PASSWORD", "apppass"),
)


def _connect():
    # Short timeout so a stopped DB fails fast as 503 instead of hanging.
    return psycopg2.connect(connect_timeout=3, **DB)


@app.get("/health")
def health():
    try:
        conn = _connect()
        conn.cursor().execute("SELECT 1")
        conn.close()
        return jsonify(status="ok")
    except psycopg2.OperationalError as exc:
        msg = str(exc).lower()
        # Auth failure (stale password) -> 500; anything else (DB unreachable,
        # container down) -> 503.
        if "password" in msg or "authentication" in msg:
            return jsonify(status="error", reason="db-auth"), 500
        return jsonify(status="error", reason="db-unreachable"), 503


@app.get("/")
def index():
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT id, name, price FROM products ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        products = [
            {"id": r[0], "name": r[1], "price": float(r[2])} for r in rows
        ]
        return jsonify(products=products)
    except psycopg2.OperationalError as exc:
        msg = str(exc).lower()
        if "password" in msg or "authentication" in msg:
            return jsonify(status="error", reason="db-auth"), 500
        return jsonify(status="error", reason="db-unreachable"), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
