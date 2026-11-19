"""Turn messy YouTube upload titles into clean movie titles plus hints.

GrjngoWesternMovies titles come in a few shapes:
    Title | 4K | Actor | Western | English
    Western Movie | Title | COLORIZED | ...
    WESTERN MOVIE: Title [Full Length] - ENGLISH
    Title (Western Movie, Full Length, English) *free full westerns*
    Western Movie - BURT LANCASTER: Title (Free, Full Length)
The first non-tag segment is the title; everything else is a hint.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Words that only ever appear in the channel's marketing tags.
TAG_WORDS = {
    "western", "westerns", "movie", "movies", "film", "films", "full", "length",
    "free", "cowboy", "cowboys", "cowboyfilm", "classic", "old", "english",
    "colorized", "4k", "8k", "uhd-2", "uhd", "hd", "1080p", "action", "comedy",
    "drama", "romance", "adventure", "spaghetti", "italian", "italo", "american",
    "wild", "west", "ranch", "entire", "feature", "rare", "cult", "best", "on",
    "youtube", "in", "for", "and", "&", "indians", "indian", "family", "a",
    "modern", "the", "watchfree", "funny", "epic", "original", "black", "white",
    "starring", "outlaws", "revenge", "watch", "to", "cool", "thriller", "mystery",
}
TRAILING_TAGS = {"english", "hd", "4k", "colorized", "full", "movie", "western", "thriller"}
# Phrases that mark a parenthetical / bracket / trailing chunk as junk.
JUNK_HINTS = re.compile(
    r"western|movie|film|full|free|english|colorized|spaghetti|cowboy|"
    r"\bhd\b|\b4k\b|youtube|length|starring|classic|blaxploitation",
    re.I,
)
EXTRA_HINTS = re.compile(
    r"\btrailer\b|photo gallery|deleted scenes|soundtrack|#shorts|"
    r"announcement|festival|short (western )?film|western short film",
    re.I,
)
YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-4][0-9])(?!\d)")

MIN_FEATURE_SECONDS = 40 * 60  # B-westerns run ~45-55 min; trailers/shorts are far shorter


@dataclass
class ParsedTitle:
    raw: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    year_hint: int | None = None
    colorized: bool = False
    claimed_res: int | None = None  # what the title *claims*; verify with real formats
    is_extra: bool = False
    extra_reason: str = ""


def _nfkc(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _words(s: str) -> list[str]:
    toks = re.split(r"[\s,/]+", s.lower().strip(" -:|*\"'()[]"))
    return [w for w in toks if any(c.isalnum() for c in w)]


def is_tag(segment: str) -> bool:
    words = _words(segment)
    return bool(words) and all(w.strip(".!") in TAG_WORDS for w in words)


def _smart_case(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        small = {"a", "an", "the", "of", "and", "or", "in", "on", "at", "to", "for"}
        out = []
        for i, w in enumerate(s.lower().split()):
            out.append(w if (i and w in small) else w[:1].upper() + w[1:])
        return " ".join(out)
    return s


def _is_mostly_upper(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def clean_title(raw: str, duration: int | None = None) -> ParsedTitle:
    s = _nfkc(raw).strip()
    low = s.lower()

    p = ParsedTitle(raw=raw, title="")
    p.colorized = "coloriz" in low
    if re.search(r"\b8k\b", low):
        p.claimed_res = 4320
    elif re.search(r"\b4k\b", low):
        p.claimed_res = 2160
    elif re.search(r"\b1080p\b|\bhd\b", low):
        p.claimed_res = 1080
    years = YEAR_RE.findall(s)
    if years:
        p.year_hint = int(years[0])

    m = EXTRA_HINTS.search(s)
    if m:
        p.is_extra, p.extra_reason = True, m.group(0).lower()
    elif duration is not None and duration < MIN_FEATURE_SECONDS:
        p.is_extra, p.extra_reason = True, f"short ({duration // 60} min)"

    s = re.sub(r"#\w+", "", s)
    # Trailing marketing after the last bracket: "*free full westerns*", "free youtube movies"
    s = re.sub(r"\*[^*]*\*\s*$", "", s).strip()
    s = re.sub(r"\)\s+[a-z ,]*(free|movies?|watchfree|cowboyfilm)[a-z ,]*$", ")", s, flags=re.I).strip()
    s = re.sub(r"\]\s*-\s*[A-Z ]+(-.*)?$", "]", s).strip()  # "] - ENGLISH - Full Movies"

    # 1) Pipe segments: take the first one that isn't pure tags.
    segments = [x.strip() for x in s.split("|") if x.strip()]
    title = next((seg for seg in segments if not is_tag(seg)), segments[0] if segments else s)

    # 2) Brackets / parentheses: junk is dropped, "aka X" and short alt names kept as alternates.
    def handle_group(m: re.Match) -> str:
        inner = m.group(1).strip()
        aka = re.match(r"(?i)aka\s+(.+)", inner)
        if aka:
            p.alt_titles.append(_smart_case(aka.group(1).strip()))
            return ""
        if YEAR_RE.fullmatch(inner):
            return ""
        if JUNK_HINTS.search(inner) or is_tag(inner):
            return ""
        p.alt_titles.append(_smart_case(inner))  # e.g. "Matalo! (Kill Him)", "(Eye For An Eye)"
        return ""

    title = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", handle_group, title)
    title = re.sub(r"[\(\[].*$", "", title)  # unbalanced leftovers

    # 3) Prefix/suffix separators: "WESTERN MOVIE: X", "X: WESTERN MOVIE", "Gunfighters - Western Movie, ..."
    title = re.sub(r"\s{2,}", " ", title).strip()
    for sep in (":", " - "):
        if sep not in title:
            continue
        left, right = (t.strip() for t in title.split(sep, 1))
        if not is_tag(left) and not JUNK_HINTS.search(right):
            p.alt_titles.extend([left, right] if sep == " - " else [left])
        right_main = right.split(",")[0].strip()
        if is_tag(left) and right:
            title = right
        elif (is_tag(right_main) or JUNK_HINTS.search(right_main) and len(_words(right_main)) <= 4) and left:
            title = left
        elif sep == ":" and _is_mostly_upper(left) and not _is_mostly_upper(right) and len(_words(left)) <= 3:
            title = right  # "BURT LANCASTER: Vengeance Valley"
    # Repeat the colon pass once ("Western Movie - BURT LANCASTER: Vengeance Valley")
    if ":" in title:
        left, right = (t.strip() for t in title.split(":", 1))
        if _is_mostly_upper(left) and not _is_mostly_upper(right) and len(_words(left)) <= 3:
            title = right

    # "Burt Lancaster in VENGEANCE VALLEY"
    m = re.match(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)+ in ([A-Z][A-Z0-9 '\-!,.]+)$", title.strip())
    if m:
        title = m.group(1)

    # "MAN FROM NOWHERE aka ARIZONA COLT"
    aka = re.split(r"(?i)\s+aka\s+", title)
    if len(aka) > 1:
        title = aka[0]
        p.alt_titles.extend(_smart_case(a.strip()) for a in aka[1:])

    # Leading actor-tag segments like "Western Movie - BURT LANCASTER" leave nothing useful; fall back.
    title = re.sub(r"\s{2,}", " ", title).strip(" -:,|\"'")
    words = title.split()
    while len(words) > 1 and words[-1].lower().strip(".!") in TRAILING_TAGS:
        words.pop()
    title = " ".join(words)
    if not title and segments:
        title = segments[0]
    p.title = _smart_case(title)
    p.alt_titles = [a for a in dict.fromkeys(p.alt_titles) if a and a.lower() != p.title.lower()]
    return p


def match_key(title: str) -> str:
    """Aggressive normalization for comparing titles to each other."""
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("$", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


_PERSON_STOP = TAG_WORDS - {"the"} | {"award", "winning", "free", "full", "duo", "legends", "documentary",
                                     "subs", "spanish", "mexican", "australia", "gunslinger", "outlaw",
                                     "bounty", "hunter", "treasure", "mountains", "war", "civil", "kung", "fu",
                                     "series", "saga", "trailer", "finale", "texas", "ranger", "gold", "robbery"}


def extract_people(raw: str, title: str = "", alts: list[str] | None = None) -> list[str]:
    """Pull probable actor/character names out of the marketing tags.

    "Arizona Colt | 4K | Giuliano Gemma | Fernando Sancho" -> ["Giuliano Gemma", "Fernando Sancho"]
    "Yuma (Western Movie starring Clint Walker, ...)"      -> ["Clint Walker"]
    """
    s = _nfkc(raw)
    s = re.sub(r"(?i)\bstarring\b", "|", s)
    s = re.sub(r"(?i)^(.*?)\bin\s+(?=[A-Z]{3,})", r"\1|", s)  # "Burt Lancaster in VENGEANCE VALLEY"
    parts = re.split(r"\s*(?:\||\(|\)|\[|\]|,|:|\s-\s|&|\*|\bwith\b)\s*", s)
    skip = {match_key(x) for x in [title, *(alts or [])] if x}
    out = []
    for part in parts:
        part = part.strip(" .\"'")
        words = part.split()
        if not 2 <= len(words) <= 4:
            continue
        if any(not re.fullmatch(r"[A-Z][\w.'\-]*", w) for w in words):
            continue
        if any(w.lower().strip(".") in _PERSON_STOP for w in words):
            continue
        if match_key(part) in skip:
            continue
        out.append(_smart_case(part))
    return list(dict.fromkeys(out))
