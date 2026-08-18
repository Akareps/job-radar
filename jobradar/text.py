"""Text utilities: HTML stripping, language guess, location and seniority parsing."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from dateutil import parser as dateparser

# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6]|br)\s*/?>", re.I)


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = _SCRIPT.sub(" ", raw)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #

# Function words that give a posting's language away. Deliberately cheap - we
# only need to know whether a posting is in a language the user reads, not to
# do real language identification.
_LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "de": (" und ", " oder ", " mit ", " für ", " wir ", " sie ", " sind ",
           " werden ", " unser", " deine", " ihre ", " kenntnisse", " erfahrung",
           " aufgaben", " anforderungen", " stelle", " mitarbeiter", " bereich",
           " sowie ", " abgeschlossen", " ausbildung", " bewerbung"),
    "fr": (" et ", " ou ", " des ", " les ", " vous ", " nous ", " votre ",
           " notre ", " expérience", " compétences", " poste", " équipe",
           " ainsi ", " travail ", " entreprise"),
    "es": (" y ", " de ", " los ", " las ", " para ", " con ", " nuestro",
           " experiencia", " conocimientos", " puesto", " equipo", " empresa"),
    "it": (" e ", " di ", " per ", " con ", " nostro", " esperienza",
           " competenze", " azienda", " ruolo", " squadra"),
    "nl": (" en ", " van ", " het ", " een ", " voor ", " met ", " onze ",
           " ervaring", " kennis", " functie", " team ", " bedrijf"),
    "pt": (" e ", " de ", " para ", " com ", " nosso", " experiência",
           " conhecimentos", " equipa", " empresa"),
    "pl": (" i ", " oraz ", " dla ", " nasz", " doświadczenie", " wiedza",
           " stanowisko", " zespół", " firma"),
    "sv": (" och ", " för ", " med ", " vår", " erfarenhet", " kunskap",
           " tjänst", " företag"),
}

#: Endonyms, so "Deutschkenntnisse" and "fluent German" both trip the same rule.
_LANGUAGE_NAMES: dict[str, tuple[str, ...]] = {
    "de": ("german", "deutsch"),
    "fr": ("french", "französisch", "francais", "français"),
    "es": ("spanish", "español", "espanol"),
    "it": ("italian", "italiano", "italienisch"),
    "nl": ("dutch", "nederlands", "niederländisch"),
    "pt": ("portuguese", "português", "portugues"),
    "pl": ("polish", "polski"),
    "sv": ("swedish", "svenska"),
    "da": ("danish", "dansk"),
    "no": ("norwegian", "norsk"),
    "fi": ("finnish", "suomi"),
    "en": ("english", "englisch", "anglais"),
}

#: Words that turn "we mention this language" into "you must have it".
#: Accented terms are listed in both forms - postings routinely drop accents,
#: and "Maitrise du francais exigee" must trip the same rule as the accented
#: spelling.
_FLUENCY = (
    r"fluent|fluency|native|mother\s?tongue|proficien|advanced|excellent|"
    r"business\s+level|c1|c2|b2|required|mandatory|essential|must\s+have|"
    # German
    r"fliessend|fließend|verhandlungssicher|muttersprach|sehr\s+gute|"
    r"stilsicher|zwingend|erforderlich|voraussetzung|"
    # French
    r"courant|maîtrise|maitrise|exigée?|exigee?|obligatoire|"
    # Spanish / Portuguese / Italian / Dutch
    r"imprescindible|obligatorio|nativo|fluidez|"
    r"obrigatóri[oa]|obrigatori[oa]|fluente|"
    r"madrelingua|richiesto|"
    r"vereist|vloeiend|noodzakelijk"
)


def detect_language(*chunks: str) -> str:
    """Cheap language guess over the opening of a posting. Defaults to 'en'."""
    text = " " + " ".join(c for c in chunks if c).lower()[:4000] + " "
    scores = {
        code: sum(text.count(marker) for marker in markers)
        for code, markers in _LANGUAGE_MARKERS.items()
    }
    best = max(scores, key=lambda k: scores[k]) if scores else "en"
    return best if scores.get(best, 0) >= 3 else "en"


@lru_cache(maxsize=512)
def _requirement_pattern(code: str) -> re.Pattern | None:
    names = _LANGUAGE_NAMES.get(code)
    if not names:
        return None
    alternation = "|".join(re.escape(n) for n in names)
    # The language named within a few words of a fluency term, either order.
    return re.compile(
        rf"(?:{_FLUENCY})[^.;\n]{{0,40}}\b(?:{alternation})\b"
        rf"|\b(?:{alternation})\b[^.;\n]{{0,40}}(?:{_FLUENCY})",
        re.I,
    )


def requires_language(text: str, code: str) -> bool:
    """Does the posting demand working fluency in `code`?

    Mentioning a language is not requiring it: "English required, German a
    plus" must not count as a German requirement, which is why the fluency
    term has to appear close to the language name.
    """
    pattern = _requirement_pattern(code)
    if pattern is None or not text:
        return False
    if not pattern.search(text):
        return False
    # "a plus" / "nice to have" negates the requirement.
    names = "|".join(re.escape(n) for n in _LANGUAGE_NAMES[code])
    optional = re.compile(
        rf"\b(?:{names})\b[^.;\n]{{0,30}}"
        rf"(a\s+plus|nice\s+to\s+have|advantage|beneficial|desirable|"
        rf"von\s+vorteil|willkommen|optional)",
        re.I,
    )
    return not optional.search(text)


# --------------------------------------------------------------------------- #
# Seniority
# --------------------------------------------------------------------------- #

_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead(er)?|leiter|leitung|head(?:\s+of)?|"
    r"director|vp|vice\s+president|chief|c[te]o|manager|managing|"
    r"architect|expert|specialist\s+iii|iii|iv|distinguished|fellow|"
    # "Experienced Quantitative Strategist" at WorldQuant scored 146 as
    # seniority=unknown because none of the classic markers appear in it.
    r"experienced|advanced|seasoned|erfahrene[rn]?|"
    r"principal|team\s+lead|tech\s+lead|group\s+lead|"
    r"associate\s+director|executive\s+director|partner)\b",
    re.I,
)

#: Term-time side jobs. These carried the full junior bonus, which put a
#: "Werkstudent:in (Gen)AI Engineer" at 144.5 - above real graduate roles.
#: You want a position for after the MSc, so they get their own class.
_WORKING_STUDENT = re.compile(
    r"\b(werkstudent|working\s+student|studentische\s+hilfskraft|student\s+assistant|"
    r"studentenjob|teilzeit\s+student|student\s+worker)", re.I,
)

_JUNIOR_TITLE = re.compile(
    r"\b(junior|jr\.?|graduate|grad\s+(program|scheme)|new\s+grad|entry[\s-]level|"
    r"campus|early\s+career|trainee|associate\s+(researcher|scientist|engineer|trader)|"
    r"praktikant|einsteiger|absolvent)\b",
    re.I,
)
_INTERN = re.compile(r"\b(intern|internship|praktikum|stage|thesis|masterarbeit)\b", re.I)
_PHD_REQ = re.compile(
    r"(ph\.?d\.?|doctorate|doktorat)\s*(degree)?\s*(is\s*)?(required|mandatory|"
    r"essential|necessary)|require[sd]?\s+a\s+ph\.?d|must\s+have\s+a\s+ph\.?d",
    re.I,
)

# "3+ years", "at least 5 years", "mindestens 3 Jahre", "5-7 years of experience"
_YEARS = re.compile(
    r"(?:(\d{1,2})\s*[-–+]\s*(?:\d{1,2})?\s*|at\s+least\s+(\d{1,2})\s*|"
    r"minimum\s+(?:of\s+)?(\d{1,2})\s*|mindestens\s+(\d{1,2})\s*|(\d{1,2})\s*\+?\s*)"
    r"(?:years?|jahre?|yrs?)\s*(?:of\s+)?(?:relevant\s+|professional\s+|work\s+)?"
    r"(?:experience|erfahrung|berufserfahrung)",
    re.I,
)


def parse_min_years(text: str) -> int | None:
    """Smallest experience requirement stated anywhere in the posting."""
    if not text:
        return None
    found = []
    for match in _YEARS.finditer(text[:12000]):
        for group in match.groups():
            if group and group.isdigit():
                value = int(group)
                if 0 <= value <= 25:
                    found.append(value)
                break
    return min(found) if found else None


def classify_seniority(title: str, description: str = "", min_years: int | None = None) -> str:
    """Returns one of: working_student, intern, junior, mid, senior, unknown.

    Title wins over description: a posting called "Senior ..." is senior even if
    the body mentions a graduate programme elsewhere on the page.
    """
    # Checked before senior/junior: "Working Student Data Engineer (Senior
    # Team)" is a working-student role whichever other markers it carries.
    if _WORKING_STUDENT.search(title):
        return "working_student"
    if _INTERN.search(title):
        return "intern"
    if _SENIOR_TITLE.search(title):
        return "senior"
    if _JUNIOR_TITLE.search(title):
        return "junior"

    years = min_years if min_years is not None else parse_min_years(description)
    if years is not None:
        if years <= 1:
            return "junior"
        if years <= 3:
            return "mid"
        return "senior"

    if _JUNIOR_TITLE.search(description[:1500]):
        return "junior"
    return "unknown"


def requires_phd(text: str) -> bool:
    return bool(_PHD_REQ.search(text or ""))


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_RELATIVE = re.compile(
    r"(?:posted\s+)?(\d+)\+?\s*(day|week|month|hour|minute|stunde|tag|woche|monat)s?\s*(?:ago|vor)?",
    re.I,
)


def parse_date(value, *, fallback_now: bool = False) -> str | None:
    """Best-effort ISO-8601 UTC from whatever a source hands us.

    Handles epoch millis (Lever), ISO strings, 'Posted Today' / '3 days ago'
    (Workday), and plain dates.
    """
    if value is None or value == "":
        return datetime.now(timezone.utc).isoformat(timespec="seconds") if fallback_now else None

    if isinstance(value, (int, float)):
        # Lever and friends use epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    low = text.lower()
    now = datetime.now(timezone.utc)

    if "today" in low or "just posted" in low or "heute" in low:
        return now.isoformat(timespec="seconds")
    if "yesterday" in low or "gestern" in low:
        return (now - timedelta(days=1)).isoformat(timespec="seconds")

    match = _RELATIVE.search(low)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {
            "minute": timedelta(minutes=amount),
            "hour": timedelta(hours=amount), "stunde": timedelta(hours=amount),
            "day": timedelta(days=amount), "tag": timedelta(days=amount),
            "week": timedelta(weeks=amount), "woche": timedelta(weeks=amount),
            "month": timedelta(days=30 * amount), "monat": timedelta(days=30 * amount),
        }.get(unit)
        if delta is not None:
            return (now - delta).isoformat(timespec="seconds")

    try:
        parsed = dateparser.parse(text, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return now.isoformat(timespec="seconds") if fallback_now else None
    if parsed is None:
        return now.isoformat(timespec="seconds") if fallback_now else None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = dateparser.parse(iso)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
