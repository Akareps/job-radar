"""Local dashboard. Ranked list, filters, triage state, source health."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from . import config as cfg
from . import ranking
from .db import VALID_STATUS, Store
from .lock import ScanLock

#: How many rows an alternate ranking mode may reorder in one request. Modes
#: sort in Python rather than SQL, so this bounds the working set. Comfortably
#: above the full database; sorting this many dicts costs a few milliseconds.
DEEP_RANK_CAP = 25_000

# Tracks only the scan this dashboard process started. Whether *any* scan is
# running machine-wide is answered by the ScanLock file, since the scheduled
# task and the command line start scans this process knows nothing about.
_scan_thread_lock = threading.Lock()
_scan_state: dict = {"running": False, "started": None, "last": None}


def _row_to_dict(row) -> dict:
    data = dict(row)
    for key in ("score_breakdown", "tags", "required_languages"):
        try:
            data[key] = json.loads(data.get(key) or ("{}" if key == "score_breakdown" else "[]"))
        except (ValueError, TypeError):
            data[key] = {} if key == "score_breakdown" else []
    posted = data.get("posted_at")
    data["age_label"] = _age_label(posted)
    data["is_fresh"] = _age_hours(posted) is not None and _age_hours(posted) < 48
    data["description"] = (data.get("description") or "")[:1500]
    return data


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _age_label(iso: str | None) -> str:
    hours = _age_hours(iso)
    if hours is None:
        return "unknown"
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 14:
        return f"{int(days)}d ago"
    if days < 60:
        return f"{int(days / 7)}w ago"
    return f"{int(days / 30)}mo ago"


def create_app(settings: dict | None = None) -> Flask:
    settings = settings or cfg.load_settings()
    db_path = cfg.resolve(settings["database"])
    profile = settings.get("profile", "config/profile.yaml")
    min_score = float(settings.get("display_min_score", 45))

    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    def store() -> Store:
        return Store(db_path)

    # ------------------------------------------------------------------ #

    @app.route("/")
    def index():
        with store() as s:
            labels = _family_labels(profile)
            return render_template(
                "index.html",
                counts=s.counts(),
                facets=s.facets(),
                family_labels=labels,
                pay_labels=_pay_labels(profile),
                home_label=_home_label(profile),
                min_score=min_score,
                health=[dict(r) for r in s.source_health()],
                scans=[dict(r) for r in s.recent_scans(8)],
            )

    @app.route("/api/jobs")
    def api_jobs():
        args = request.args
        show_all = args.get("show_all") == "1"
        mode = args.get("mode") or ranking.DEFAULT_MODE
        page_size = max(1, min(int(args.get("limit", 100)), 500))
        offset = max(0, int(args.get("offset", 0)))

        # A non-default mode reorders the whole result set, so pagination has
        # to happen after ranking, not in SQL. Pull a generous working set,
        # rank it, then slice. Even the unfiltered view is only a few thousand
        # rows, so this stays well under a millisecond of sorting.
        deep = mode != ranking.DEFAULT_MODE
        fetch_limit = DEEP_RANK_CAP if deep else page_size
        fetch_offset = 0 if deep else offset
        # In the default mode the stored score IS the displayed score, so the
        # threshold can be applied in SQL, which is cheap. In any other mode
        # the number on screen is recomputed per row, and filtering in SQL
        # would drop rows the mode ranks far above the threshold: a software
        # role stored at 63 but shown at 164 under "Best paid" disappeared.
        sql_min_score = None if (show_all or deep) else min_score

        with store() as s:
            rows = s.query(
                status=args.get("status") or None,
                role_family=args.get("role") or None,
                tier=args.get("tier") or None,
                source=args.get("source") or None,
                min_score=sql_min_score,
                max_age_days=int(args["max_age"]) if args.get("max_age") else None,
                search=args.get("q") or None,
                include_duplicates=args.get("dupes") == "1",
                include_hidden=args.get("status") == "hidden",
                limit=fetch_limit,
                offset=fetch_offset,
            )
            total = s.count_matching(
                status=args.get("status") or None,
                role_family=args.get("role") or None,
                tier=args.get("tier") or None,
                source=args.get("source") or None,
                min_score=sql_min_score,
                max_age_days=int(args["max_age"]) if args.get("max_age") else None,
                search=args.get("q") or None,
                include_duplicates=args.get("dupes") == "1",
                include_hidden=args.get("status") == "hidden",
            )

        jobs = [_row_to_dict(r) for r in rows]
        ranking.rank(jobs, mode)
        # Whether the working set hit the cap is a property of the fetch, not
        # of the count left after filtering below.
        truncated = deep and len(rows) >= DEEP_RANK_CAP
        if deep and not show_all:
            # Apply the threshold against the score the user is actually
            # looking at, now that the mode has computed it.
            jobs = [job for job in jobs
                    if (job.get("ranked_score") or job.get("score") or 0.0) >= min_score]
            total = len(jobs)
        # A non-default mode reorders the working set, so anything past
        # DEEP_RANK_CAP is not reachable in that mode. Report the reachable
        # count rather than the raw total, otherwise the UI promises pages
        # that come back empty.
        reachable = min(total, DEEP_RANK_CAP) if deep else total
        if deep:
            jobs = jobs[offset:offset + page_size]

        for job in jobs:
            job["score"] = job.pop("ranked_score", job["score"])

        return jsonify({
            "jobs": jobs,
            "total": reachable,
            "total_matching": total,
            "truncated": truncated,
            "offset": offset,
            "limit": page_size,
            "has_more": offset + len(jobs) < reachable,
            "mode": mode,
        })

    @app.route("/api/modes")
    def api_modes():
        return jsonify({"modes": ranking.mode_list(), "default": ranking.DEFAULT_MODE})

    @app.route("/api/jobs/<job_id>/status", methods=["POST"])
    def api_set_status(job_id: str):
        status = (request.json or {}).get("status", "")
        if status not in VALID_STATUS:
            return jsonify({"error": f"invalid status {status!r}"}), 400
        with store() as s:
            if not s.set_status(job_id, status):
                return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, "status": status})

    @app.route("/api/jobs/<job_id>/notes", methods=["POST"])
    def api_set_notes(job_id: str):
        with store() as s:
            if not s.set_notes(job_id, (request.json or {}).get("notes", "")):
                return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/stats")
    def api_stats():
        # Read the machine-wide lock, not this process's flag: the hourly task
        # runs in a completely separate process, and the dashboard used to
        # report "no scan running" while one was in full flight.
        holder = ScanLock(cfg.resolve("data/scan.lock")).owner()
        with store() as s:
            return jsonify({
                "counts": s.counts(),
                "facets": s.facets(),
                "health": [dict(r) for r in s.source_health()],
                "scans": [dict(r) for r in s.recent_scans(8)],
                "scan_running": holder is not None,
                "scan_owner": holder,
                "scan_is_ours": _scan_state["running"],
                "last_scan_result": _scan_state["last"],
            })

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        """Fire a scan in the background so the page stays responsive."""
        holder = ScanLock(cfg.resolve("data/scan.lock")).owner()
        if holder is not None:
            return jsonify({
                "ok": False,
                "error": f"a scan is already running (pid {holder.get('pid')})",
            }), 409

        def worker():
            with _scan_thread_lock:
                _scan_state.update(running=True, started=datetime.now(timezone.utc).isoformat())
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "jobradar", "scan"],
                        cwd=str(cfg.ROOT), capture_output=True, text=True, timeout=3600,
                    )
                    _scan_state["last"] = {
                        "ok": proc.returncode == 0,
                        "output": (proc.stdout or proc.stderr or "")[-4000:],
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                except Exception as exc:  # noqa: BLE001
                    _scan_state["last"] = {"ok": False, "output": str(exc),
                                           "at": datetime.now(timezone.utc).isoformat()}
                finally:
                    _scan_state["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/sync", methods=["POST"])
    def api_sync():
        """Pull the GitHub Actions runner's findings into the local database."""
        from .cli import sync_from_remote

        ok, message = sync_from_remote(settings)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

    return app


def _read_profile(profile_path) -> dict:
    import yaml
    try:
        with open(cfg.resolve(str(profile_path)), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}


def _family_labels(profile_path) -> dict[str, str]:
    data = _read_profile(profile_path)
    labels = {
        key: spec.get("label", key)
        for key, spec in (data.get("role_families") or {}).items()
    }
    labels["other"] = "Other"
    return labels


def _home_label(profile_path) -> str:
    """Header subtitle: where this instance is hunting."""
    location = _read_profile(profile_path).get("location") or {}
    home = location.get("home") or ""
    hubs = [g.get("place", "") for g in (location.get("also_good") or [])]
    parts = [p for p in [home, *hubs] if p]
    return " · ".join(parts) if parts else "set a home city in profile.yaml"


def _pay_labels(profile_path) -> dict[str, str]:
    bands = (_read_profile(profile_path).get("pay_bands") or {}).get("labels") or {}
    return {str(k): v for k, v in bands.items()}
