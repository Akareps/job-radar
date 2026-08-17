"""Read a CV and turn it into scoring-profile hints.

Entirely optional. `jobradar init` asks the same questions itself, so the tool
works with no API key and no extra dependencies. When the `anthropic` package
and a key are both present, this pre-fills the wizard's answers from a PDF or
text CV so there is less to type — and it catches skills people forget to
mention about themselves.

The wizard asks for explicit consent before sending CV text to Anthropic and
shows every extracted value as an editable default before writing profile.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jobradar.cvparse")

MODEL = "claude-opus-5"

SYSTEM = """You extract structured facts from a CV so a job-matching tool can \
rank postings for this candidate.

Rules:
- Only report what the CV actually supports. Do not infer a skill from a job \
title, and do not pad the skill lists.
- Sort skills by how central they are to this person's work: `skills_high` is \
what they would be hired for, `skills_low` is incidental exposure.
- Use lowercase ISO 639-1 codes for languages (en, de, fr, el, ...), and \
include only languages they can work in.
- `target_roles` is what this person is plausibly applying for next given \
their trajectory, not every role they have held.
- Use "" or [] for anything the CV does not state. Never guess."""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Candidate's full name, or ''."},
        "current_location": {
            "type": "string",
            "description": "Where they are now, as 'City, Country'. '' if absent.",
        },
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ISO 639-1 codes they can work in.",
        },
        "degree_level": {
            "type": "string",
            "enum": ["bsc", "msc", "phd", "other", ""],
            "description": "Highest degree held or in progress.",
        },
        "graduating": {
            "type": "string",
            "description": "YYYY-MM if a degree is in progress, else ''.",
        },
        "years_experience": {
            "type": "integer",
            "description": "Full-time professional years, excluding internships. 0 if none.",
        },
        "target_roles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Job titles this person should be shown, most likely first.",
        },
        "skills_high": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Core skills they'd be hired for. Lowercase. Max 15.",
        },
        "skills_medium": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Solid working skills. Lowercase. Max 20.",
        },
        "skills_low": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tools and incidental exposure. Lowercase. Max 20.",
        },
    },
    "required": [
        "name", "current_location", "languages", "degree_level", "graduating",
        "years_experience", "target_roles", "skills_high", "skills_medium",
        "skills_low",
    ],
    "additionalProperties": False,
}


@dataclass
class CVHints:
    name: str = ""
    current_location: str = ""
    languages: list[str] = field(default_factory=list)
    degree_level: str = ""
    graduating: str = ""
    years_experience: int = 0
    target_roles: list[str] = field(default_factory=list)
    skills_high: list[str] = field(default_factory=list)
    skills_medium: list[str] = field(default_factory=list)
    skills_low: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.target_roles or self.skills_high or self.name)


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #

def extract_text(path: str | Path) -> str:
    """Plain text from a PDF, .txt or .md CV."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() == ".pdf":
        try:
            import pypdf
        except ImportError as exc:  # pragma: no cover - dependency guidance
            raise RuntimeError(
                "Reading a PDF CV needs pypdf. Install it with `pip install pypdf`, "
                "or paste your CV into a .txt file and point at that instead."
            ) from exc
        reader = pypdf.PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")

    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 100:
        raise RuntimeError(
            f"Only got {len(text)} characters out of {path.name}. If it is a "
            "scanned PDF the text layer is missing - paste the CV into a .txt "
            "file and use that."
        )
    return text


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def available() -> tuple[bool, str]:
    """-> (usable, why not). Credentials may come from a key or `ant auth login`."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "the `anthropic` package is not installed (pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # A stored CLI profile also works, so this is a hint rather than a bar.
        return True, "no ANTHROPIC_API_KEY set - will try a stored `ant auth login` profile"
    return True, ""


def from_cv(path: str | Path, model: str = MODEL) -> CVHints:
    """Extract profile hints from a CV. Raises with a readable message on failure."""
    import anthropic

    text = extract_text(path)
    # A CV is short; this bound only guards against someone passing a thesis.
    text = text[:60_000]

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={
                # A scoped extraction, not a reasoning problem.
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": f"Extract the profile from this CV.\n\n<cv>\n{text}\n</cv>",
            }],
        )
    except anthropic.AuthenticationError as exc:
        raise RuntimeError(
            "Anthropic rejected the credentials. Set ANTHROPIC_API_KEY, or run "
            "`ant auth login`."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise RuntimeError("Rate limited by the Anthropic API - try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Could not reach the Anthropic API: {exc}") from exc

    # Check before touching content: a refusal returns HTTP 200 with no text.
    if response.stop_reason == "refusal":
        raise RuntimeError(
            "The model declined to process this document. Fill the profile in "
            "by hand instead - `jobradar init` asks the same questions."
        )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The response was truncated. Try a shorter CV.")

    payload = next((b.text for b in response.content if b.type == "text"), "")
    if not payload:
        raise RuntimeError("The model returned no content.")

    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"Could not parse the extraction: {exc}") from exc

    return _to_hints(data)


def _to_hints(data: dict) -> CVHints:
    def strings(key: str, limit: int) -> list[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return []
        seen, out = set(), []
        for item in raw:
            value = str(item).strip().lower()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out[:limit]

    try:
        years = int(data.get("years_experience") or 0)
    except (TypeError, ValueError):
        years = 0

    return CVHints(
        name=str(data.get("name") or "").strip(),
        current_location=str(data.get("current_location") or "").strip(),
        languages=strings("languages", 8) or ["en"],
        degree_level=str(data.get("degree_level") or "").strip(),
        graduating=str(data.get("graduating") or "").strip(),
        years_experience=max(0, years),
        # Titles keep their capitalisation; everything else is folded.
        target_roles=[str(r).strip() for r in (data.get("target_roles") or [])][:10],
        skills_high=strings("skills_high", 15),
        skills_medium=strings("skills_medium", 20),
        skills_low=strings("skills_low", 20),
    )
