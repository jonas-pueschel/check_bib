#!/usr/bin/env python3
"""
check_bib.py -- Verify the fields of .bib entries against authoritative online
sources, and fill in / flag missing information.

For each entry the script does whichever of these applies:

  * Has a normal DOI      -> look it up on Crossref and compare every field.
  * Has an arXiv DOI       -> Crossref doesn't hold arXiv records (they live at
    (10.48550/arXiv...)       DataCite), so query the arXiv API instead, then
                              also check whether the preprint has since been
                              published (via arXiv's journal_ref / linked DOI
                              and a Crossref title search).
  * Has no DOI at all      -> search Crossref by title + author to find the
                              DOI, verify the title really matches, report it,
                              and (with --out) write it back into a copy of the
                              .bib file.

Why Crossref for the lookup? Its REST API returns structured JSON: separate
given/family names, and BOTH the full journal name and its ISO abbreviation,
which makes checking abbreviated authors and journal abbreviations reliable.

Usage:
    python check_bib.py references.bib --mailto you@example.org
    python check_bib.py references.bib --mailto you@example.org --out fixed.bib

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
        raw=msg,
    )


def fetch_crossref(doi, mailto=None, session=None):
    sess = session or requests.Session()
    try:
        r = sess.get(CROSSREF_WORK.format(doi=requests.utils.quote(doi)),
                     headers=_ua(mailto), timeout=30)
    except requests.RequestException as exc:
        print(f"  ! network error looking up {doi}: {exc}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} for {doi}", file=sys.stderr)
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


def fetch_arxiv(arxiv_id, session=None):
    sess = session or requests.Session()
    try:
        r = sess.get(ARXIV_API, params={"id_list": arxiv_id, "max_results": 1},
                     headers={"User-Agent": "check_bib/2.0"}, timeout=30)
    except requests.RequestException as exc:
        print(f"  ! network error looking up arXiv:{arxiv_id}: {exc}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  ! arXiv HTTP {r.status_code} for {arxiv_id}", file=sys.stderr)
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


def compare(local, ref, check_year=True):
    """Full field comparison. Returns list of mismatch descriptions."""
    problems = authors_match(local.authors, ref.authors)

    if local.title and ref.title and norm(local.title) != norm(ref.title):
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

    if local.number and ref.number and norm(local.number) != norm(ref.number):
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

    return problems


# --------------------------------------------------------------------------
# 5. Writing a discovered DOI back into the .bib text
# --------------------------------------------------------------------------

def insert_doi(text, key, doi):
    """Insert a `doi = {...},` line right after the entry's opening line,
    matching the indentation of the following field. Preserves everything
    else in the file verbatim. Returns (new_text, inserted?)."""
    m = re.search(rf"(@\w+\s*\{{\s*{re.escape(key)}\s*,[ \t]*\r?\n)", text)
    if not m:
        return text, False
    after = text[m.end():]
    im = re.match(r"([ \t]+)\S", after)
    indent = im.group(1) if im else "  "
    line = f"{indent}doi = {{{doi}}},\n"
    return text[:m.end()] + line + text[m.end():], True


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
    if ref is None:
        print(f"[{local.key}] SKIPPED -- DOI '{local.doi}' not found on Crossref")
        return "skipped", None
    report(local.key, compare(local, ref))
    return "checked", None


def handle_arxiv(local, args, session):
    arxiv_id = extract_arxiv_id(local.doi, local.raw.get("eprint", ""),
                                local.raw.get("url", ""))
    if not arxiv_id:
        print(f"[{local.key}] SKIPPED -- looks like arXiv but no id could be parsed")
        return "skipped", None

    rec = fetch_arxiv(arxiv_id, session=session)
    time.sleep(max(args.delay, 3.0))          # arXiv asks for ~3s between calls
    if rec is None:
        print(f"[{local.key}] SKIPPED -- arXiv:{arxiv_id} not found")
        return "skipped", None

    # arXiv preprints predate publication, so don't flag the year.
    problems = compare(local, rec.reference, check_year=False)
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
        print(f"    * publication status: {note}")
        # If we found a real published DOI, check the entry against it too.
        if pub_doi and not is_arxiv_doi(pub_doi):
            pub_ref = fetch_crossref(pub_doi, mailto=args.mailto, session=session)
            time.sleep(args.delay)
            if pub_ref:
                pubs = compare(local, pub_ref)
                if pubs:
                    print(f"    * vs published version ({pub_doi}):")
                    for p in pubs:
                        print(f"        - {p}")
                else:
                    print(f"    * matches the published version ({pub_doi})")
        return "checked", ("published", local.key, pub_doi)
    else:
        print("    * publication status: no published version found (still a preprint)")
    return "checked", None


def handle_missing_doi(local, args, session):
    if not local.title:
        print(f"[{local.key}] SKIPPED -- no DOI and no title to search on")
        return "skipped", None

    cands = search_crossref(local.title, local.authors,
                            mailto=args.mailto, session=session)
    time.sleep(args.delay)
    best, score = best_candidate(local, cands)

    if best and score >= TITLE_ACCEPT and author_family_overlap(local, best):
        print(f"[{local.key}] no DOI in entry -> found {best.doi} "
              f"[title match {score:.2f}]")
        report(local.key, compare(local, best),
               ok_msg="fields otherwise match the found record")
        return "found", ("doi", local.key, best.doi)

    if best and score >= 0.6:
        print(f"[{local.key}] no DOI -- best candidate uncertain "
              f"(title match {score:.2f}), verify manually:")
        for c in cands[:3]:
            print(f"        {c.doi}  {c.title}")
        return "uncertain", None

    print(f"[{local.key}] no DOI -- no confident match found on Crossref")
    return "uncertain", None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check .bib entries against Crossref / arXiv.")
    ap.add_argument("bibfile")
    ap.add_argument("--mailto", help="your email (Crossref's faster polite pool)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between lookups (default 0.5)")
    ap.add_argument("--out", metavar="FILE",
                    help="write a copy of the .bib with discovered DOIs added")
    args = ap.parse_args(argv)

    refs = parse_bib(args.bibfile)
    print(f"Parsed {len(refs)} entries from {args.bibfile}\n")

    session = requests.Session()
    counts = {"checked": 0, "found": 0, "uncertain": 0, "skipped": 0}
    discovered = {}     # key -> doi to write back
    published = []      # (key, doi) preprints found to be published

    for local in refs:
        if local.doi and is_arxiv_doi(local.doi):
            status, extra = handle_arxiv(local, args, session)
        elif extract_arxiv_id(local.raw.get("eprint", "")) and not local.doi:
            status, extra = handle_arxiv(local, args, session)
        elif local.doi:
            status, extra = handle_normal_doi(local, args, session)
        else:
            status, extra = handle_missing_doi(local, args, session)

        counts[status] = counts.get(status, 0) + 1
        if extra and extra[0] == "doi":
            discovered[extra[1]] = extra[2]
        elif extra and extra[0] == "published":
            published.append((extra[1], extra[2]))

    print(f"\nDone: {counts.get('checked',0)} checked, "
          f"{counts.get('found',0)} DOI found, "
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
        for key, doi in discovered.items():
            text, ok = insert_doi(text, key, doi)
            added += int(ok)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nWrote {args.out} with {added} DOI(s) added "
              f"(original file untouched).")
    elif discovered:
        print(f"\n{len(discovered)} DOI(s) discovered. "
              f"Re-run with --out FILE to write them into a copy of the .bib.")


# --------------------------------------------------------------------------
# Optional: doi2bib backend
# --------------------------------------------------------------------------

def fetch_doi2bib(doi, session=None):
    sess = session or requests.Session()
    try:
        r = sess.get(f"https://www.doi2bib.org/bib/{requests.utils.quote(doi)}",
                     headers={"Accept": "application/x-bibtex"}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.text.strip():
        return None
    db = bibtexparser.bparser.BibTexParser(common_strings=True).parse(r.text)
    if not db.entries:
        return None
    e = db.entries[0]
    return Reference(
        title=clean_latex(e.get("title", "")),
        authors=parse_bib_authors(e.get("author", "")),
        journal=clean_latex(e.get("journal", "")),
        year=e.get("year", "").strip(),
        volume=e.get("volume", "").strip(),
        number=e.get("number", "").strip(),
        pages=e.get("pages", "").strip(),
        article_number=e.get("eid", "").strip() or e.get("article-number", "").strip(),
        doi=e.get("doi", "").strip() or doi,
        raw=e,
    )


if __name__ == "__main__":
    main()