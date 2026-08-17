"""Scan orchestration: fan out over sources, score, dedupe, persist."""

from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config as cfg
from .db import Store
from .http import Http
from .lock import ScanLock
from .models import Job
from .scoring import Scorer
from .sources import build_sources
from .sources.base import Source

log = logging.getLogger("jobradar.pipeline")


def online(host: str = "1.1.1.1", port: int = 53, timeout: float = 3.0) -> bool:
    """Cheap connectivity check so an offline laptop skips silently."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dedupe_within_batch(jobs: list[Job]) -> list[Job]:
    """Collapse exact repeats inside one scan (same source key seen twice)."""
    seen: dict[str, Job] = {}
    for job in jobs:
        if job.id not in seen:
            seen[job.id] = job
    return list(seen.values())


def run_scan(settings: dict | None = None, *, only: list[str] | None = None,
             skip_offline_check: bool = False, ignore_lock: bool = False) -> dict:
    """Runs one full scan. Returns a summary dict."""
    settings = settings or cfg.load_settings()

    if not skip_offline_check and not online():
        log.info("no internet connection, skipping scan")
        return {"skipped": True, "reason": "offline"}

    # Machine-wide, so the scheduled task, the dashboard button and a manual
    # run cannot pile on top of each other.
    lock = ScanLock(cfg.resolve("data/scan.lock"))
    if not ignore_lock and not lock.acquire("scan"):
        holder = lock.owner() or {}
        log.info("another scan is already running (pid %s), skipping",
                 holder.get("pid", "?"))
        return {"skipped": True, "reason": "another scan is already running",
                "holder_pid": holder.get("pid")}

    try:
        return _run_scan_locked(settings, only)
    finally:
        lock.release()


def _run_scan_locked(settings: dict, only: list[str] | None) -> dict:
    secrets = cfg.load_secrets()
    companies = cfg.load_companies(settings["companies"])
    sources: list[Source] = build_sources(settings, companies, secrets)

    if only:
        wanted = set(only)
        sources = [
            s for s in sources
            if s.name in wanted
            or getattr(s, "platform", None) in wanted
            or getattr(s, "company", "") in wanted
        ]

    log.info("scanning %d sources", len(sources))

    store = Store(cfg.resolve(settings["database"]))
    scorer = Scorer(cfg.resolve(settings["profile"]))
    http = Http()
    # Tidy up rows left behind by scans that were killed mid-flight, so the
    # history in `jobradar health` never shows a run stuck in progress.
    store.close_stale_scans()
    scan_id = store.start_scan()
    started = time.monotonic()

    all_jobs: list[Job] = []
    errors: list[str] = []
    # Health is aggregated per source *name*, so 200 Greenhouse boards roll up
    # into one "ats:greenhouse" row instead of flooding the panel.
    per_source: dict[str, int] = {}
    per_source_err: dict[str, str] = {}

    workers = int(settings.get("workers", 8))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(s.safe_fetch, http): s for s in sources}
            for future in as_completed(futures):
                source = futures[future]
                jobs, error = future.result()
                label = getattr(source, "company", None) or source.name
                if error:
                    errors.append(f"{source.name}/{label}: {error}")
                    per_source_err[source.name] = error
                    log.warning("%s (%s): %s", source.name, label, error)
                else:
                    log.debug("%s (%s): %d jobs", source.name, label, len(jobs))
                per_source[source.name] = per_source.get(source.name, 0) + len(jobs)
                all_jobs.extend(jobs)
    except BaseException as exc:  # noqa: BLE001 - includes KeyboardInterrupt
        # Record the failure rather than leaving an open-ended scan row behind.
        store.fail_scan(scan_id, f"{type(exc).__name__}: {exc}")
        store.close()
        raise

    all_jobs = _dedupe_within_batch(all_jobs)
    for job in all_jobs:
        scorer.score(job)

    n_new, n_updated = store.upsert_jobs(all_jobs)
    n_dupes = store.mark_duplicates()

    for name, count in per_source.items():
        store.record_source(name, count, per_source_err.get(name))

    duration = time.monotonic() - started
    store.finish_scan(scan_id, fetched=len(all_jobs), new=n_new,
                      updated=n_updated, duration=duration, errors=errors[:50])

    top = store.query(min_score=float(settings.get("display_min_score", 45)),
                      status="new", limit=25)
    store.close()

    summary = {
        "skipped": False,
        "sources": len(sources),
        "fetched": len(all_jobs),
        "new": n_new,
        "updated": n_updated,
        "duplicates_collapsed": n_dupes,
        "errors": errors,
        "duration_s": round(duration, 1),
        "per_source": dict(sorted(per_source.items(), key=lambda kv: -kv[1])),
        "top_new": [
            {"title": r["title"], "company": r["company"], "score": r["score"],
             "city": r["city"], "url": r["url"], "source": r["source"]}
            for r in top
        ],
    }
    log.info(
        "scan done: %d fetched, %d new, %d updated, %d dupes, %.1fs",
        len(all_jobs), n_new, n_updated, n_dupes, duration,
    )
    return summary
