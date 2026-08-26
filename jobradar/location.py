"""Classify a posting's location relative to where you live.

Configured entirely from `profile.yaml`:

    location:
      home: "Berlin, Germany"
      bands:
        - {within_km: 40,  tier: local,    points: 32}
        - {within_km: 150, tier: regional, points: -6}
      also_good:
        - {place: "Amsterdam, Netherlands", tier: hub, points: 16, within_km: 40}
      remote:    {tier: remote,    points: 2}
      elsewhere: {tier: elsewhere, points: -60}
      unknown:   {tier: unknown,   points: -12}
      # Optional safety net used when the city dataset is unavailable or a
      # place name cannot be resolved.
      name_hints:
        local: [berlin]
        hub:   [amsterdam]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .geo import CityIndex, Place, fold, is_remote

log = logging.getLogger("jobradar.location")

DEFAULT_UNKNOWN = ("unknown", -12.0)


@dataclass(frozen=True)
class Verdict:
    tier: str
    points: float
    city: str = ""
    country: str = ""
    distance_km: float | None = None


class LocationClassifier:
    def __init__(self, config: dict[str, Any], index: CityIndex | None = None):
        self.config = config or {}
        self.index = index or CityIndex()

        self.bands = sorted(
            (b for b in self.config.get("bands", []) if b.get("within_km") is not None),
            key=lambda b: float(b["within_km"]),
        )
        self._remote = self.config.get("remote", {"tier": "remote", "points": 2})
        self._elsewhere = self.config.get("elsewhere", {"tier": "elsewhere", "points": -60})
        self._unknown = self.config.get("unknown", {"tier": "unknown", "points": -12})

        self.home: Place | None = None
        home_name = self.config.get("home")
        if home_name:
            self.home = self.index.resolve(home_name)
            if self.home is None and self.index.available:
                log.warning("could not locate home %r in the city dataset", home_name)

        # Extra places worth relocating for, each with its own radius.
        self.also_good: list[tuple[Place | None, dict]] = []
        for entry in self.config.get("also_good", []) or []:
            place = self.index.resolve(entry.get("place", ""))
            self.also_good.append((place, entry))

        # Folded name -> (tier, points), consulted when geocoding cannot help.
        self._hints: dict[str, tuple[str, float]] = {}
        for tier, names in (self.config.get("name_hints") or {}).items():
            points = self._points_for_tier(tier)
            for name in names or []:
                self._hints[fold(name)] = (tier, points)

    # ------------------------------------------------------------------ #

    def _points_for_tier(self, tier: str) -> float:
        for band in self.bands:
            if band.get("tier") == tier:
                return float(band.get("points", 0))
        for _, entry in self.also_good:
            if entry.get("tier") == tier:
                return float(entry.get("points", 0))
        for spec in (self._remote, self._elsewhere, self._unknown):
            if spec.get("tier") == tier:
                return float(spec.get("points", 0))
        return 0.0

    @property
    def geocoding_available(self) -> bool:
        return self.index.available and self.home is not None

    # ------------------------------------------------------------------ #

    def classify(self, location: str, country_hint: str = "",
                 remote_flag: bool = False) -> Verdict:
        text = f"{location} {country_hint}".strip()

        if remote_flag or (text and is_remote(text)):
            return Verdict(self._remote.get("tier", "remote"),
                           float(self._remote.get("points", 0)), "Remote")

        if not text:
            return self._unknown_verdict()

        place = self.index.resolve(location, country_hint)

        if place is not None and self.home is not None:
            distance = place.distance_to(self.home)
            for band in self.bands:
                if distance <= float(band["within_km"]):
                    return Verdict(band.get("tier", "local"),
                                   float(band.get("points", 0)),
                                   place.name, place.country, round(distance, 1))

            for hub, entry in self.also_good:
                if hub is None:
                    continue
                radius = float(entry.get("within_km", 40))
                if place.distance_to(hub) <= radius:
                    return Verdict(entry.get("tier", "hub"),
                                   float(entry.get("points", 0)),
                                   place.name, place.country, round(distance, 1))

            return Verdict(self._elsewhere.get("tier", "elsewhere"),
                           float(self._elsewhere.get("points", 0)),
                           place.name, place.country, round(distance, 1))

        # No coordinates: fall back to configured name hints so the tool still
        # ranks sensibly before `jobradar geo-update` has ever been run.
        hint = self._match_hint(text)
        if hint is not None:
            tier, points = hint
            return Verdict(tier, points, place.name if place else "",
                           place.country if place else "")

        if place is not None:
            return Verdict(self._elsewhere.get("tier", "elsewhere"),
                           float(self._elsewhere.get("points", 0)),
                           place.name, place.country)

        return self._unknown_verdict()

    def _match_hint(self, text: str) -> tuple[str, float] | None:
        folded = f" {fold(text)} "
        best: tuple[str, float] | None = None
        best_len = 0
        for name, verdict in self._hints.items():
            # Whole-token match, so "england" cannot match a hint of "gland".
            if f" {name} " in folded and len(name) > best_len:
                best, best_len = verdict, len(name)
        return best

    def _unknown_verdict(self) -> Verdict:
        return Verdict(self._unknown.get("tier", DEFAULT_UNKNOWN[0]),
                       float(self._unknown.get("points", DEFAULT_UNKNOWN[1])))

    # ------------------------------------------------------------------ #

    def describe(self) -> str:
        """One line for the setup wizard and `jobradar health`."""
        if not self.index.available:
            return ("no city dataset - ranking by name hints only. "
                    "Run `jobradar geo-update` for distance-based scoring.")
        if self.home is None:
            return (f"city dataset loaded ({len(self.index):,} places) but home "
                    f"{self.config.get('home')!r} did not resolve.")
        bands = ", ".join(f"{b['tier']}<={b['within_km']}km" for b in self.bands)
        return (f"home {self.home.name} ({self.home.country}); "
                f"{len(self.index):,} places; bands: {bands or 'none'}")
