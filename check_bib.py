#!/usr/bin/env python3
"""
check_bib.py -- Verify .bib entries against authoritative online sources, and
flag missing or inconsistent information.

For each entry the script picks a lookup based on what identifiers it has:

  * Normal DOI            -> Crossref (structured JSON: separate given/family
                             names, and both the full journal name and its ISO
                             abbreviation, which makes checking abbreviated
                             authors and journals reliable).
  * arXiv preprint        -> the arXiv API. Recognised from an arXiv DOI
    (10.48550/arXiv...)      (10.48550/arXiv...), an `eprint` field, or an
                             arXiv id in the `url`/`journal` field. Also checks
                             whether the preprint has since been published
                             (arXiv's journal_ref / linked DOI, or a Crossref
                             title search) and can construct its arXiv DOI.
  * Book with an ISBN     -> Open Library, then Google Books (no API key). Used
                             for books that have no DOI.
  * No DOI/ISBN           -> the lookup is chosen by entry type:
                             - articles/proceedings papers: Crossref title+
                               author search; a close title match reports the
                               DOI and, with --out, writes it back;
                             - books (@book/@inbook/@incollection/...): a book
                               catalog search (Open Library, then Google Books)
                               to recover the ISBN. Crossref's book coverage is
                               spotty, so catalogs are used instead. Book hits
                               are REPORTED for you to confirm (not auto-
                               trusted), matched on title+author, and lenient
                               about edition/year; --out writes the ISBN back.

Field comparison is deliberately tolerant of harmless variation:
  - abbreviated author given names ("A." matches "Axel"), full family names;
  - journal abbreviations ("BIT Numer. Math." vs "BIT Numerical Mathematics");
  - combined journal issues ("7" matches a "7-8" double issue);
  - online-first papers (prefers the print/volume year, accepts either);
  - corrupted source metadata (a U+FFFD where an umlaut was lost on ingest is
    treated as a near-match, not a title mismatch).

It also checks each entry for the fields BibTeX REQUIRES for its type
(@article needs journal; @book needs publisher; @inproceedings needs
booktitle; etc.), independently of the online lookup.

Usage:
    python check_bib.py references.bib --mailto you@example.org
    python check_bib.py references.bib --mailto you@example.org --out fixed.bib
    python check_bib.py references.bib --suggest   # also suggest missing
                                                   # volume/issue/pages/publisher

Dependencies:
    pip install bibtexparser pylatexenc requests
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests
import bibtexparser
from pylatexenc.latex2text import LatexNodes2Text

_LATEX = LatexNodes2Text()

CROSSREF_WORK = "https://api.crossref.org/works/{doi}"
CROSSREF_SEARCH = "https://api.crossref.org/works"
ARXIV_API = "https://export.arxiv.org/api/query"

# How similar two titles must be (0..1) before we trust an auto-discovered DOI.
TITLE_ACCEPT = 0.90


# --------------------------------------------------------------------------
# 1. Data structures
# --------------------------------------------------------------------------

@dataclass
class Reference:
    """Normalised, source-agnostic view of a single work."""
    key: str = ""
    entrytype: str = "article"
    title: str = ""
    authors: list[tuple[str, str]] = field(default_factory=list)  # (family, given)
    journal: str = ""
    year: str = ""                   # preferred/displayed year (print if available)
    year_candidates: list[str] = field(default_factory=list)  # all plausible years
    volume: str = ""
    number: str = ""
    pages: str = ""
    article_number: str = ""
    doi: str = ""
    journal_alt: str = ""            # crossref short-container-title
    publisher: str = ""              # mainly for books
    isbn: str = ""                   # recovered for books found by title search
    raw: dict = field(default_factory=dict)


@dataclass
class ArxivRecord:
    """What the arXiv API tells us about a preprint."""
    reference: Reference
    published_doi: str = ""          # DOI of the published version, if linked
    journal_ref: str = ""            # free-text journal reference, if published


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def clean_latex(s: str) -> str:
    if not s:
        return ""
    try:
        s = _LATEX.latex_to_text(s)
    except Exception:
        pass
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


def norm(s: str) -> str:
    s = clean_latex(s)
    s = strip_accents(s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in norm(s).split(" ") if t]


def initials(given: str) -> list[str]:
    parts = re.split(r"[\s.\-]+", clean_latex(given))
    return [strip_accents(p)[0].lower() for p in parts if p]


def title_score(a: str, b: str) -> float:
    """0..1 similarity of two titles after normalisation."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# --------------------------------------------------------------------------
# 2. Parsing the .bib file
# --------------------------------------------------------------------------

def parse_bib_authors(field_value: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\s+and\s+", field_value.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:
            family, _, given = chunk.partition(",")
        else:
            bits = chunk.split()
            family, given = bits[-1], " ".join(bits[:-1])
        out.append((clean_latex(family).strip(), clean_latex(given).strip()))
    return out


def parse_bib(path: str) -> list[Reference]:
    parser = bibtexparser.bparser.BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    with open(path, encoding="utf-8") as fh:
        db = bibtexparser.load(fh, parser=parser)

    refs: list[Reference] = []
    for e in db.entries:
        refs.append(Reference(
            key=e.get("ID", ""),
            entrytype=e.get("ENTRYTYPE", "article"),
            title=clean_latex(e.get("title", "")),
            authors=parse_bib_authors(e.get("author", "")),
            journal=clean_latex(e.get("journal", "")),
            year=e.get("year", "").strip(),
            volume=e.get("volume", "").strip(),
            number=e.get("number", "").strip(),
            pages=e.get("pages", "").strip(),
            article_number=e.get("eid", "").strip() or e.get("article-number", "").strip(),
            doi=e.get("doi", "").strip(),
            publisher=e.get("publisher", "").strip(),
            raw=e,
        ))
    return refs


# --------------------------------------------------------------------------
# 3. Crossref lookups (single DOI + bibliographic search)
# --------------------------------------------------------------------------

def _ua(mailto: str | None) -> dict:
    ua = "check_bib/2.0"
    if mailto:
        ua += f" (mailto:{mailto})"
    return {"User-Agent": ua}


def crossref_to_reference(msg: dict) -> Reference:
    authors = []
    for a in msg.get("author", []):
        family = a.get("family", "").strip()
        given = a.get("given", "").strip()
        if family or given:
            authors.append((family, given))

    def first(lst):
        return lst[0] if isinstance(lst, list) and lst else ""

    # A work published online in one year but assigned to a print volume in the
    # next is common. Citations (and .bib files) almost always use the PRINT /
    # volume year, so prefer published-print for the displayed year. But keep
    # every date Crossref reports as an acceptable match, because some databases
    # do cite the online year -- that ambiguity shouldn't be flagged as an error.
    # (Deliberately ignore `created`/`deposited`/`indexed`: those are metadata
    # registration dates, not publication dates.)
    year = ""
    year_candidates = []
    for datekey in ("published-print", "published-online", "issued", "published"):
        dp = msg.get(datekey, {}).get("date-parts", [[]])
        if dp and dp[0] and dp[0][0]:
            y = str(dp[0][0])
            if not year:
                year = y                 # first in preference order = displayed
            if y not in year_candidates:
                year_candidates.append(y)

    return Reference(
        title=clean_latex(first(msg.get("title", []))),
        authors=authors,
        journal=first(msg.get("container-title", [])),
        journal_alt=first(msg.get("short-container-title", [])),
        year=year,
        year_candidates=year_candidates,
        volume=str(msg.get("volume", "")).strip(),
        number=str(msg.get("issue", "")).strip(),
        pages=str(msg.get("page", "")).strip(),
        article_number=str(msg.get("article-number", "")).strip(),
        doi=str(msg.get("DOI", "")).strip(),
        publisher=str(msg.get("publisher", "")).strip(),
        raw=msg,
    )


def fetch_crossref(doi, mailto=None, session=None):
    sess = session or requests.Session()
    try:
        r = sess.get(CROSSREF_WORK.format(doi=requests.utils.quote(doi)),
                     headers=_ua(mailto), timeout=30)
    except requests.RequestException as exc:
        print(f"  ! network error looking up {doi} at {CROSSREF_WORK}: {exc}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} for {doi} at {CROSSREF_WORK}", file=sys.stderr)
        return None
    return crossref_to_reference(r.json().get("message", {}))


def search_crossref(title, authors, mailto=None, session=None, rows=5):
    """Bibliographic search -> list of candidate References (best first)."""
    sess = session or requests.Session()
    params = {"rows": rows}
    if title:
        params["query.bibliographic"] = title
    if authors:
        params["query.author"] = " ".join(f or g for f, g in authors)
    try:
        r = sess.get(CROSSREF_SEARCH, params=params, headers=_ua(mailto), timeout=30)
    except requests.RequestException as exc:
        print(f"  ! network error during search: {exc}", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} at {CROSSREF_SEARCH}", file=sys.stderr)
        return []
    items = r.json().get("message", {}).get("items", [])
    return [crossref_to_reference(it) for it in items]


def author_family_overlap(a: Reference, b: Reference) -> bool:
    fa = {norm(f) for f, _ in a.authors if f}
    fb = {norm(f) for f, _ in b.authors if f}
    return bool(fa & fb) if (fa and fb) else True


def best_candidate(local: Reference, candidates: list[Reference]):
    """Pick the candidate whose title best matches; return (ref, score)."""
    best, best_s = None, 0.0
    for c in candidates:
        s = title_score(local.title, c.title)
        if s > best_s:
            best, best_s = c, s
    return best, best_s


# --------------------------------------------------------------------------
# 3b. arXiv lookup
# --------------------------------------------------------------------------

_ARXIV_NEW = r"\d{4}\.\d{4,5}"
_ARXIV_OLD = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARX = "{http://arxiv.org/schemas/atom}"


def is_arxiv_doi(doi: str) -> bool:
    return "arxiv" in (doi or "").lower()


def extract_arxiv_id(*fields) -> str | None:
    """Pull an arXiv id from a DOI ('10.48550/arXiv.2201.12345'),
    an eprint field, or a URL. Returns e.g. '2201.12345' or 'math/0309136'."""
    for f in fields:
        if not f:
            continue
        m = re.search(rf"({_ARXIV_OLD}|{_ARXIV_NEW})(v\d+)?", f, re.I)
        if m:
            return m.group(1)
    return None


def arxiv_doi_from_id(arxiv_id: str) -> str:
    """Registered arXiv DOI for an id:
    '2201.12345' -> '10.48550/arXiv.2201.12345'."""
    return f"10.48550/arXiv.{arxiv_id}"


def detect_arxiv_id(local) -> str | None:
    """Find an arXiv id for an entry even when it has no `doi` field. Checks,
    in order: an arXiv DOI, the `eprint` field, and any field that *mentions*
    arXiv (`url`, `journal`, `note`, `howpublished`) -- e.g. a URL like
    'https://arxiv.org/abs/2201.12345' or a journal like
    'arXiv preprint arXiv:2201.12345'. The "arxiv" guard on the free-text
    fields avoids mistaking an unrelated number (a volume, a year) for an id."""
    if is_arxiv_doi(local.doi):
        return extract_arxiv_id(local.doi)
    eid = extract_arxiv_id(local.raw.get("eprint", ""))
    if eid:
        return eid
    for fld in ("url", "journal", "note", "howpublished"):
        val = local.raw.get(fld, "")
        if val and "arxiv" in val.lower():
            aid = extract_arxiv_id(val)
            if aid:
                return aid
    return None


def fetch_arxiv(arxiv_id, session=None):
    sess = session or requests.Session()
    try:
        r = sess.get(ARXIV_API, params={"id_list": arxiv_id, "max_results": 1},
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException as exc:
        print(f"  ! network error looking up arXiv:{arxiv_id}: {exc}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  ! arXiv HTTP {r.status_code} for {arxiv_id} at {ARXIV_API}", file=sys.stderr)
        return None
    return parse_arxiv_atom(r.text)


def parse_arxiv_atom(xml_text: str):
    root = ET.fromstring(xml_text)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        return None
    # arXiv returns an entry even for a bad id, but without a title/id link.
    title_el = entry.find(f"{_ATOM}title")
    id_el = entry.find(f"{_ATOM}id")
    if title_el is None or id_el is None or not (title_el.text or "").strip():
        return None

    authors = []
    for a in entry.findall(f"{_ATOM}author"):
        name = a.find(f"{_ATOM}name")
        if name is not None and name.text:
            bits = name.text.split()
            family = bits[-1] if bits else name.text
            given = " ".join(bits[:-1])
            authors.append((family.strip(), given.strip()))

    published = entry.find(f"{_ATOM}published")
    year = (published.text or "")[:4] if published is not None else ""

    doi_el = entry.find(f"{_ARX}doi")
    jref_el = entry.find(f"{_ARX}journal_ref")

    ref = Reference(
        title=clean_latex(re.sub(r"\s+", " ", title_el.text)),
        authors=authors,
        year=year,
        # NB: deliberately leave ref.doi empty. The published DOI (if any) is
        # kept in `published_doi` and handled in the publication-status check,
        # so the primary title/author comparison doesn't false-flag the DOI.
    )
    return ArxivRecord(
        reference=ref,
        published_doi=(doi_el.text or "").strip() if doi_el is not None else "",
        journal_ref=(jref_el.text or "").strip() if jref_el is not None else "",
    )


# --------------------------------------------------------------------------
# 3c. Book lookup by ISBN (for books that have no DOI)
# --------------------------------------------------------------------------

OPENLIB_API = "https://openlibrary.org/api/books"
GOOGLEBOOKS_API = "https://www.googleapis.com/books/v1/volumes"


def clean_isbn(s: str) -> str:
    """Strip hyphens/spaces from an ISBN: '978-0-691-13298-3' -> '9780691132983'."""
    return re.sub(r"[^0-9Xx]", "", s or "").upper()


def _split_name(fullname: str) -> tuple[str, str]:
    """'P.-A. Absil' -> ('Absil', 'P.-A.'); 'Absil, P.-A.' -> ('Absil','P.-A.')."""
    fullname = clean_latex(fullname).strip()
    if "," in fullname:
        fam, _, given = fullname.partition(",")
        return fam.strip(), given.strip()
    bits = fullname.split()
    if not bits:
        return "", ""
    return bits[-1], " ".join(bits[:-1])


def fetch_openlibrary(isbn, session=None):
    """Look a book up on Open Library by ISBN (no key needed)."""
    sess = session or requests.Session()
    try:
        r = sess.get(OPENLIB_API, params={"bibkeys": f"ISBN:{isbn}",
                     "format": "json", "jscmd": "data"},
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} at {OPENLIB_API}", file=sys.stderr)
        return None
    rec = r.json().get(f"ISBN:{isbn}")
    if not rec:
        return None
    authors = [_split_name(a.get("name", "")) for a in rec.get("authors", [])
               if a.get("name")]
    pubs = rec.get("publishers", [])
    publisher = pubs[0].get("name", "") if pubs else ""
    ym = re.search(r"\d{4}", rec.get("publish_date", ""))
    return Reference(
        title=rec.get("title", ""), authors=authors,
        year=ym.group(0) if ym else "", publisher=publisher, isbn=isbn, raw=rec,
    )


def fetch_googlebooks(isbn, session=None):
    """Look a book up on the Google Books API by ISBN (no key for light use)."""
    sess = session or requests.Session()
    try:
        r = sess.get(GOOGLEBOOKS_API, params={"q": f"isbn:{isbn}"},
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} at {GOOGLEBOOKS_API}", file=sys.stderr)
        return None
    items = r.json().get("items", [])
    if not items:
        return None
    vi = items[0].get("volumeInfo", {})
    authors = [_split_name(a) for a in vi.get("authors", [])]
    ym = re.search(r"\d{4}", vi.get("publishedDate", ""))
    return Reference(
        title=vi.get("title", ""), authors=authors,
        year=ym.group(0) if ym else "", publisher=vi.get("publisher", ""),
        isbn=isbn, raw=vi,
    )


def _best_isbn(isbns) -> str:
    """Pick a preferred ISBN from a list, favouring ISBN-13."""
    cleaned = [c for c in (clean_isbn(x) for x in isbns) if c]
    for c in cleaned:
        if len(c) == 13:
            return c
    return cleaned[0] if cleaned else ""


def search_openlibrary_books(title, authors, session=None, limit=5):
    """Search Open Library by title+author. Returns candidate References,
    each carrying the catalog's ISBN so it can be recovered."""
    sess = session or requests.Session()
    params = {"title": title, "limit": limit}
    if authors:
        params["author"] = " ".join(f or g for f, g in authors)
    try:
        r = sess.get("https://openlibrary.org/search.json", params=params,
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} at https://openlibrary.org/search.json", file=sys.stderr)
        return []
    out = []
    for d in r.json().get("docs", [])[:limit]:
        pubs = d.get("publisher", [])
        out.append(Reference(
            title=d.get("title", ""),
            authors=[_split_name(n) for n in d.get("author_name", [])],
            year=str(d.get("first_publish_year", "") or ""),
            publisher=pubs[0] if pubs else "",
            isbn=_best_isbn(d.get("isbn", [])), raw=d,
        ))
    return out


def search_googlebooks_books(title, authors, session=None, limit=5):
    """Search Google Books by title+author. Returns candidate References."""
    sess = session or requests.Session()
    q = f"intitle:{title}"
    if authors:
        q += " " + " ".join(f"inauthor:{f}" for f, _ in authors if f)
    try:
        r = sess.get(GOOGLEBOOKS_API, params={"q": q, "maxResults": limit},
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} at {GOOGLEBOOKS_API}", file=sys.stderr)
        return []
    out = []
    print(r.json())
    for it in r.json().get("items", [])[:limit]:
        vi = it.get("volumeInfo", {})
        ym = re.search(r"\d{4}", vi.get("publishedDate", ""))
        ids = [x.get("identifier", "") for x in vi.get("industryIdentifiers", [])]
        out.append(Reference(
            title=vi.get("title", ""),
            authors=[_split_name(a) for a in vi.get("authors", [])],
            year=ym.group(0) if ym else "", publisher=vi.get("publisher", ""),
            isbn=_best_isbn(ids), raw=vi,
        ))
    return out


def search_book(title, authors, session=None):
    """Title+author book search. Open Library first; also consult Google Books
    if Open Library returns nothing OR its matches carry no ISBN (Open Library
    search records often omit the ISBN, while Google Books usually has it)."""
    cands = search_openlibrary_books(title, authors, session=session)
    if not cands or not any(c.isbn for c in cands):
        cands = cands + search_googlebooks_books(title, authors, session=session)
    return cands


def fetch_book_by_isbn(isbn, session=None):
    """Try Open Library, then Google Books. Returns (Reference, source_name)."""
    ref = fetch_openlibrary(isbn, session)
    if ref and ref.title:
        return ref, "Open Library"
    ref = fetch_googlebooks(isbn, session)
    if ref and ref.title:
        return ref, "Google Books"
    return None, ""


# --------------------------------------------------------------------------
# 4. Comparison logic
# --------------------------------------------------------------------------

def authors_match(local, ref):
    problems = []
    if len(local) != len(ref):
        problems.append(
            f"author count differs: bib has {len(local)} "
            f"({', '.join(f or g for f, g in local)}), "
            f"source has {len(ref)} ({', '.join(f or g for f, g in ref)})"
        )
        return problems
    for i, ((lf, lg), (rf, rg)) in enumerate(zip(local, ref), 1):
        if norm(lf) != norm(rf):
            problems.append(f"author #{i} family name: bib='{lf}' vs source='{rf}'")
            continue
        li, ri = initials(lg), initials(rg)
        n = min(len(li), len(ri))
        if li[:n] != ri[:n]:
            problems.append(
                f"author #{i} ({lf}) given-name initials: "
                f"bib='{lg}' ({''.join(li)}) vs source='{rg}' ({''.join(ri)})"
            )
    return problems


_JOURNAL_STOPWORDS = {"of", "on", "and", "the", "for", "in", "a", "an", "de", "der"}


def abbrev_compatible(abbrev, full):
    at = [t for t in tokens(abbrev) if t not in _JOURNAL_STOPWORDS]
    ft = [t for t in tokens(full) if t not in _JOURNAL_STOPWORDS]
    if not at or len(at) != len(ft):
        return False
    return all(f.startswith(a) for a, f in zip(at, ft))


def journal_match(local, ref):
    candidates = [c for c in (ref.journal, ref.journal_alt) if c]
    if not local or not candidates:
        return True
    for cand in candidates:
        if norm(local) == norm(cand):
            return True
        if abbrev_compatible(local, cand) or abbrev_compatible(cand, local):
            return True
    return False


def issue_match(a: str, b: str) -> bool:
    """True if two issue designations agree, tolerating combined issues.
    Old journals often bundle issues (e.g. "7-8"), while a .bib may cite just
    one part ("7"). After normalisation "7-8" becomes "7 8", so we accept a
    single-number issue that is one component of the other side."""
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    return na in nb.split() or nb in na.split()


# Minimum fields for a citation to be structurally complete, per BibTeX entry
# type. A tuple means "at least one of these must be present" (e.g. a book
# needs an author OR an editor). This is the classic BibTeX requirement set.
REQUIRED_FIELDS = {
    "article":       ["author", "title", "journal", "year"],
    "book":          [("author", "editor"), "title", "publisher", "year"],
    "booklet":       ["title"],
    "inbook":        [("author", "editor"), "title", ("chapter", "pages"),
                      "publisher", "year"],
    "incollection":  ["author", "title", "booktitle", "publisher", "year"],
    "inproceedings": ["author", "title", "booktitle", "year"],
    "conference":    ["author", "title", "booktitle", "year"],
    "proceedings":   ["title", "year"],
    "manual":        ["title"],
    "mastersthesis": ["author", "title", "school", "year"],
    "phdthesis":     ["author", "title", "school", "year"],
    "techreport":    ["author", "title", "institution", "year"],
    "unpublished":   ["author", "title", "note"],
    "misc":          [],
}


def missing_required_fields(local):
    """Required fields (per BibTeX entry type) that are absent OR present but
    empty. Handles alternatives like (author, editor). Returns [] for unknown
    types rather than guessing. A field like `number={}` counts as missing."""
    spec = REQUIRED_FIELDS.get(local.entrytype)
    if spec is None:
        return []

    def present(f):
        return bool(local.raw.get(f, "").strip())

    missing = []
    for req in spec:
        if isinstance(req, tuple):
            if not any(present(f) for f in req):
                missing.append(" or ".join(req))
        elif not present(req):
            missing.append(req)
    return missing


def compare(local, ref, check_year=True, suggest=False):
    """Full field comparison. Returns list of mismatch descriptions."""
    problems = authors_match(local.authors, ref.authors)

    if local.title and ref.title:
        # Old Crossref records sometimes store U+FFFD (the Unicode replacement
        # character) where a non-ASCII letter was lost on ingest -- e.g. an
        # umlaut in a German title. That character is irrecoverable, so exact
        # comparison would wrongly flag a correct entry. When it's present in
        # the source, fall back to a similarity check; only relaxing here keeps
        # exact matching (and typo detection) intact for clean records.
        if "\ufffd" in ref.title or "\ufffd" in local.title:
            if title_score(local.title, ref.title) < 0.90:
                problems.append(f"title: bib='{local.title}' vs source='{ref.title}' "
                                f"(source metadata contains corrupted characters)")
        elif norm(local.title) != norm(ref.title):
            problems.append(f"title: bib='{local.title}' vs source='{ref.title}'")

    if not journal_match(local.journal, ref):
        shown = ref.journal + (f" / {ref.journal_alt}" if ref.journal_alt else "")
        problems.append(f"journal: bib='{local.journal}' vs source='{shown}'")

    if check_year and local.year and ref.year:
        acceptable = ref.year_candidates or [ref.year]
        if local.year not in acceptable:
            extra = ""
            if len(acceptable) > 1:
                extra = f" (Crossref dates: {', '.join(acceptable)})"
            problems.append(f"year: bib='{local.year}' vs source='{ref.year}'{extra}")

    if local.volume and ref.volume and norm(local.volume) != norm(ref.volume):
        problems.append(f"volume: bib='{local.volume}' vs source='{ref.volume}'")

    if local.number and ref.number and not issue_match(local.number, ref.number):
        problems.append(f"number/issue: bib='{local.number}' vs source='{ref.number}'")

    if local.pages and ref.pages and norm(local.pages) != norm(ref.pages):
        problems.append(f"pages: bib='{local.pages}' vs source='{ref.pages}'")

    if (local.article_number and ref.article_number
            and norm(local.article_number) != norm(ref.article_number)):
        problems.append(
            f"article number (eid): bib='{local.article_number}' "
            f"vs source='{ref.article_number}'")

    if local.doi and ref.doi and local.doi.lower() != ref.doi.lower():
        problems.append(f"DOI: bib='{local.doi}' vs source='{ref.doi}'")

    if suggest:
        # Optional citation fields the source has but the entry omits. These are
        # suggestions, not errors (many styles legitimately drop the issue),
        # and are distinct from the required-field check done structurally.
        local_pages = local.pages or local.article_number
        ref_pages = ref.pages or ref.article_number
        for label, lv, rv in (("volume", local.volume, ref.volume),
                              ("number/issue", local.number, ref.number),
                              ("pages", local_pages, ref_pages)):
            if rv and not lv:
                problems.append(f"missing '{label}': source has '{rv}'")

    return problems


# --------------------------------------------------------------------------
# 5. Writing a discovered DOI back into the .bib text
# --------------------------------------------------------------------------

def insert_field(text, key, field_name, value):
    """Insert a `<field_name> = {...},` line right after the entry's opening
    line, matching the indentation of the following field. Preserves everything
    else in the file verbatim. Returns (new_text, inserted?)."""
    m = re.search(rf"(@\w+\s*\{{\s*{re.escape(key)}\s*,[ \t]*\r?\n)", text)
    if not m:
        return text, False
    after = text[m.end():]
    im = re.match(r"([ \t]+)\S", after)
    indent = im.group(1) if im else "  "
    line = f"{indent}{field_name} = {{{value}}},\n"
    return text[:m.end()] + line + text[m.end():], True


def insert_doi(text, key, doi):
    return insert_field(text, key, "doi", doi)


# --------------------------------------------------------------------------
# Reporting helpers
# --------------------------------------------------------------------------

def report(key, problems, ok_msg="OK"):
    if problems:
        print(f"[{key}] {len(problems)} possible issue(s):")
        for p in problems:
            print(f"    - {p}")
    else:
        print(f"[{key}] {ok_msg}")


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def handle_normal_doi(local, args, session):
    ref = fetch_crossref(local.doi, mailto=args.mailto, session=session)
    time.sleep(args.delay)
    extras = []
    if ref is None:
        print(f"[{local.key}] SKIPPED -- DOI '{local.doi}' not found on Crossref")
        return "skipped", []
    if args.concise:
        problems = compare(local, ref, suggest=args.suggest)
        if len(problems) > 0:
            extras.append(("problems", problems))
            report(local.key, problems)
    else:
        report(local.key, compare(local, ref, suggest=args.suggest))
    return "checked", extras


def handle_arxiv(local, args, session, arxiv_id):
    extras = []
    rec = fetch_arxiv(arxiv_id, session=session)
    time.sleep(max(args.delay, 3.0))          # arXiv asks for ~3s between calls
    if rec is None:
        print(f"[{local.key}] SKIPPED -- arXiv:{arxiv_id} not found")
        return "skipped", extras

    # If the entry carried no DOI, construct the registered arXiv DOI so it can
    # be reported and (with --out) written back into the .bib.
    if not local.doi:
        adoi = arxiv_doi_from_id(arxiv_id)
        print(f"[{local.key}] no DOI in entry -> arXiv preprint {arxiv_id} "
              f"(DOI {adoi})")
        extras.append(("doi", local.key, adoi))

    # arXiv preprints predate publication, so don't flag the year.
    problems = compare(local, rec.reference, check_year=False, suggest=args.suggest)
    if len(problems) > 0:
        extras.append(("problems", problems))

    if not args.concise or len(problems) > 0:
        report(local.key, problems, ok_msg=f"OK (matches arXiv:{arxiv_id})")

    # --- publication status -------------------------------------------------
    pub_doi = rec.published_doi
    note = None
    if pub_doi:
        note = f"authors linked a published DOI: {pub_doi}"
    elif rec.journal_ref:
        note = f"arXiv journal_ref set: {rec.journal_ref}"
    else:
        # Not linked on arXiv -> ask Crossref whether a published version exists.
        cands = search_crossref(local.title, local.authors,
                                mailto=args.mailto, session=session)
        time.sleep(args.delay)
        best, score = best_candidate(local, cands)
        if (best and best.doi and not is_arxiv_doi(best.doi)
                and score >= TITLE_ACCEPT and author_family_overlap(local, best)):
            pub_doi = best.doi
            j = best.journal or "?"
            note = (f"appears to be published as {pub_doi} "
                    f"in {j} ({best.year}) [title match {score:.2f}]")

    if note:
        if args.concise and len(problems) == 0 and local.doi:
            report(local.key, problems, ok_msg=f"OK (matches arXiv:{arxiv_id})")
        print(f"    * publication status: {note}")
        # If we found a real published DOI, check the entry against it too.
        if pub_doi and not is_arxiv_doi(pub_doi):
            pub_ref = fetch_crossref(pub_doi, mailto=args.mailto, session=session)
            time.sleep(args.delay)
            # This only adds verbose output with neglegible value
            # if pub_ref:
            #     pubs = compare(local, pub_ref, suggest=args.suggest)
            #     if pubs and not args.concise:
            #         print(f"    * vs published version ({pub_doi}):")
            #         for p in pubs:
            #             print(f"        - {p}")
            #     else:
            #         print(f"    * matches the published version ({pub_doi})")
        extras.append(("published", local.key, pub_doi))
    elif not args.concise:
        print("    * publication status: no published version found (still a preprint)")
    return "checked", extras


def handle_missing_doi(local, args, session):
    if not local.title:
        print(f"[{local.key}] SKIPPED -- no DOI and no title to search on")
        return "skipped", []
    extras = []
    cands = search_crossref(local.title, local.authors,
                            mailto=args.mailto, session=session)
    time.sleep(args.delay)
    best, score = best_candidate(local, cands)

    if best and score >= TITLE_ACCEPT and author_family_overlap(local, best):
        print(f"[{local.key}] no DOI in entry -> found {best.doi} "
              f"[title match {score:.2f}]")
        problems = compare(local, best, suggest=args.suggest)
        if len(problems) > 0:
            extras.append(("problems", problems))
        if not args.concise or len(problems) > 0:
            report(local.key, problems, ok_msg="fields otherwise match the found record")
        extras.append(("doi", local.key, best.doi))
        return "found", extras

    if best and score >= 0.6:
        print(f"[{local.key}] no DOI -- best candidate uncertain "
              f"(title match {score:.2f}), verify manually:")
        for c in cands[:3]:
            print(f"        {c.doi}  {c.title}")
        return "uncertain", extras

    print(f"[{local.key}] no DOI -- no confident match found on Crossref")
    return "uncertain", extras


def handle_isbn(local, isbn, args, session):
    """Look a (no-DOI) book up by ISBN via Open Library / Google Books.
    Falls back to the Crossref title search if the ISBN isn't found."""
    extras = []
    ref, source = fetch_book_by_isbn(isbn, session=session)
    time.sleep(args.delay)
    if ref is None:
        print(f"[{local.key}] ISBN {isbn} not found in Open Library or "
              f"Google Books -- trying a title search")
        return handle_missing_doi(local, args, session)

    problems = compare(local, ref, suggest=args.suggest)
    # For a book looked up by ISBN, the publisher is the headline datum, so
    # surface it whenever the entry lacks one (regardless of --suggest).
    # commented out, since this is catched by "miss" regardless
    # if not local.publisher and ref.publisher:
    #     problems.append(f"missing 'publisher': source has '{ref.publisher}'")
    if len(problems) > 0:
        extras.append(("problems", problems))
    if not args.concise or len(problems) > 0:
        report(local.key, problems, ok_msg=f"OK (matches {source}, ISBN {isbn})")
    return "checked", extras


# Entry types treated as books when they have no DOI/ISBN: these resolve far
# better against a book catalog (Open Library / Google Books) than Crossref.
BOOK_TYPES = {"book", "inbook", "incollection", "proceedings", "booklet", "manual"}


def handle_missing_book(local, args, session):
    """No DOI, no ISBN, book-like type -> search a book catalog by title+author
    to recover the ISBN. Books are REPORTED for confirmation (not auto-trusted
    like the article path), matched on title+author, and not failed on a year
    gap, because editions/reprints legitimately differ. Falls back to the
    Crossref title search if the catalogs turn up nothing."""
    extras = []
    if not local.title:
        print(f"[{local.key}] SKIPPED -- no DOI/ISBN and no title to search on")
        return "skipped", []

    cands = search_book(local.title, local.authors, session=session)
    time.sleep(args.delay)
    if not cands:
        print(f"[{local.key}] no DOI/ISBN -- not found in book catalogs, "
              f"trying Crossref")
        return handle_missing_doi(local, args, session)

    best, score = best_candidate(local, cands)
    # If the top title match carries no ISBN, prefer an equally-good candidate
    # (e.g. from Google Books) that does, so the ISBN can be reported/recovered.
    if best and not best.isbn:
        for c in cands:
            if (c.isbn and title_score(local.title, c.title) >= TITLE_ACCEPT
                    and author_family_overlap(local, c)):
                best, score = c, title_score(local.title, c.title)
                break

    if best and score >= TITLE_ACCEPT and author_family_overlap(local, best):
        if best.isbn:
            print(f"[{local.key}] no DOI/ISBN -> book catalog match, "
                  f"ISBN {best.isbn} [title {score:.2f}] -- please verify the edition")
        else:
            print(f"[{local.key}] no DOI/ISBN -> book catalog match, but no ISBN "
                  f"in the record [title {score:.2f}] -- please verify the edition")
        # Lenient: check author list only; the title already matched by score,
        # and the year may legitimately differ by edition.
        problems = authors_match(local.authors, best.authors)
        if not local.publisher and best.publisher:
            problems.append(f"missing 'publisher': catalog has '{best.publisher}'")
        if not args.concise or len(problems) > 0:
            report(local.key, problems, ok_msg="title/author match the catalog record")
        extras = [("isbn", local.key, best.isbn)] if best.isbn else []
        if len(problems) > 0:
            extras.append(("problems", problems))
        return "found", extras

    if best and score >= 0.6:
        print(f"[{local.key}] no DOI/ISBN -- best book candidate uncertain "
              f"(title {score:.2f}), verify manually:")
        for c in cands[:3]:
            print(f"        {c.isbn or '(no isbn)'}  {c.title}")
        return "uncertain", []

    print(f"[{local.key}] no DOI/ISBN -- no confident book match found")
    return "uncertain", []


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check .bib entries against Crossref / arXiv.")
    ap.add_argument("bibfile")
    ap.add_argument("--mailto", help="your email (Crossref's faster polite pool)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between lookups (default 0.5)")
    ap.add_argument("--out", metavar="FILE",
                    help="write a copy of the .bib with recovered DOIs/ISBNs added")
    ap.add_argument("--suggest", action="store_true",
                    help="also suggest optional fields (volume/issue/pages) the "
                         "online record has but the entry omits")
    ap.add_argument("--concise", action="store_true",
                help="only display entries where action may be required")
    args = ap.parse_args(argv)

    refs = parse_bib(args.bibfile)
    print(f"Parsed {len(refs)} entries from {args.bibfile}\n")

    session = requests.Session()
    counts = {"checked": 0, "found": 0, "uncertain": 0, "skipped": 0}
    discovered = {}     # key -> (field_name, value) to write back
    published = []      # (key, doi) preprints found to be published

    for local in refs:
        arxiv_id = detect_arxiv_id(local)
        isbn = clean_isbn(local.raw.get("isbn", ""))
        if local.doi and not is_arxiv_doi(local.doi):
            status, extras = handle_normal_doi(local, args, session)
        elif arxiv_id:
            status, extras = handle_arxiv(local, args, session, arxiv_id)
        elif isbn:
            status, extras = handle_isbn(local, isbn, args, session)
        elif local.entrytype in BOOK_TYPES:
            status, extras = handle_missing_book(local, args, session)
        else:
            status, extras = handle_missing_doi(local, args, session)

        # Structural completeness: required fields for this entry type. This is
        # independent of the online lookup, so it runs (and reports) even when
        # the entry was skipped.
        miss = missing_required_fields(local)
        if miss:
            if args.concise and len(extras) == 0:
                report(local.key, [])
            print(f"    * incomplete @{local.entrytype}: missing required "
                  f"field(s): {', '.join(miss)}")

        counts[status] = counts.get(status, 0) + 1
        for extra in extras:
            if extra[0] in ("doi", "isbn"):
                discovered[extra[1]] = (extra[0], extra[2])
            elif extra[0] == "published":
                published.append((extra[1], extra[2]))

    # The Done line partitions entries by lookup outcome. "looked up" merges
    # 'checked' and 'found' (both were successfully resolved against a source);
    # they are not reported as a separate "recovered" number because that would
    # invite confusion with the write-back count below, which is a DIFFERENT
    # axis (an entry can be looked up AND recover a writable identifier).
    looked_up = counts.get('checked', 0) + counts.get('found', 0)
    print(f"\nDone: {looked_up} looked up, "
          f"{counts.get('uncertain',0)} unresolved, "
          f"{counts.get('skipped',0)} skipped.")

    if published:
        print("\nPreprints that appear to be published (consider updating):")
        for key, doi in published:
            print(f"    [{key}] -> {doi}")

    if discovered and args.out:
        with open(args.bibfile, encoding="utf-8") as fh:
            text = fh.read()
        added = 0
        for key, (field_name, value) in discovered.items():
            text, ok = insert_field(text, key, field_name, value)
            added += int(ok)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nWrote {args.out} with {added} identifier(s) added "
              f"(original file untouched).")
    elif discovered:
        print(f"\n{len(discovered)} identifier(s) recovered (doi/isbn) -- "
              f"re-run with --out FILE to write them into a copy of the .bib.")

if __name__ == "__main__":
    main()