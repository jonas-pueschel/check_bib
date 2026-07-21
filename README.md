# check_bib.py 
Written with help of Claude Opus 4.8. Verify the fields of `.bib` entries against authoritative online, 
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
* `--mailto you@example.org` puts you in Crossref's faster "polite" pool. The passed files does not get changed. 
* `--out fixed.bib` creates an output file, where missing DOIs are added (other missing fields are not yet added).
* `--suggest` flags missing non-essential fields (refer to the `REQUIRED_FIELDS` variable), that occur in the reference but not the `.bib` file.

## Caveats
The comparison has two caveats:
1. Authors are currently only compared with lastname and the initial of the first name: `P\"uschel, Johannes` instead of `P\"uschel, Jonas` would hence not be caught (`Püschl, Jonas` however would). Umlaute and other special characters are normalized (e.g. `ü -> u`) in the comparison. Also, middle names are truncated in the comparison.
2. The `year` field is truly disambiguous. Crossref gives different dates for `published`, `published-online`, `published-print` and `issued`. The script accepts every year that apperas in either of those fields.
3. There are no consistency checks between entries, i.e. if all entries use full or abbreviated journal titles or if all entries use the same author format. 

## Why Crossref for the lookup? 
Its REST API returns structured JSON: separate
given/family names, and BOTH the full journal name and its ISO abbreviation,
which makes checking abbreviated authors and journal abbreviations reliable.
