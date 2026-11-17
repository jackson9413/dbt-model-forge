"""dbt-model-forge - a local-first dbt model review workbench.

Everything runs on this machine. The model SQL is analysed by the engine in
engine/ and never leaves the process; only the review history is stored, in a
local SQLite file.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

from engine import analyzer, report

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(BASE, "samples")
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "reviews.db")

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")

PROFILES = [
    {"key": "general", "label": "General review"},
    {"key": "grain_audit", "label": "Grain audit"},
    {"key": "incremental_review", "label": "Incremental & performance"},
    {"key": "new_model_onboarding", "label": "New model onboarding"},
    {"key": "legacy_refactor", "label": "Legacy refactor"},
]


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            model TEXT NOT NULL,
            profile TEXT NOT NULL,
            verdict TEXT NOT NULL,
            overall REAL NOT NULL,
            finding_count INTEGER NOT NULL,
            sql_text TEXT NOT NULL,
            yaml_text TEXT NOT NULL,
            result_json TEXT NOT NULL,
            rating INTEGER,
            notes TEXT
        )
        """
    )
    return conn


def sample_index():
    path = os.path.join(SAMPLES_DIR, "index.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_sample(sample_id):
    folder = os.path.join(SAMPLES_DIR, sample_id)
    if not os.path.isdir(folder):
        return None
    out = {}
    for key, filename in (("sql", "model.sql"), ("yaml", "schema.yml")):
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                out[key] = fh.read()
        else:
            out[key] = ""
    return out


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/profiles")
def profiles():
    return jsonify(PROFILES)


@app.route("/api/samples")
def samples():
    return jsonify(sample_index())


@app.route("/api/samples/<sample_id>")
def sample(sample_id):
    data = read_sample(sample_id)
    if data is None:
        return jsonify({"error": "sample not found"}), 404
    for item in sample_index():
        if item.get("id") == sample_id:
            data.update(item)
    return jsonify(data)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True) or {}
    sql = body.get("sql") or ""
    yaml_text = body.get("yaml") or ""
    model_name = (body.get("model_name") or "").strip()
    profile = body.get("profile") or "general"
    if not sql.strip():
        return jsonify({"error": "Paste a dbt model first."}), 400
    result = analyzer.analyze(sql, yaml_text, model_name or None, profile)
    run_id = None
    if body.get("save"):
        conn = db()
        cur = conn.execute(
            "INSERT INTO reviews (created_at, model, profile, verdict, overall,"
            " finding_count, sql_text, yaml_text, result_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                result["model"],
                result["profile"],
                result["score"]["verdict"],
                result["score"]["overall"],
                len(result["findings"]),
                sql,
                yaml_text,
                report.to_json(result),
            ),
        )
        conn.commit()
        run_id = cur.lastrowid
        conn.close()
    result["run_id"] = run_id
    result["markdown"] = report.to_markdown(result)
    return jsonify(result)


@app.route("/api/runs")
def runs():
    conn = db()
    rows = conn.execute(
        "SELECT id, created_at, model, profile, verdict, overall, finding_count,"
        " rating, notes FROM reviews ORDER BY id DESC LIMIT 60"
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/runs/<int:run_id>", methods=["PATCH"])
def update_run(run_id):
    body = request.get_json(silent=True) or {}
    conn = db()
    row = conn.execute("SELECT id FROM reviews WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "run not found"}), 404
    rating = body.get("rating")
    notes = body.get("notes")
    if rating is not None:
        conn.execute("UPDATE reviews SET rating = ? WHERE id = ?", (int(rating), run_id))
    if notes is not None:
        conn.execute("UPDATE reviews SET notes = ? WHERE id = ?", (notes, run_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": run_id})


@app.route("/api/export/<int:run_id>")
def export(run_id):
    fmt = (request.args.get("fmt") or "md").lower()
    conn = db()
    row = conn.execute(
        "SELECT result_json FROM reviews WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "run not found"}), 404
    result = json.loads(row["result_json"])
    if fmt == "json":
        payload = report.to_json(result)
        return Response(payload, mimetype="application/json")
    payload = report.to_markdown(result)
    return Response(payload, mimetype="text/markdown")


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    print("dbt-model-forge running on http://127.0.0.1:5057")
    app.run(host="127.0.0.1", port=5057, debug=False)
