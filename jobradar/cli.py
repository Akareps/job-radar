"""Command line: scan, serve, rescore, top, health, discover."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import config as cfg
from .db import Store
from .pipeline import run_scan
from .scoring import rescore_db


def _ensure_streams() -> None:
    """Give the process real stdout/stderr even when launched windowless.

    The scheduled task runs pythonw.exe so no console window can appear. A
    GUI-subsystem process started without a console has sys.stdout and
    sys.stderr set to None, and the first print() or log write would then die
    with AttributeError. Point them at the null device instead; the scan's real
    output goes to data/scan.log through a FileHandler either way.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _log_to_file(settings: dict) -> None:
    log_path = cfg.resolve("data/scan.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
    logging.getLogger().addHandler(handler)


def cmd_scan(args) -> int:
    settings = cfg.load_settings(args.config)
    _log_to_file(settings)
    summary = run_scan(settings, only=args.only, skip_offline_check=args.force)

    if summary.get("skipped"):
        print(f"Skipped: {summary['reason']}")
        return 0

    print(
        f"\n{summary['fetched']} postings from {summary['sources']} sources "
        f"in {summary['duration_s']}s "
        f"-> {summary['new']} new, {summary['updated']} updated, "
        f"{summary['duplicates_collapsed']} duplicates collapsed"
    )
    if summary["errors"]:
        print(f"\n{len(summary['errors'])} source errors "
              f"(see `jobradar health`):")
        for err in summary["errors"][:8]:
            print(f"  - {err[:150]}")

    if args.sync:
        ok, message = sync_from_remote(settings)
        # A failed sync is informational, never fatal: the local scan already
        # succeeded and the cloud results will merge on the next attempt.
        logging.getLogger("jobradar").info("sync: %s", message)
        print(f"\n{message}")

    if summary["top_new"]:
        print("\nTop new matches:")
        for job in summary["top_new"][:15]:
            print(f"  {job['score']:5.1f}  {job['title'][:58]:58}  "
                  f"{job['company'][:26]:26}  {job['city'][:14]:14}  [{job['source']}]")
    else:
        print("\nNo new postings above the display threshold this run.")
    return 0


def cmd_serve(args) -> int:
    from .web import create_app

    settings = cfg.load_settings(args.config)
    app = create_app(settings)
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  jobradar dashboard -> {url}\n  (Ctrl+C to stop)\n")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "  WARNING: the dashboard has no authentication. Binding it to a "
            "network interface can expose your job-search data.\n"
        )
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


def cmd_rescore(args) -> int:
    settings = cfg.load_settings(args.config)
    with Store(cfg.resolve(settings["database"])) as store:
        n = rescore_db(store, cfg.resolve(settings["profile"]))
        store.mark_duplicates()
    print(f"Rescored {n} postings against {settings['profile']}")
    return 0


def cmd_top(args) -> int:
    settings = cfg.load_settings(args.config)
    with Store(cfg.resolve(settings["database"])) as store:
        rows = store.query(
            status=args.status, role_family=args.role, min_score=args.min_score,
            max_age_days=args.max_age, limit=args.limit,
        )
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
            return 0
        if not rows:
            print("No matches. Try `jobradar scan` first, or lower --min-score.")
            return 0
        print(f"\n{'score':>6}  {'title':58}  {'company':26}  {'where':14}  source")
        print("-" * 130)
        for r in rows:
            print(f"{r['score']:6.1f}  {(r['title'] or '')[:58]:58}  "
                  f"{(r['company'] or '')[:26]:26}  {(r['city'] or '')[:14]:14}  {r['source']}")
        print(f"\n{len(rows)} shown. Full detail: `jobradar serve`")
    return 0


def cmd_compact(args) -> int:
    """Shrink the database: cap descriptions, prune stale rows, VACUUM."""
    from .models import MAX_DESCRIPTION_CHARS

    settings = cfg.load_settings(args.config)
    db_path = cfg.resolve(settings["database"])
    before_bytes = db_path.stat().st_size if db_path.exists() else 0

    with Store(db_path) as store:
        stats = store.prune(max_age_days=args.max_age,
                            below_score=args.below_score,
                            hard_max_age_days=args.hard_max_age,
                            dry_run=args.dry_run)
        if args.dry_run:
            print(f"Would remove {stats['would_remove']:,} of "
                  f"{stats['remaining']:,} postings: untriaged rows scoring "
                  f"under {args.below_score} and older than {args.max_age}d, "
                  f"or anything older than {args.hard_max_age}d. Nothing changed.")
            return 0

        shortened = store.truncate_descriptions(MAX_DESCRIPTION_CHARS)
        saved = store.vacuum()

    after_bytes = db_path.stat().st_size if db_path.exists() else 0
    print(f"Removed {stats['removed']:,} stale postings "
          f"({stats['remaining']:,} remain, your shortlist/applied/rejected untouched).")
    print(f"Shortened {shortened:,} oversized descriptions to {MAX_DESCRIPTION_CHARS:,} chars.")
    print(f"Database {before_bytes/1e6:.1f} MB -> {after_bytes/1e6:.1f} MB "
          f"(reclaimed {saved/1e6:.1f} MB by VACUUM).")
    return 0


def cmd_init(args) -> int:
    from .wizard import run

    return run(force=args.force, cv_path=args.cv)


def cmd_geo_update(args) -> int:
    """Download the city dataset that distance-based location scoring needs."""
    from .geo import CACHE_NAME, CityIndex
    from .http import Http

    cache = cfg.resolve(f"data/{CACHE_NAME}")
    print("Downloading GeoNames cities5000 (~5 MB)...")
    try:
        index = CityIndex.download(Http(timeout=120, retries=1), cache)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}")
        print("Location scoring will fall back to the name hints in profile.yaml.")
        return 1

    size = cache.stat().st_size / 1e6
    print(f"Cached {len(index):,} places to {cache} ({size:.1f} MB).")
    print("Data: GeoNames, CC BY 4.0 - https://www.geonames.org/")

    settings = cfg.load_settings(args.config)
    profile_path = cfg.resolve(settings["profile"])
    if profile_path.exists():
        from .location import LocationClassifier
        import yaml

        with open(profile_path, "r", encoding="utf-8") as fh:
            profile = yaml.safe_load(fh) or {}
        classifier = LocationClassifier(profile.get("location", {}) or {}, index)
        print(f"\n{classifier.describe()}")
        if classifier.geocoding_available:
            print("Run `jobradar rescore` to re-rank with real distances.")
    return 0


def cmd_health(args) -> int:
    settings = cfg.load_settings(args.config)
    with Store(cfg.resolve(settings["database"])) as store:
        counts = store.counts()
        print("\nPostings:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

        print(f"\n{'source':24} {'last count':>10} {'fails':>6}  last ok")
        print("-" * 78)
        for row in store.source_health():
            status = "OK" if not row["last_error"] else f"ERR {row['last_error'][:40]}"
            print(f"{row['source'][:24]:24} {row['last_count']:>10} "
                  f"{row['fail_streak']:>6}  {(row['last_ok'] or '-')[:19]}  {status}")

        print(f"\n{'scan':>5}  {'when':20} {'found':>6} {'new':>5} {'time':>7}  errors")
        print("-" * 78)
        for row in store.recent_scans(10):
            errs = len(json.loads(row["errors"] or "[]"))
            print(f"{row['id']:>5}  {(row['started_at'] or '')[:19]:20} "
                  f"{row['n_fetched']:>6} {row['n_new']:>5} "
                  f"{(row['duration_s'] or 0):>6.1f}s  {errs}")
    return 0


def sync_from_remote(settings: dict, remote: str = "origin",
                     branch: str = "radar-state", timeout: int = 120) -> tuple[bool, str]:
    """Merge the GitHub Actions runner's database into the local one.

    -> (ok, message). Never raises and never blocks indefinitely: this also
    runs unattended from the scheduled task, where a git credential prompt
    would otherwise hang the process until the task's time limit kills it.
    """
    import subprocess
    import tempfile

    import gzip

    # The runner publishes a compressed database so larger state files remain
    # below GitHub's per-file limit.
    # The plain name is still accepted so an older state branch keeps working.
    remote_candidates = ("data/jobs.db.gz", "data/jobs.db")
    # Refuse any interactive credential prompt rather than wait on one.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

    def git(*cmd, binary: bool = False):
        return subprocess.run(
            ["git", *cmd], cwd=str(cfg.ROOT), capture_output=True,
            text=not binary, timeout=timeout, env=env,
        )

    try:
        if git("rev-parse", "--git-dir").returncode != 0:
            return False, ("Not a git repository, nothing to sync. "
                           "See README 'GitHub Actions backup'.")

        fetch = git("fetch", remote, branch)
        if fetch.returncode != 0:
            return False, (f"Could not fetch {remote}/{branch}: "
                           f"{(fetch.stderr or '').strip()[:200]}")

        payload = None
        for candidate in remote_candidates:
            show = git("show", f"{remote}/{branch}:{candidate}", binary=True)
            if show.returncode == 0 and show.stdout:
                payload, remote_path = show.stdout, candidate
                break
        if payload is None:
            return False, (f"No database on {remote}/{branch} yet "
                           f"(looked for {', '.join(remote_candidates)}).")
    except subprocess.TimeoutExpired:
        return False, f"git timed out after {timeout}s - skipping sync."
    except OSError as exc:
        return False, f"git unavailable: {exc}"

    if remote_path.endswith(".gz"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            return False, f"could not decompress {remote_path}: {exc}"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        from .models import MAX_DESCRIPTION_CHARS
        from .scoring import rescore_db

        with Store(cfg.resolve(settings["database"])) as store:
            inserted, refreshed, touched = store.merge_from(tmp_path)
            # Re-apply the storage cap because a remote database written by an
            # older release may still carry full-length descriptions.
            store.truncate_descriptions(MAX_DESCRIPTION_CHARS)
            # The remote scored these against whatever profile.yaml it had.
            # Scoring is a local derivation, so redo it here before the rows
            # are allowed to influence any ranking.
            rescored = rescore_db(store, cfg.resolve(settings["profile"]), touched)
            store.mark_duplicates()
    except Exception as exc:  # noqa: BLE001 - a bad remote DB must not kill a scan
        return False, f"merge failed: {type(exc).__name__}: {exc}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return True, (f"Merged {remote}/{branch}: {inserted} new postings, "
                  f"{refreshed} refreshed, {rescored} rescored locally. "
                  f"Your triage marks were preserved.")


def cmd_sync(args) -> int:
    settings = cfg.load_settings(args.config)
    ok, message = sync_from_remote(settings, args.remote, args.branch)
    print(message)
    return 0 if ok else 1


def cmd_merge(args) -> int:
    settings = cfg.load_settings(args.config)
    with Store(cfg.resolve(settings["database"])) as store:
        inserted, refreshed = store.merge_from(args.path)
    print(f"Merged {args.path}: {inserted} new, {refreshed} refreshed.")
    return 0


def cmd_verify(args) -> int:
    from .discover import verify_companies

    settings = cfg.load_settings(args.config)
    verify_companies(cfg.resolve(settings["companies"]),
                     workers=args.workers, prune=not args.keep)
    return 0


def cmd_discover(args) -> int:
    from .discover import discover_all

    settings = cfg.load_settings(args.config)
    discover_all(
        seed_path=cfg.resolve(args.seed),
        out_path=cfg.resolve(settings["companies"]),
        workers=args.workers,
        merge=not args.replace,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jobradar",
        description="Company-direct job discovery, ranked against your profile.",
    )
    parser.add_argument("-c", "--config", default=None, help="path to settings.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up your profile by answering a few questions")
    p.add_argument("--cv", help="path to a CV to pre-fill the answers from (PDF/txt/md)")
    p.add_argument("--force", action="store_true", help="overwrite an existing profile.yaml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("geo-update", help="download the city dataset for distance scoring")
    p.set_defaults(func=cmd_geo_update)

    p = sub.add_parser("scan", help="run one scan across all sources")
    p.add_argument("--only", nargs="*", help="limit to these source or platform names")
    p.add_argument("--force", action="store_true", help="scan even if the connectivity check fails")
    p.add_argument("--sync", action="store_true",
                   help="afterwards, merge the GitHub Actions runner's findings too")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("serve", help="open the dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--host", default="127.0.0.1",
        help="bind address (dashboard has no authentication; keep the localhost default)",
    )
    p.add_argument("--debug", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("rescore", help="re-apply profile.yaml to everything stored")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("top", help="print the current best matches")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--max-age", type=int, default=None, help="only postings newer than N days")
    p.add_argument("--status", default=None, help="new | seen | shortlist | applied | rejected")
    p.add_argument("--role", default=None, help="role family key from profile.yaml")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("health", help="source health and recent scan history")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("compact", help="prune stale postings and shrink the database file")
    p.add_argument("--max-age", type=int, default=21,
                   help="age in days after which low-scoring untriaged postings go")
    p.add_argument("--below-score", type=float, default=45.0,
                   help="score under which a posting counts as low-value (0 disables)")
    p.add_argument("--hard-max-age", type=int, default=180,
                   help="age in days after which untriaged postings go regardless of score")
    p.add_argument("--dry-run", action="store_true", help="report what would go, change nothing")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("sync", help="merge the GitHub Actions scan results into your local DB")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="radar-state")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("merge", help="merge another jobradar database file")
    p.add_argument("path")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("discover", help="resolve which ATS each seed company uses")
    p.add_argument("--seed", default="config/seed_companies.yaml")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--replace", action="store_true", help="overwrite companies.yaml instead of merging")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("verify", help="re-check stored boards, drop dead and misattributed ones")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--keep", action="store_true", help="report problems without removing entries")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    _ensure_streams()
    _setup_logging(args.verbose)
    return args.func(args)
