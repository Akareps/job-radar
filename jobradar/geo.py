"""Turn a posting's location text into a distance from where you live.

The original version of this tool hardcoded a list of Swiss municipalities.
That does not travel. Instead we resolve place names against the GeoNames
`cities5000` gazetteer (~69,000 places with population 5,000+, CC BY 4.0) and
score by great-circle distance from your home city.

The dataset is fetched once with `jobradar geo-update` and cached locally as a
compact TSV. Everything degrades gracefully without it: a configured list of
place names still works, so the tool is usable offline and on day one.
"""

from __future__ import annotations

import gzip
import io
import logging
import math
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("jobradar.geo")

GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
GEONAMES_MEMBER = "cities5000.txt"
CACHE_NAME = "cities.tsv.gz"

#: Country names and codes we recognise in a location string. Used to
#: disambiguate the many duplicate city names ("Cambridge", "Birmingham").
_COUNTRY_HINTS = {
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "svizzera": "CH",
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "uk": "GB",
    "netherlands": "NL", "holland": "NL", "nederland": "NL",
    "germany": "DE", "deutschland": "DE",
    "france": "FR", "austria": "AT", "österreich": "AT", "osterreich": "AT",
    "belgium": "BE", "belgique": "BE", "spain": "ES", "españa": "ES",
    "italy": "IT", "italia": "IT", "portugal": "PT", "ireland": "IE",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "finland": "FI",
    "poland": "PL", "czechia": "CZ", "czech republic": "CZ",
    "united states": "US", "usa": "US", "u.s.": "US", "america": "US",
    "canada": "CA", "australia": "AU", "singapore": "SG", "japan": "JP",
    "india": "IN", "china": "CN", "hong kong": "HK", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "luxembourg": "LU",
}
_COUNTRY_CODES = frozenset(_COUNTRY_HINTS.values())

_REMOTE = re.compile(
    r"\b(remote|anywhere|worldwide|work\s+from\s+home|home[\s-]+based|"
    r"telearbeit|home\s?office|distributed)\b",
    re.I,
)

#: Split a location string into candidate place fragments.
_SPLIT = re.compile(r"[,;/|()\[\]–—]|\s+-\s+|\bor\b|\band\b", re.I)

#: Aliases we keep for matching. Non-latin scripts can never match a posting
#: written in a latin script, so they are dead weight in the cache.
_LATIN = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .'\-]*")

#: Lowercased US state codes. "Austin, TX" and "New York, NY" carry no country
#: name, and many US city names are shared worldwide.
_US_STATES = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms "
    "mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv "
    "wi wy dc".split()
)


def _us_state_codes(blob: str) -> set[str]:
    """US-state-shaped tokens without mistaking words such as ``or``/``in``."""
    tokens = {
        match.group(0).lower()
        for match in re.finditer(r"\b[A-Z]{2}\b", blob)
    }
    # Some feeds lowercase state codes, but a two-letter field immediately
    # after a comma is still sufficiently explicit: ``Austin, tx``.
    tokens.update(
        match.group(1).lower()
        for match in re.finditer(r",\s*([A-Za-z]{2})(?=\s*(?:[,;/|()]|$))", blob)
    )
    return tokens.intersection(_US_STATES)


def fold(text: str) -> str:
    """Lowercase, strip diacritics, squeeze to single-spaced alphanumerics."""
    text = unicodedata.normalize("NFKD", (text or "").lower().replace("ß", "ss"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    country: str
    population: int

    def distance_to(self, other: "Place") -> float:
        return haversine_km(self.lat, self.lon, other.lat, other.lon)


class CityIndex:
    """Folded place name -> most populous matching city."""

    def __init__(self, places: dict[str, Place] | None = None):
        self._by_name: dict[str, Place] = places or {}

    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def available(self) -> bool:
        return bool(self._by_name)

    # ------------------------------------------------------------------ #
    # Building and caching
    # ------------------------------------------------------------------ #

    @classmethod
    def download(cls, http, cache_path: str | Path) -> "CityIndex":
        """Fetch GeoNames and write the compact cache. -> the built index."""
        resp = http.request("GET", GEONAMES_URL)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(f"could not download {GEONAMES_URL}")

        archive = zipfile.ZipFile(io.BytesIO(resp.content))
        raw = archive.read(GEONAMES_MEMBER).decode("utf-8", "replace")

        rows: list[tuple[str, str, str, str, str, str]] = []
        for line in raw.splitlines():
            f = line.split("\t")
            if len(f) < 15:
                continue
            name, ascii_name, alternates = f[1], f[2], f[3]
            lat, lon, country, population = f[4], f[5], f[8], f[14]

            # The canonical display name is GeoNames' own `asciiname`. Deriving
            # it from the alias list instead produced "Cirihe" for Zurich and
            # "BER" for Berlin, because aliases include transliterations and
            # airport codes.
            canonical = (ascii_name or name).strip()
            if not canonical:
                continue

            # Aliases exist only for matching, and the cap on how many we keep
            # has to be spent on the *useful* ones. Taking them in file order
            # sent "New York, NY" to a village in Ukraine: GeoNames names the
            # city "New York City", so the bare "New York" alias is the only
            # route to it, and it fell outside the cap. Rank aliases that
            # overlap the canonical name first.
            canonical_folded = fold(canonical)
            candidates = [
                alias.strip() for alias in alternates.split(",")
                if alias.strip() and len(alias.strip()) <= 40
                and _LATIN.fullmatch(alias.strip())
            ]

            def relevance(alias: str) -> tuple[int, int]:
                folded = fold(alias)
                overlaps = folded in canonical_folded or canonical_folded in folded
                return (0 if overlaps else 1, len(folded))

            # The cap has to be generous, not just well-ordered. Renamed cities
            # are reached *only* through an alias that shares nothing with the
            # canonical name - "Bangalore" for Bengaluru, "Bombay" for Mumbai -
            # so a tight cap plus an overlap-first sort drops exactly those.
            aliases = {name, ascii_name}
            aliases.update(sorted(candidates, key=relevance)[:50])
            joined = "|".join(sorted(a for a in aliases if a))
            rows.append((canonical, joined, lat, lon, country, population))

        cache = Path(cache_path)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cache, "wt", encoding="utf-8", newline="\n") as fh:
            fh.write("# GeoNames cities5000, CC BY 4.0 - https://www.geonames.org/\n")
            for row in rows:
                fh.write("\t".join(row) + "\n")

        log.info("cached %d cities to %s", len(rows), cache)
        return cls.load(cache)

    @classmethod
    def load(cls, cache_path: str | Path) -> "CityIndex":
        """Read the cache. Returns an empty index when it is absent."""
        cache = Path(cache_path)
        if not cache.exists():
            return cls()

        by_name: dict[str, Place] = {}
        try:
            with gzip.open(cache, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 6:
                        continue
                    canonical, joined, lat, lon, country, population = parts
                    try:
                        place_lat, place_lon = float(lat), float(lon)
                        pop = int(population or 0)
                    except ValueError:
                        continue
                    place = Place(canonical, place_lat, place_lon, country, pop)
                    for alias in joined.split("|"):
                        key = fold(alias)
                        if not key or len(key) < 2:
                            continue
                        # Duplicate names are everywhere; the biggest city wins,
                        # which is the right guess for a job posting.
                        current = by_name.get(key)
                        if current is None or pop > current.population:
                            by_name[key] = place
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            log.warning("city cache unreadable (%s); falling back to name lists", exc)
            return cls()

        return cls(by_name)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def lookup(self, name: str, country: str | None = None) -> Place | None:
        place = self._by_name.get(fold(name))
        if place is None:
            return None
        if country and place.country != country:
            # Name matched, but in the wrong country. Better to admit we do not
            # know than to place a London, Ontario job in London, England.
            return None
        return place

    def resolve(self, location: str, country_hint: str = "") -> Place | None:
        """Best guess at the city a posting's location string refers to."""
        if not location:
            return None

        blob = f"{location} {country_hint}"
        country = self.country_from(location, country_hint)

        fragments = [f.strip() for f in _SPLIT.split(blob) if f.strip()]
        # Longest first: "basel stadt" should beat "basel" when both resolve,
        # and a two-word city should not be missed by testing single words only.
        candidates = sorted(set(fragments), key=len, reverse=True)
        candidates.append(blob)

        best: Place | None = None
        for fragment in candidates:
            place = self.lookup(fragment, country)
            if place is None:
                continue
            if best is None or place.population > best.population:
                best = place
        if best is not None:
            return best

        # Nothing matched a whole fragment. Try each word, which rescues
        # "Greater Zurich Area" and "Basel-Stadt".
        for word in re.findall(r"[a-zA-Zà-ÿ]{3,}", blob):
            place = self.lookup(word, country)
            if place is not None and (best is None or place.population > best.population):
                best = place
        return best

    @staticmethod
    def country_from(location: str = "", country_hint: str = "") -> str | None:
        """Infer an ISO country, trusting a structured country field first.

        Free-form ``Wilmington, DE`` must keep meaning Delaware, while an ATS
        that supplies ``country=DE`` means Germany.  The separate inputs give
        us enough context to resolve that otherwise unavoidable ambiguity.
        """
        hint = (country_hint or "").strip()
        if hint:
            if re.fullmatch(r"[A-Za-z]{2}", hint):
                code = hint.upper()
                if code == "UK":
                    return "GB"
                # This is a source's structured country field, so trust any
                # two-letter ISO-style value.  Free-form location text below
                # remains deliberately conservative because CA/DE/IN can be
                # US state abbreviations.
                return code
            country = CityIndex._country_from(hint)
            if country:
                return country
        return CityIndex._country_from(location or "")

    @staticmethod
    def countries_from(location: str = "", country_hint: str = "") -> set[str]:
        """Return every explicit country without guessing ambiguous state codes."""
        hint = (country_hint or "").strip()
        if hint:
            if re.fullmatch(r"[A-Za-z]{2}", hint):
                code = hint.upper()
                return {"GB" if code == "UK" else code}
            countries = CityIndex._countries_from_text(hint)
            if countries:
                return countries
        return CityIndex._countries_from_text(location or "")

    @staticmethod
    def is_country_only(location: str = "", country_hint: str = "") -> bool:
        """Whether ``location`` contains a country but no usable city.

        Checking this before city lookup prevents tokens such as ``USA`` from
        accidentally matching an unrelated city alias in the GeoNames data.
        A two-letter value that is also a US state is country-only only when a
        source supplied it separately as the structured country field.
        """
        raw = (location or "").strip()
        hint = (country_hint or "").strip()
        if not raw:
            return bool(hint and CityIndex.country_from("", hint))
        if hint and fold(raw) == fold(hint) and CityIndex.country_from("", hint):
            return True

        folded = fold(raw)
        if any(folded == fold(name) for name in _COUNTRY_HINTS):
            return True
        if re.fullmatch(r"[A-Z]{2}", raw):
            return raw in _COUNTRY_CODES and raw.lower() not in _US_STATES
        return False

    @staticmethod
    def _country_from(blob: str) -> str | None:
        folded = f" {fold(blob)} "
        for name, code in _COUNTRY_HINTS.items():
            if f" {fold(name)} " in folded:
                return code
        # A US state code pins the country, which matters for the many city
        # names the US shares with the rest of the world.
        if _us_state_codes(blob):
            return "US"
        match = re.search(r"\b([A-Z]{2})\b", blob)
        if match and match.group(1) in _COUNTRY_CODES:
            return match.group(1)
        return None

    @staticmethod
    def _countries_from_text(blob: str) -> set[str]:
        """All explicit countries in free-form text; US states stay US states."""
        countries: set[str] = set()
        # Interpret colliding codes within each listed location.  This lets
        # ``Germany (DE)`` mean Germany while ``Germany / Wilmington, DE``
        # still records that the posting also has a US office.
        whole = f" {fold(blob)} "
        all_named_countries = {
            code for name, code in _COUNTRY_HINTS.items()
            if f" {fold(name)} " in whole
        }
        segments = re.split(
            r"\s*(?:[;/|]|\bor\b|\band\b|\s+[–—-]\s+)\s*", blob, flags=re.I,
        )
        for segment in segments:
            folded = f" {fold(segment)} "
            named_countries = {
                code for name, code in _COUNTRY_HINTS.items()
                if f" {fold(name)} " in folded
            }
            countries.update(named_countries)
            state_codes = _us_state_codes(segment)
            standalone = re.fullmatch(r"\s*([A-Za-z]{2})\s*", segment)
            # ``Germany (DE)`` repeats the named country; DE means Germany
            # there, not Delaware.  The same is true across ``Germany - DE``;
            # but ``Germany / Wilmington, DE`` still contains a US location.
            if any(
                state.upper() not in named_countries
                and not (standalone and standalone.group(1).upper() == state.upper()
                         and state.upper() in all_named_countries)
                for state in state_codes
            ):
                countries.add("US")
            for match in re.finditer(r"\b([A-Z]{2})\b", segment):
                code = match.group(1)
                if code in _COUNTRY_CODES and code.lower() not in _US_STATES:
                    countries.add(code)
        return countries


@lru_cache(maxsize=4)
def load_index(cache_path: str) -> CityIndex:
    return CityIndex.load(cache_path)


def is_remote(*chunks: str) -> bool:
    return bool(_REMOTE.search(" ".join(c for c in chunks if c)))
