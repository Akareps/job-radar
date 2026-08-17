"""Work out which ATS a company uses, so we can poll it directly.

Two passes per company:

1. Fingerprint - fetch the careers page (and the site root) and look for an
   ATS URL embedded in the markup. Almost every careers page either iframes
   its board or links to it, so this resolves most companies exactly.
2. Guess - derive candidate tokens from the company name and probe each ATS
   API. Catches companies whose careers page is fully client-side rendered.

Anything that resolves is then *verified* by calling the real API and checking
it returns a plausible posting count. Nothing lands in companies.yaml unverified.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

from .config import save_companies
from .http import Http
from .sources.ats import ATS_CLASSES

log = logging.getLogger("jobradar.discover")

# ATS URL fingerprints. Each yields (platform, token, extra-kwargs).
FINGERPRINTS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)")),
    ("lever",      re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([A-Za-z0-9_-]+)")),
    ("ashby",      re.compile(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9._-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)")),
    ("workday",    re.compile(r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("personio",   re.compile(r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:de|com)")),
    # Anchored on the ATS host on both sides. An earlier version alternated on
    # a bare "//" so it matched every protocol-relative URL on the page -
    # thousands of junk tokens per document and a huge CPU burn.
    ("workable",   re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([A-Za-z0-9_-]+)")),
    ("workable",   re.compile(r"([A-Za-z0-9_-]+)\.workable\.com")),
    ("recruitee",  re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com")),
    ("teamtailor", re.compile(r"([A-Za-z0-9_-]+)\.teamtailor\.com")),
    ("join",       re.compile(r"join\.com/companies/([A-Za-z0-9_-]+)")),
]

# Tokens that are part of the ATS's own URLs, never a customer board name.
TOKEN_BLOCKLIST = {
    "www", "api", "jobs", "job", "boards", "board", "careers", "career", "embed",
    "static", "assets", "cdn", "app", "apply", "posting", "postings", "v1", "v0",
    "en", "de", "fr", "search", "company", "companies", "job_board", "widget",
    "images", "img", "css", "js", "help", "support", "about", "privacy", "terms",
    "greenhouse", "lever", "ashbyhq", "smartrecruiters", "workday", "personio",
    "workable", "recruitee", "teamtailor",
}

CAREERS_PATHS = [
    "/careers", "/en/careers", "/jobs", "/en/jobs", "/company/careers",
    "/about/careers", "/careers/jobs", "/join-us", "/work-with-us",
    "/de/jobs", "/karriere", "/en/about-us/careers", "/company/jobs",
]


def slug_variants(name: str) -> list[str]:
    """Candidate board tokens derived from a company name."""
    base = re.sub(r"[^a-z0-9\s]", "", name.lower()).strip()
    words = base.split()
    if not words:
        return []
    joined = "".join(words)
    hyphen = "-".join(words)
    first = words[0]
    variants = [joined, hyphen, first]
    # Drop legal suffixes: "Sygnum Bank AG" -> "sygnum".
    trimmed = [w for w in words if w not in
               ("ag", "sa", "gmbh", "ltd", "inc", "llc", "group", "holding", "bank",
                "technologies", "technology", "solutions", "labs", "systems")]
    if trimmed and trimmed != words:
        variants += ["".join(trimmed), "-".join(trimmed), trimmed[0]]
    seen, out = set(), []
    for v in variants:
        if len(v) >= 3 and v not in seen and v not in TOKEN_BLOCKLIST:
            seen.add(v)
            out.append(v)
    # Three variants is the sweet spot: the fourth and fifth almost never hit
    # and each one costs a probe against every ATS platform.
    return out[:3]


#: Careers pages are occasionally enormous (one Personio landing page is 1.7MB).
#: A board URL always appears well before that, and capping keeps a pathological
#: page from dominating a scan.
MAX_FINGERPRINT_BYTES = 400_000
#: Per-pattern match cap - one real board never appears hundreds of times.
MAX_MATCHES_PER_PATTERN = 12


def fingerprint_html(html: str) -> list[tuple[str, str, dict]]:
    """Extract every plausible (platform, token, extra) from page markup."""
    html = html[:MAX_FINGERPRINT_BYTES]
    hits: list[tuple[str, str, dict]] = []
    for platform, rx in FINGERPRINTS:
        for n, match in enumerate(rx.finditer(html)):
            if n >= MAX_MATCHES_PER_PATTERN:
                break
            if platform == "workday":
                tenant, wd, site = match.group(1), match.group(2), match.group(3)
                if tenant.lower() in TOKEN_BLOCKLIST or site.lower() in ("wday", "cxs"):
                    continue
                hits.append(("workday", f"{tenant}/{site}", {"host": f"{tenant}.{wd}"}))
                continue
            token = match.group(1)
            if not token or token.lower() in TOKEN_BLOCKLIST or len(token) < 3:
                continue
            hits.append((platform, token, {}))
    # Preserve order, drop repeats.
    seen, out = set(), []
    for item in hits:
        key = (item[0], item[1].lower())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


#: Below this token_set_ratio between the seed name and the name the board
#: reports for itself, a guessed token is treated as a different company.
IDENTITY_THRESHOLD = 55


def identity_matches(http: Http, platform: str, token: str, expected: str) -> bool | None:
    """Does this board actually belong to `expected`?

    -> True / False, or None when the platform exposes no name to check against
    (Lever, Ashby, Teamtailor and join.com return only what we already asked for).

    Guessing tokens from a company name is where discovery goes wrong: probing
    "julius" found a Personio board belonging to someone else entirely, and
    "meta" found a one-job Recruitee board that is not Meta.
    """
    cls = ATS_CLASSES.get(platform)
    getter = getattr(cls, "board_identity", None)
    if getter is None:
        return None
    try:
        actual = getter(http, token)
    except Exception:  # noqa: BLE001
        return None
    if not actual:
        return None

    from rapidfuzz import fuzz

    score = fuzz.token_set_ratio(normalise_name(expected), normalise_name(actual))
    if score < IDENTITY_THRESHOLD:
        log.info("rejecting %s/%s: board says %r, expected %r (%.0f%%)",
                 platform, token, actual, expected, score)
        return False
    return True


def normalise_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    n = re.sub(r"\b(ag|sa|gmbh|ltd|limited|llc|inc|plc|bv|nv|holding|holdings|"
               r"group|international|global|schweiz|switzerland|suisse|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def verify(http: Http, platform: str, token: str, extra: dict) -> int | None:
    """Call the real API. Returns posting count, or None if the board isn't real."""
    cls = ATS_CLASSES.get(platform)
    if cls is None or not hasattr(cls, "discover"):
        return None
    try:
        if platform == "workday":
            return cls.discover(http, token, extra.get("host", ""))
        return cls.discover(http, token)
    except Exception as exc:  # noqa: BLE001
        log.debug("verify %s/%s failed: %s", platform, token, exc)
        return None


def discover_company(http: Http, entry: dict) -> dict | None:
    """-> a companies.yaml entry, or None if no board could be verified."""
    name = entry["name"]

    # Caller already knows the platform: just confirm it still works.
    if entry.get("platform") and entry.get("token"):
        extra = {k: v for k, v in entry.items()
                 if k not in ("name", "platform", "token", "url", "tier", "note", "enabled")}
        count = verify(http, entry["platform"], entry["token"], extra)
        if count is not None:
            return {**entry, "verified_jobs": count}
        log.info("%s: configured %s board no longer resolves", name, entry["platform"])
        return None

    candidates: list[tuple[str, str, dict]] = []
    via = "fingerprint"

    # --- pass 1: fingerprint the careers page --------------------------
    urls: list[str] = []
    if entry.get("url"):
        urls.append(entry["url"])
    if entry.get("domain"):
        domain = entry["domain"].rstrip("/")
        if not domain.startswith("http"):
            domain = "https://" + domain
        urls += [domain + path for path in CAREERS_PATHS[:6]]

    for url in urls[:7]:
        html = http.get_text(url)
        if not html:
            continue
        found = fingerprint_html(html)
        if found:
            candidates.extend(found)
            break

    # --- pass 2: guess tokens from the name ----------------------------
    # Guessed tokens are riskier: probing "unique" on Personio resolves to an
    # unrelated German company called "unique land use GmbH". Entries found
    # this way are tagged so you can eyeball them in companies.yaml.
    if not candidates:
        via = "guess"
        for token in slug_variants(name):
            for platform in ("greenhouse", "lever", "ashby", "personio",
                             "smartrecruiters", "workable", "recruitee", "teamtailor"):
                candidates.append((platform, token, {}))

    # --- verify, best first --------------------------------------------
    best: tuple[int, str, str, dict] | None = None
    unverifiable = False
    for rank, (platform, token, extra) in enumerate(candidates[:28]):
        count = verify(http, platform, token, extra)
        if count is None or count == 0:
            continue
        # A token we guessed has to prove it belongs to this company. A token
        # we read off the company's own careers page needs no such proof.
        if via == "guess":
            matches = identity_matches(http, platform, token, name)
            if matches is False:
                continue
            if matches is None:
                unverifiable = True
        if best is None or count > best[0]:
            best = (count, platform, token, extra)
        # A fingerprinted hit with live postings is almost certainly correct,
        # so stop rather than letting a name-guess outvote it on job count.
        if via == "fingerprint" and rank < 3:
            break

    if best is None:
        return None

    count, platform, token, extra = best
    result = {
        "name": name,
        "platform": platform,
        "token": token,
        "verified_jobs": count,
        # "guess-unverified" means the token was guessed AND the platform gives
        # us no company name to check it against. Worth an eyeball.
        "via": "guess-unverified" if (via == "guess" and unverifiable) else via,
        **extra,
    }
    if entry.get("tier"):
        result["tier"] = entry["tier"]
    if entry.get("note"):
        result["note"] = entry["note"]
    return result


def verify_companies(out_path: Path, workers: int = 10, prune: bool = True) -> list[dict]:
    """Re-check every board in companies.yaml.

    Drops entries whose board no longer resolves, and entries that were resolved
    by guessing a token and turn out to belong to a different company. Worth
    running every few months - boards get renamed and companies migrate ATS.
    """
    with open(out_path, "r", encoding="utf-8") as fh:
        companies = (yaml.safe_load(fh) or {}).get("companies", [])

    http = Http(timeout=20, retries=1)
    kept: list[dict] = []
    dropped: list[str] = []

    def check(entry: dict) -> tuple[dict, str | None]:
        extra = {k: v for k, v in entry.items()
                 if k not in ("name", "platform", "token", "enabled",
                              "tier", "note", "verified_jobs", "via")}
        count = verify(http, entry["platform"], entry["token"], extra)
        if count is None:
            return entry, "board no longer resolves"
        if str(entry.get("via", "")).startswith("guess"):
            if identity_matches(http, entry["platform"], entry["token"], entry["name"]) is False:
                return entry, "board belongs to a different company"
        entry["verified_jobs"] = count
        return entry, None

    print(f"Re-verifying {len(companies)} boards...\n")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, problem in pool.map(check, companies):
            if problem:
                dropped.append(f"{entry['name']} ({entry['platform']}/{entry['token']}): {problem}")
                print(f"  DROP  {entry['name'][:34]:34} {problem}", flush=True)
                if not prune:
                    kept.append(entry)
            else:
                kept.append(entry)
                print(f"  ok    {entry['name'][:34]:34} "
                      f"{entry['platform']}/{entry['token']} "
                      f"({entry['verified_jobs']} jobs)", flush=True)

    kept.sort(key=lambda c: c["name"].lower())
    header = (
        "# Company-direct ATS boards. Generated by `jobradar discover`\n"
        "# and re-checked by `jobradar verify`.\n"
        "# Add entries by hand too - name/platform/token is all that's required."
    )
    save_companies(out_path, kept, header)
    total = sum(c.get("verified_jobs", 0) for c in kept)
    print(f"\nKept {len(kept)} boards ({total} live postings reachable).")
    if dropped:
        print(f"Dropped {len(dropped)}:")
        for d in dropped:
            print(f"  - {d}")
    return kept


def discover_all(seed_path: Path, out_path: Path, workers: int = 10,
                 merge: bool = True) -> list[dict]:
    with open(seed_path, "r", encoding="utf-8") as fh:
        seed = yaml.safe_load(fh) or {}
    entries: list[dict] = seed.get("companies", [])

    existing: dict[str, dict] = {}
    if merge and Path(out_path).exists():
        with open(out_path, "r", encoding="utf-8") as fh:
            prev = yaml.safe_load(fh) or {}
        existing = {c["name"]: c for c in prev.get("companies", [])}

    http = Http(timeout=20, retries=1)
    resolved: list[dict] = []
    failed: list[str] = []

    print(f"Probing {len(entries)} companies across "
          f"{len(ATS_CLASSES)} ATS platforms...\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(discover_company, http, e): e for e in entries}
        for i, future in enumerate(as_completed(futures), 1):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("%s raised %s", entry["name"], exc)
                result = None
            if result:
                resolved.append(result)
                print(f"  [{i:3}/{len(entries)}] {result['name'][:34]:34} "
                      f"-> {result['platform']:16} {result['token'][:30]:30} "
                      f"({result['verified_jobs']} jobs)", flush=True)
            else:
                failed.append(entry["name"])
                print(f"  [{i:3}/{len(entries)}] {entry['name'][:34]:34} -> not found",
                      flush=True)

    merged = dict(existing)
    for entry in resolved:
        merged[entry["name"]] = entry
    companies = sorted(merged.values(), key=lambda c: c["name"].lower())

    header = (
        "# Company-direct ATS boards. Generated by `jobradar discover`.\n"
        "# Every entry here was verified against the live API at write time.\n"
        "# Add entries by hand too - name/platform/token is all that's required."
    )
    save_companies(out_path, companies, header)

    total_jobs = sum(c.get("verified_jobs", 0) for c in resolved)
    print(f"\nResolved {len(resolved)}/{len(entries)} companies "
          f"({total_jobs} live postings reachable).")
    print(f"Wrote {len(companies)} entries to {out_path}")
    if failed:
        print(f"\nNot resolved ({len(failed)}): {', '.join(failed[:25])}"
              + (" ..." if len(failed) > 25 else ""))
        print("These likely use an in-house or unsupported ATS. "
              "Add them by hand if you find their board URL.")
    return companies
