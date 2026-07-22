# check_bib.py 
Written with help of Claude Opus 4.8. Verify the fields of `.bib` entries against authoritative online 
sources, and fill in / flag missing information.

For each entry the script does whichever of these applies:

 * Has a normal DOI      -> look it up on Crossref and compare every field.
 * Has an arXiv DOI       -> Crossref doesn't hold arXiv records (they live at
   (10.48550/arXiv...)       DataCite), so query the arXiv API instead, then
                             also check whether the preprint has since been
                             published (via arXiv's journal_ref / linked DOI
                             and a Crossref title search).
 * Book with an ISBN     -> Open Library, then Google Books (no API key). Used
                            for books that have no DOI.
 * No DOI/ISBN           -> the lookup is chosen by entry type:
     - articles/proceedings papers: Crossref title +
       author search; a close title match reports the
       DOI and, with `--out`, writes it back;
     - books (`@book`/`@inbook`/`@incollection`/...): a book
       catalog search (Open Library, then Google Books)
       to recover the ISBN. Crossref's book coverage is
       spotty, so catalogs are used instead. Book hits
       are REPORTED for you to confirm (not auto-
       trusted), matched on title+author, and lenient
       about edition/year; `--out` writes the ISBN back.


## Dependencies:
The dependencies need to be installed before the script can be run. Optionally, one can first initialize a python virtual enviroment
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```
The dependencies are installed via
```bash
pip install bibtexparser pylatexenc requests
```

## Usage:
To run the file on `references.bib`, run
```bash
python check_bib.py references.bib 
```
It has the following (optional) command line parameters
* `--mailto you@example.org` puts you in Crossref's faster "polite" pool. The passed files do not get changed (recommended). 
* `--out fixed.bib` creates an output file, where missing DOIs and ISBNs are added (other missing fields are not yet added).
* `--suggest` flags missing non-essential fields (refer to the `REQUIRED_FIELDS` variable), that occur in the reference but not the `.bib` file (recommended).
* `--concise` only display entries where action may be required.

## Caveats
The comparison has some caveats:
1. Authors are currently only compared with lastname and the initial of the first name: `P\"uschel, Johannes` instead of `P\"uschel, Jonas` would hence not be caught (`P\"uschl, Jonas` however would). Umlaute and other special characters are normalized (e.g. `ü -> u`) in the comparison. Also, middle names are truncated in the comparison.
2. The `year` field is truly disambiguous. Crossref gives different dates for `published`, `published-online`, `published-print` and `issued`. The script accepts every year that appears in any of those fields.
3. There are no consistency checks between entries, i.e. if all entries use full or abbreviated journal titles or if all entries use the same author format. 

## License

MIT License © 2026 Jonas Püschel
