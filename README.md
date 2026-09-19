# Bellhaven CRM Sync

Keeps Bellhaven Senior Living's facility-to-parent links in the CRM accurate by comparing the CRM against Bellhaven's public website. Every change is proposed with evidence and written only after a human approves it.

## How it works

```
scrape.py  ->  match.py  ->  app.py (review)  ->  apply.py (writes to CRM)
website        proposals     approve / reject     billing SOP enforced here
```

| File | Purpose |
|---|---|
| `explore.py` | One-off exploration of the API and data, used to find the traps before building |
| `scrape.py` | Pulls every community (name, address, care offerings) from the directory **and** homepage |
| `match.py` | Matches locations to CRM accounts and writes proposals to `data/pipeline.db` |
| `app.py` | Local review app: side-by-side evidence, approve or reject |
| `apply.py` | Writes an approved proposal to the CRM; enforces the CHOW billing SOP |
| `common.py` | Shared config (parent ID, orphan status, care-type mapping) and API/DB helpers |
| `.github/workflows/daily-sync.yml` | Daily schedule (proposes only, never writes) |
| `crontab.txt` | Cron alternative |

## Running it

```bash
pip3 install -r requirements.txt
export CRM_TOKEN=<your token>
python3 match.py --scrape   # scrape + generate proposals
python3 app.py              # review at http://127.0.0.1:5000
```

Running `match.py` again after approving should report `proposals_found: 0`.

## Writeup

**Matching approach.** I scraped all 35 locations: 34 from the paginated directory plus Bellhaven Meadows of Findlay, which is linked only from the homepage. Matching is address-first (house number, normalized street, then zip or city), because names change when facilities are sold and addresses don't. If no address matches, it falls back to name plus same city, which caught Ashtabula, whose CRM record had a PO box. It never matches on name alone across cities, which kept Amberly Manor (Hudson, OH) separate from an unrelated Amberly Manor in Colorado Springs, and Carlisle PA separate from New Carlisle OH. Address matching also found three facilities under pre-Bellhaven names (e.g. Sunny Acres Retirement Home → Bellhaven Willow Creek) and several leftover Harborview and Cedar Trail records, consistent with the acquisitions described on the About page.

**Result.** 29 approved changes. 6 re-parents: Marietta and Tiffin went through CHOW; Lima and Findlay have revenue but $0 AR, so they moved directly. 7 duplicates retired (Inactive plus `duplicate_of_account`). 4 new accounts. 9 field fixes (7 renames, a transposed zip, a PO box). 3 orphans. A re-run proposes nothing, with all 35 locations matched.

**Judgment calls.**
- *Source of truth:* the website decides which facilities Bellhaven owns and their current details; the CRM remains the authority on billing data, which the pipeline never overwrites.
- *Orphans* are set to Needs Review with a note, and their parent is unchanged. The website shows Bellhaven dropped them but not who owns them now, and moving Sandusky would trigger CHOW.
- *Duplicate survivors* are chosen by billing history, then correct parent, then completeness. None of the retired copies had billing history.
- *CHOW* is enforced at approval time against live CRM data. It is retry-safe: if linking fails after the new account is created, a retry reuses that account rather than creating a second one.
- *Re-runs* are idempotent: each proposal gets a fingerprint in SQLite, so decided items are never re-proposed. The daily job only proposes; nothing writes without a human click.

**How I used AI.** I used Claude to scaffold the scraper, matcher, and review app, and directed and checked it at each step. Checking its work caught several issues: an early script pulled only 50 of 121 accounts (wrong pagination parameter) and assumed an `id` field that didn't exist; the first match run proposed ~20 false care-type changes because the CRM and website use different vocabularies ("Memory Care" vs "Memory Support"), which I fixed with a translation table; and a review of the SOP code found a retry case that could have created duplicate CHOW accounts. I reviewed every proposal's evidence before approving.

**What I'd build next.** An undo action using the before-values already stored with each proposal; bulk approval for high-confidence changes like pure renames; CRM contacts (e.g. Bellhaven email domains) as extra match evidence; alerts when items sit in Needs Review; and a hosted database instead of committing SQLite.
