"""Geo and location-classification tests.

The distance cases need the city dataset; run `jobradar geo-update` first.
Without it, only the offline checks run.

Run: python scripts/test_geo.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.geo import CityIndex, fold, haversine_km, is_remote   # noqa: E402
from jobradar.location import LocationClassifier                    # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def near(label, got, want, tol):
    if got is None or abs(got - want) > tol:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want} +/- {tol}")


# --- offline: folding and distance ------------------------------------------
check("fold umlaut", fold("Zürich"), "zurich")
check("fold eszett", fold("Straße"), "strasse")
check("fold punctuation", fold("St. Gallen"), "st gallen")
check("fold accents", fold("Genève"), "geneve")
near("Zurich->Basel", haversine_km(47.3769, 8.5417, 47.5596, 7.5886), 75, 5)
near("London->Amsterdam", haversine_km(51.5074, -0.1278, 52.3676, 4.9041), 358, 10)
check("remote detected", is_remote("Fully Remote (EU)"), True)
check("non-remote", is_remote("Zurich, Switzerland"), False)


def make(home, hubs=(), index=None, hints=None):
    config = {
        "home": home,
        "bands": [
            {"within_km": 40, "tier": "local", "points": 32},
            {"within_km": 150, "tier": "regional", "points": -6},
        ],
        "also_good": [
            {"place": h, "tier": "hub", "points": 16, "within_km": 40} for h in hubs
        ],
        "remote": {"tier": "remote", "points": 2},
        "elsewhere": {"tier": "elsewhere", "points": -60},
        "unknown": {"tier": "unknown", "points": -12},
    }
    if hints:
        config["name_hints"] = hints
    return LocationClassifier(config, index or CityIndex())


# --- offline: name-hint fallback works with no dataset -----------------------
offline = make("Zurich, Switzerland",
               hints={"local": ["zurich", "zug", "basel"], "hub": ["london"]})
check("offline: no geocoding", offline.geocoding_available, False)
check("offline: local hint", offline.classify("Zurich, Switzerland").tier, "local")
check("offline: hub hint", offline.classify("London, United Kingdom").tier, "hub")
check("offline: remote", offline.classify("Remote").tier, "remote")
check("offline: unknown stays unknown", offline.classify("Tokyo, Japan").tier, "unknown")
# Whole-token matching: "England" must not match a hint of "gland".
gland = make("Gland, Switzerland", hints={"local": ["gland"]})
check("offline: England is not Gland",
      gland.classify("Oxford, England").tier != "local", True)

# --- online: everything below needs the dataset ------------------------------
cache = ROOT / "data" / "cities.tsv.gz"
index = CityIndex.load(cache)
if not index.available:
    print("City dataset absent - ran offline checks only.")
    print("Run `jobradar geo-update` to enable the distance tests.")
    if failures:
        print(f"\nFAILED {len(failures)}:\n" + "\n".join(f"  - {f}" for f in failures))
        sys.exit(1)
    print("All offline geo checks passed.")
    sys.exit(0)

zurich = make("Zurich, Switzerland",
              hubs=["London, United Kingdom", "Amsterdam, Netherlands"], index=index)
check("home resolves", zurich.geocoding_available, True)

for location, tier in [
    ("Zurich, Switzerland", "local"),
    ("Zürich", "local"),
    ("Schlieren", "local"),
    ("Zug", "local"),
    ("Winterthur", "local"),
    ("Basel (City)", "regional"),
    ("Geneva, Switzerland", "elsewhere"),
    ("London, United Kingdom", "hub"),
    ("Amsterdam, Netherlands", "hub"),
    ("Oxford, United Kingdom", "elsewhere"),
    ("Berlin, Germany", "elsewhere"),
    ("Remote", "remote"),
]:
    check(f"zurich: {location!r}", zurich.classify(location).tier, tier)

# The whole point of geocoding: it works from anywhere, not just Switzerland.
austin = make("Austin, Texas", index=index)
check("austin: local", austin.classify("Austin, TX").tier, "local")
check("austin: suburb is local", austin.classify("Round Rock, Texas").tier, "local")
check("austin: same-state city is regional",
      austin.classify("San Antonio, TX").tier, "regional")
# A US state code has to pin the country, or "New York, NY" matches a village
# of the same name in Ukraine ~10,000 km away.
verdict = austin.classify("New York, NY")
check("austin: New York resolves to the US city", verdict.country, "US")
near("austin: New York distance", verdict.distance_km, 2430, 60)

for home, place, tier in [
    ("Berlin, Germany", "Potsdam", "local"),
    ("Berlin, Germany", "Munich, Germany", "elsewhere"),
    ("Amsterdam, Netherlands", "Utrecht", "local"),
    ("Amsterdam, Netherlands", "Rotterdam", "regional"),
    ("Bangalore, India", "Bengaluru", "local"),
    ("Bangalore, India", "Mysore, India", "regional"),
    ("Singapore", "Singapore", "local"),
]:
    check(f"{home} / {place}", make(home, index=index).classify(place).tier, tier)

# Renamed and exonym cities reach the right place only through aliases.
for query, expect_country, min_pop in [
    ("Bangalore", "IN", 1_000_000),
    ("Bombay", "IN", 1_000_000),
    ("Cologne", "DE", 500_000),      # canonical is "Koeln"; a tiny Italian
    ("Vienna", "AT", 1_000_000),     # town is also literally named Cologne
    ("Praha", "CZ", 500_000),
    ("Firenze", "IT", 200_000),
    ("Den Haag", "NL", 200_000),
    ("New York", "US", 1_000_000),
]:
    place = index.resolve(query)
    check(f"resolve {query!r} country", place.country if place else None, expect_country)
    check(f"resolve {query!r} is the major city",
          bool(place and place.population >= min_pop), True)

if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All geo/location checks passed ({len(index):,} places indexed).")
