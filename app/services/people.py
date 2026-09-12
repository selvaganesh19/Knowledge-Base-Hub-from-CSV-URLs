"""Deterministic person extraction: finding names and titles without an LLM.

The assignment is about people. Until now that data existed only if an LLM key was
configured, which means the single most important capability in the application
silently disappeared without one.

This module reads the same evidence an LLM would, using the structure that
leadership pages reliably have: a person's name followed closely by their job
title. It is deliberately conservative - a wrong name in a person card is worse
than a missing one, because it is presented as fact - so it requires the two to be
adjacent and the title to match a known vocabulary.

It runs alongside the LLM pass, not instead of it. The LLM reads prose this cannot
("Jane has led the company since 2019 as its chief executive"), and this reads
pages the LLM is not available for, or where the LLM returns nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Seniority words that make a nearby string a job title rather than a description.
#: Ordered longest-first at match time so "Chief Executive Officer" wins over
#: "Officer" and the captured title is the specific one.
TITLE_WORDS = (
    "chief executive officer",
    "chief financial officer",
    "chief technology officer",
    "chief operating officer",
    "chief marketing officer",
    "chief revenue officer",
    "chief people officer",
    "chief information officer",
    "chief strategy officer",
    "chief legal officer",
    "chief scientific officer",
    "chief medical officer",
    "chief data officer",
    "chief risk officer",
    "chief commercial officer",
    "chief business officer",
    "managing director",
    "executive chairman",
    "executive vice president",
    "senior vice president",
    "vice president",
    "executive director",
    "non-executive director",
    "general manager",
    "president",
    "chairman",
    "chairwoman",
    "chairperson",
    "co-founder",
    "cofounder",
    "founder",
    "ceo",
    "cfo",
    "cto",
    "coo",
    "cmo",
    "cro",
    "cio",
    "chro",
    "director",
    "treasurer",
    "secretary",
    "partner",
    "principal",
    "head of",
    "board member",
)

_TITLE_ALTERNATION = "|".join(re.escape(word) for word in TITLE_WORDS)

#: A job title as it appears on a page. A title can be chained to another with
#: "and", "&" or a comma - "President and CEO", "Executive Vice President and Chief
#: Accounting Officer" - so those continuations are consumed.
#:
#: Deliberately not extended to "of X". "Chief Financial Officer of Northwind
#: Robotics" would then capture the company as part of the role, which is a factual
#: error on a person card; and "Managing Director of Greater China" losing its area
#: is a smaller loss than misattributing an employer.
TITLE_RE = re.compile(
    rf"\b(?:{_TITLE_ALTERNATION})\b"
    rf"(?:\s*(?:,|and|&)\s*(?:{_TITLE_ALTERNATION})\b)*",
    re.IGNORECASE,
)

#: "Chief Security Officer" and similar are real titles that the fixed vocabulary
#: cannot enumerate - there are too many, and new ones appear. This catches the
#: shape instead of the word.
CHIEF_ROLE_RE = re.compile(r"\bchief\s+[a-z]{3,20}\s+officer\b", re.IGNORECASE)

#: UI verbs that follow a title on a card ("... Read Jane's bio"). Everything from
#: one of these onwards is interface text, not part of the role.
UI_TAIL_RE = re.compile(
    r"\b(?:read|view|learn|more|see|show|meet|explore|discover|contact|follow|connect|bio)\b.*$",
    re.IGNORECASE,
)

#: A plausible human name. Two to four capitalised words, each starting with a
#: letter, allowing the particles and suffixes real names carry.
NAME_RE = re.compile(
    r"\b([A-Z][a-zà-öø-ÿ'’\-]{1,20}"
    r"(?:\s+(?:[A-Z][a-zà-öø-ÿ'’\-]{1,20}|van|von|de|del|della|der|di|da|la|le|bin|al|st\.?))"
    r"{0,3})\b"
)

#: Words that end a name when they follow it. "Ron Sugar Former CEO" captures the
#: label as a third name word, which produces a person called "Ron Sugar Former" -
#: a wrong record presented as fact, which is worse than no record.
#:
#: The second group is organisational nouns. A card reading "Gary Miller Customer
#: Success" is a person and their department; without these, "Customer" and
#: "Success" are absorbed into the name. Only words implausible as surnames belong
#: here, since the rule applies to every word after the first.
NAME_TERMINATORS = frozenset(
    [
        "former",
        "formerly",
        "current",
        "currently",
        "previously",
        "interim",
        "acting",
        "outgoing",
        "chief",
        "executive",
        "officer",
        "senior",
        "managing",
        "global",
        "regional",
        "president",
        "vice",
        "chairman",
        "chairwoman",
        "chairperson",
        "director",
        "founder",
        "cofounder",
        "partner",
        "principal",
        "treasurer",
        "secretary",
        "head",
        "board",
        "member",
        "lead",
        "independent",
        "group",
        "division",
        "operations",
        "strategy",
        "marketing",
        "finance",
        "technology",
        "legal",
        "customer",
        "success",
        "sales",
        "engineering",
        "product",
        "design",
        "talent",
        "communications",
        "research",
        "development",
        "platform",
        "solutions",
        "accounting",
        "services",
        "support",
        "department",
        "office",
        "team",
        "staff",
        "business",
        "commercial",
        "digital",
        "innovation",
        "and",
        "of",
        "the",
        "at",
        "for",
        "with",
    ]
)

#: Words that start a line with a capital but are never a person's name. Without
#: this, "Our Leadership Team" and "Read More" become people.
NOT_NAMES = frozenset(
    [
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "and",
        "another",
        "any",
        "are",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "company",
        "contact",
        "cookie",
        "copyright",
        "could",
        "covid",
        "directors",
        "does",
        "download",
        "each",
        "email",
        "employee",
        "enable",
        "executive",
        "executives",
        "find",
        "first",
        "follow",
        "footer",
        "for",
        "founder",
        "founders",
        "from",
        "get",
        "global",
        "have",
        "head",
        "help",
        "here",
        "home",
        "how",
        "human",
        "information",
        "investors",
        "join",
        "leadership",
        "learn",
        "link",
        "linkedin",
        "list",
        "login",
        "management",
        "manager",
        "marketing",
        "meet",
        "menu",
        "more",
        "most",
        "name",
        "news",
        "next",
        "none",
        "officers",
        "online",
        "our",
        "page",
        "people",
        "please",
        "policy",
        "president",
        "privacy",
        "products",
        "profile",
        "read",
        "registered",
        "resources",
        "results",
        "rights",
        "search",
        "see",
        "senior",
        "service",
        "services",
        "share",
        "should",
        "sign",
        "since",
        "site",
        "social",
        "solutions",
        "staff",
        "story",
        "subscribe",
        "support",
        "team",
        "terms",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "title",
        "together",
        "top",
        "twitter",
        "update",
        "us",
        "use",
        "user",
        "using",
        "view",
        "website",
        "welcome",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "with",
        "work",
        "world",
        "you",
        "your",
        "youtube",
    ]
)

#: Line prefixes that mark a section as person-bearing. Used to weight where to
#: look, not as a hard filter - a leadership page often has no such heading.
LEADERSHIP_HINTS = (
    "leadership",
    "executive",
    "executives",
    "management",
    "board",
    "directors",
    "team",
    "people",
    "founders",
    "about",
    "biography",
    "our team",
    "who we are",
    "officers",
)

#: How close a title must sit to a name to be paired with it, in lines.
MAX_LINE_DISTANCE = 3


@dataclass
class ExtractedPerson:
    """One person found by pattern rather than by model."""

    name: str
    title: str = ""
    company: str = ""
    location: str = ""
    bio: str = ""
    confidence: float = 0.5
    evidence: str = ""
    source_lines: tuple[int, int] = (0, 0)
    field_origin: dict = field(default_factory=dict)

    def as_record(self) -> dict:
        """The same shape the LLM pass produces, so both feed one storage path."""
        return {
            "name": self.name,
            "title": self.title,
            "company": self.company,
            "bio": self.bio,
            "email": "",
            "phone": "",
            "linkedin_url": "",
            "location": self.location,
            "confidence": self.confidence,
        }


def is_person_name(candidate: str) -> bool:
    """Whether a string reads as a human name rather than a heading or a button."""
    words = candidate.split()
    if not (2 <= len(words) <= 4):
        return False

    # A name is capitalised throughout. "Our Leadership" fails on the second word
    # being in the stop list; "Read More" fails for the same reason.
    for word in words:
        if not word[:1].isupper():
            return False

    cleaned = [re.sub(r"[^a-z]", "", word.lower()) for word in words]
    if any(word in NOT_NAMES for word in cleaned):
        return False

    # Every word being a single letter or a known title word means this is a
    # heading, not a person.
    return not all(word in NOT_NAMES or len(word) <= 1 for word in cleaned)


def find_pairs(text: str) -> list[ExtractedPerson]:
    """Pair each name with the nearest job title within a few lines.

    Adjacency is the whole signal. A name and a title three lines apart on a
    leadership page are almost always the same person; a name and a title on
    opposite sides of the page are not, which is why the distance is bounded.

    Every occurrence is returned, including repeats of the same person. A page that
    lists someone twice - once as "CFO" on a card and once as "Chief Financial
    Officer" in a biography - should not lose the fuller title to whichever came
    first. Collapsing those is `extract_people`'s job, because it is the function
    that knows to prefer the longer title.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    people: list[ExtractedPerson] = []

    for index, line in enumerate(lines):
        # Long lines are prose, and prose is where a biography states a name and a
        # role in one sentence ("Dr. Elena Vasquez serves as Chief Executive
        # Officer of ..."). They are not skipped - only absurdly long ones, which
        # are extraction artefacts rather than sentences.
        if not line or len(line) > 600:
            continue

        person = _first_person_on_line(lines, index, line)
        if person is not None:
            people.append(person)

    return people


def _first_person_on_line(lines: list[str], index: int, line: str) -> ExtractedPerson | None:
    """The first name on a line that is both plausible and has a title near it.

    Every candidate is tried, not just the first. A line beginning "Dr. Elena
    Vasquez serves as ..." yields "Dr" as its first match - a single word, so not a
    name - and stopping there would lose the real name behind it.

    Exactly one person is returned per line, and it is the earliest *qualifying*
    candidate. Returning every match would let a trailing company name ("Ron Sugar
    ... Northrop Grumman") qualify as a person using the title of the person before
    it.
    """
    for name_match in NAME_RE.finditer(line):
        name = _clean_name(name_match.group(1))
        if not is_person_name(name):
            continue

        title, distance = _nearest_title(lines, index, name)
        if not title:
            continue

        start = min(index, index + distance)
        end = max(index, index + distance)
        return ExtractedPerson(
            name=name,
            title=title,
            evidence=" | ".join(lines[start : end + 1])[:300],
            source_lines=(start, end),
            # A name with its title on the very next line is about as certain as
            # pattern matching gets; three lines away is a guess.
            confidence=0.8 if abs(distance) <= 1 else 0.6,
        )

    return None


def _nearest_title(lines: list[str], name_index: int, name: str = "") -> tuple[str, int]:
    """The closest job title to a name.

    The name's own line is checked first - after it, then before it - for the prose
    forms where the two sit in one sentence:

        "Dr. Elena Vasquez serves as Chief Executive Officer of ..."
        "The Chief Technology Officer is Samuel Okonkwo, who ..."

    Only then are neighbouring lines searched, forward before backward. Reading the
    name's own line first is what keeps a card layout from pairing one person with
    the next card's title.
    """
    if name:
        same_line = _title_after_name(lines[name_index], name)
        if same_line:
            return same_line, 0
        preceding = _title_before_name(lines[name_index], name)
        if preceding:
            return preceding, 0

    for distance in range(1, MAX_LINE_DISTANCE + 1):
        for direction in (1, -1):
            index = name_index + direction * distance
            if not (0 <= index < len(lines)):
                continue
            title = _title_in(lines[index])
            if title:
                return title, direction * distance
    return "", 0


def _title_before_name(line: str, name: str) -> str:
    """A title stated earlier on the same line as the name.

    Covers the sentence form that leads with the role: "The Chief Technology
    Officer is Samuel Okonkwo". Only reached when nothing after the name matched,
    so a card layout - where the title follows its own person's name - is unaffected.
    """
    position = line.find(name)
    if position <= 0:
        return ""

    head = line[:position].strip()
    # "The Chief Technology Officer is " - the linking verb is not part of the role.
    head = re.sub(r"\b(?:is|was|and|,|—|–|-)\s*$", "", head, flags=re.IGNORECASE).strip()
    if not head:
        return ""
    return _title_in(head)


def _title_after_name(line: str, name: str) -> str:
    """A title appearing later on the same line as the name.

    Only the text after the name is searched, and only as far as the end of that
    sentence. Searching the whole line would let a title belonging to the previous
    person match this one; running past the full stop would let it pull the title
    out of the *next* person's sentence.
    """
    position = line.find(name)
    if position < 0:
        return ""

    tail = line[position + len(name) :]
    sentence_end = re.search(r"\.\s|\.$", tail)
    if sentence_end:
        tail = tail[: sentence_end.start() + 1]

    # "Jane Doe is the chief executive officer" - the verb between them, and the
    # article after it, are not part of the title.
    tail = re.sub(
        r"^\s*(?:(?:is|was|serves as|served as|as)\s+(?:the\s+)?|[,;—–-]\s*)?",
        "",
        tail,
        flags=re.IGNORECASE,
    )
    return _title_in(tail)


def _title_in(line: str) -> str:
    """Extract a job title from a line, if it holds one and is not a paragraph."""
    candidate = (line or "").strip().lstrip("#-*• ").strip()
    # Generous, because a sentence-bounded tail from _title_after_name is legitimately
    # long ("Chief Financial Officer of Northwind Robotics"). What stops a paragraph
    # matching is that the extracted title itself is length-capped in _clean_title,
    # not that the line was short.
    if not candidate or len(candidate) > 220:
        return ""

    match = TITLE_RE.search(candidate)
    generic = CHIEF_ROLE_RE.search(candidate)
    if match and generic:
        # Whichever starts earlier is where the title begins.
        match = match if match.start() <= generic.start() else generic
    elif generic:
        match = generic
    if not match:
        return ""

    title = _clean_title(match.group(0))
    if not title:
        return ""
    # "Our leadership team" contains "leadership" but is a heading, not a role.
    if title.lower() in {"director", "partner", "principal", "president", "founder"} and (
        candidate.lower().startswith(("our ", "the "))
    ):
        return ""
    return title


def _clean_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip(" .,;:-–—")
    # Card layouts put a link after the title ("... Read Jane's bio"); everything
    # from that verb on is interface text.
    cleaned = UI_TAIL_RE.sub("", cleaned).strip(" .,;:-–—")
    if not cleaned or len(cleaned) > 90:
        return ""

    # Preserve the acronyms people actually search for, and title-case the rest.
    words = []
    for word in cleaned.split():
        lowered = word.lower()
        if lowered in {"ceo", "cfo", "cto", "coo", "cmo", "cro", "cio", "chro"}:
            words.append(lowered.upper())
        elif word.isupper() and len(word) > 1:
            words.append(word)
        else:
            words.append(
                word.lower() if lowered in {"of", "and", "the", "for"} else word.capitalize()
            )
    return " ".join(words)[:200]


def _clean_name(name: str) -> str:
    """Normalise a captured name and cut it at the first non-name word.

    The regex is greedy: on "Ron Sugar Former CEO and Chairman" it captures three
    capitalised words and returns "Ron Sugar Former". Truncating at the first role
    or function word turns that back into "Ron Sugar".
    """
    words = re.sub(r"\s+", " ", (name or "").strip()).split()

    kept: list[str] = []
    for word in words:
        stripped = re.sub(r"[^a-z]", "", word.lower())
        # The first word was already validated as a name; anything after it that
        # reads as a role, a label or a function word ends the name.
        if kept and (stripped in NAME_TERMINATORS or stripped in NOT_NAMES):
            break
        kept.append(word)

    return " ".join(kept)[:200]


#: Word-boundary matcher for the leadership hints, built once. Hints are matched
#: against headings and URLs only, never running prose.
_LEADERSHIP_HINT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(hint) for hint in LEADERSHIP_HINTS) + r")\b"
)


def is_leadership_page(text: str, url: str = "") -> bool:
    """Whether a page looks like it is about people at all.

    Only the URL path and the page's own headings are searched, not the body. The
    hint words are far too common in prose - "a page about shipping rates" contains
    "about" - so scanning running text would answer yes for almost every page and
    make the check worthless.

    This decides whether the extra extraction pass is worth running. It is not on
    the correctness path: `extract_people` runs on any page regardless.
    """
    haystack = url.lower()
    for line in (text or "").splitlines()[:60]:
        stripped = line.strip()
        if stripped.startswith("#"):
            haystack += " " + stripped.lower()

    return bool(_LEADERSHIP_HINT_RE.search(haystack))


def extract_people(text: str, url: str = "", company: str = "") -> list[ExtractedPerson]:
    """Find every plausible person on a page, best-evidenced first."""
    people = find_pairs(text)

    if company:
        for person in people:
            person.company = company
    else:
        inferred = infer_company(text, url)
        for person in people:
            person.company = inferred

    # Deduplicate on name alone: the same person listed twice on one page with two
    # slightly different titles is one person, and the version with the fuller
    # title is the one worth keeping.
    best: dict[str, ExtractedPerson] = {}
    for person in people:
        key = person.name.lower()
        existing = best.get(key)
        if existing is None or len(person.title) > len(existing.title):
            best[key] = person

    ordered = sorted(
        best.values(),
        key=lambda person: (-person.confidence, -len(person.title), person.name),
    )
    return ordered


def infer_company(text: str, url: str = "") -> str:
    """Best-effort company name from the page, never invented.

    Pages state their company in the title tag far more often than in the body, and
    a wrong company on a person card is a factual claim, so this returns "" rather
    than guessing when nothing is explicit.
    """
    head = (text or "")[:500]

    # "Acme Corp | Leadership" / "Leadership - Acme Corp" / "Acme Corp: Our Team"
    match = re.search(
        r"^\s*(?:about\s+)?([A-Z][\w&.'\- ]{2,40}?)\s*[|–—:]\s*"
        r"(?:leadership|management|executive|team|about|our team|board|directors)",
        head,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return match.group(1).strip()[:200]

    # "Leadership | Acme Corp" / "Our Team - Acme Corp"
    match = re.search(
        r"^\s*(?:leadership|management|executive team|our team|about)\s*[|–—:]\s*"
        r"([A-Z][\w&.'\- ]{2,40})\s*$",
        head,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return match.group(1).strip()[:200]

    # "About Northwind Robotics" - no separator at all.
    match = re.search(
        r"^\s*about\s+([A-Z][\w&.'\- ]{2,40})\s*$", head, re.MULTILINE | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()[:200]

    if url:
        from app.services.url_guard import domain_of

        host = domain_of(url)
        # A bare IP is not a company name, and "127" on a person card is nonsense.
        if host and not re.fullmatch(r"[\d.]+", host):
            # "apple.com" -> "Apple". Only the first label: "www.example.co.uk"
            # would otherwise read as "Www".
            label = host.split(".")[0]
            if label and label not in {"www", "localhost"}:
                return label.capitalize()[:200]
    return ""
