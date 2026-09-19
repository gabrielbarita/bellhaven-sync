"""
match.py - Step 2: match website locations to CRM accounts and write proposals.

Nothing here writes to the CRM. It only fills data/pipeline.db with proposals
that a human approves in the review app.

Usage:
    python3 match.py              # uses data/locations.json from scrape.py
    python3 match.py --scrape     # scrape the site first
"""

import hashlib
import json
import re
import sys
from difflib import SequenceMatcher

from common import (BELLHAVEN_PARENT_ID, DATA, ORPHAN_STATUS, crm_care_types, db,
                    get_all_accounts, needs_chow, now, num, primary_care_type)

# ------------------------------------------------------------ normalization
ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "court": "ct", "place": "pl",
    "parkway": "pkwy", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
UNIT = re.compile(r"\b(suite|ste|unit|apt|#)\b.*$")


def norm_street(s):
    s = (s or "").lower().replace(".", " ").replace(",", " ")
    s = UNIT.sub("", s)
    return " ".join(ABBREV.get(t, t) for t in s.split())


def norm_name(s):
    s = (s or "").lower().replace("&", " and ").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = s.replace("rehab ", "rehabilitation ").replace("centre", "center")
    s = s.replace("healthcare", "health care")
    drop = {"the", "of", "at", "and"}
    return " ".join(t for t in s.split() if t not in drop)


def sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def house_number(street):
    m = re.match(r"^\s*(\d+)", street or "")
    return m.group(1) if m else None


def care_set(value):
    return {p.strip().lower() for p in re.split(r"[;,/|]", value or "") if p.strip()}


# ------------------------------------------------------------ scoring
def score(loc, acct):
    """How well does a CRM account match a website location?"""
    ls, as_ = norm_street(loc["street"]), norm_street(acct.get("billing_street"))
    same_number = house_number(ls) is not None and house_number(ls) == house_number(as_)
    street_sim = sim(ls, as_)
    same_zip = loc["zip"] and loc["zip"] == str(acct.get("billing_zip") or "")[:5]
    same_city = (loc["city"].lower() == str(acct.get("billing_city") or "").lower()
                 and loc["state"] == acct.get("billing_state"))
    name_sim = sim(norm_name(loc["name"]), norm_name(acct.get("name")))

    address_match = same_number and street_sim >= 0.8 and (same_zip or same_city)
    if address_match:
        tier = "address"                     # strongest: names change, addresses don't
    elif name_sim >= 0.85 and same_city:
        tier = "name+city"                   # address differs: likely a data-entry error
    else:
        tier = None
    return {
        "tier": tier, "street_sim": round(street_sim, 2), "name_sim": round(name_sim, 2),
        "same_number": same_number, "same_zip": bool(same_zip), "same_city": same_city,
    }


# ------------------------------------------------------------ helpers
def fingerprint(kind, account_id, slug, payload):
    raw = json.dumps([kind, account_id, slug, payload], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def snapshot(a):
    keys = ["account_id", "name", "parent_id", "parent_name", "billing_street",
            "billing_city", "billing_state", "billing_zip", "care_type", "status",
            "phone", "lifetime_revenue", "outstanding_ar", "chow_current_account",
            "duplicate_of_account", "note", "updated_at"]
    return {k: a.get(k) for k in keys}


def site_fields(loc):
    """The CRM field values the website says this facility should have."""
    return {
        "name": loc["name"],
        "billing_street": loc["street"],
        "billing_city": loc["city"],
        "billing_state": loc["state"],
        "billing_zip": loc["zip"],
        "care_type": primary_care_type(loc["care_offerings"]),
    }


def field_diffs(loc, acct):
    """Only real differences - 'Avenue' vs 'Ave' is not a change."""
    want, changes = site_fields(loc), {}
    if want["name"] != acct.get("name"):
        changes["name"] = want["name"]
    if norm_street(want["billing_street"]) != norm_street(acct.get("billing_street")):
        changes["billing_street"] = want["billing_street"]
    if want["billing_city"].lower() != str(acct.get("billing_city") or "").lower():
        changes["billing_city"] = want["billing_city"]
    if want["billing_state"] != acct.get("billing_state"):
        changes["billing_state"] = want["billing_state"]
    if want["billing_zip"] != str(acct.get("billing_zip") or "")[:5]:
        changes["billing_zip"] = want["billing_zip"]
    # Only a real mismatch: the CRM value must be one of the site's offerings (translated)
    if acct.get("care_type") not in crm_care_types(loc["care_offerings"]):
        changes["care_type"] = want["care_type"]
    if acct.get("status") != "Active":
        changes["status"] = "Active"
    return changes


def survivor_rank(a):
    """Which copy of a duplicate to keep: billing history first, then correct parent..."""
    filled = sum(1 for k in ("billing_street", "billing_zip", "phone", "care_type") if a.get(k))
    return (num(a.get("lifetime_revenue")) > 0, a.get("parent_id") == BELLHAVEN_PARENT_ID,
            a.get("status") == "Active", filled)


# ------------------------------------------------------------ main matching
def build_proposals(locations, accounts):
    by_id = {a["account_id"]: a for a in accounts}
    parent_ids = {a.get("parent_id") for a in accounts if a.get("parent_id")}

    # Candidates = facility accounts that are still "live" records.
    # Excluded: parent companies, already-resolved duplicates, and old CHOW
    # records (their chow_current_account points at the live replacement).
    candidates = [a for a in accounts
                  if a["account_id"] not in parent_ids
                  and "(parent account)" not in str(a.get("name", "")).lower()
                  and not a.get("duplicate_of_account")
                  and not a.get("chow_current_account")]

    proposals, matched_ids, confident = [], set(), []

    for loc in locations:
        scored = [(a, score(loc, a)) for a in candidates]
        hits = [(a, s) for a, s in scored if s["tier"]]
        # Prefer address matches; only fall back to name+city if there are none
        if any(s["tier"] == "address" for _, s in hits):
            hits = [(a, s) for a, s in hits if s["tier"] == "address"]
        matched_ids.update(a["account_id"] for a, _ in hits)

        # ---- no CRM account: create one
        if not hits:
            best = sorted(scored, key=lambda x: -x[1]["name_sim"])[:3]
            payload = {**site_fields(loc), "parent_id": BELLHAVEN_PARENT_ID, "status": "Active",
                       "phone": loc.get("phone", ""),
                       "note": f"Created from Bellhaven website listing ({loc['url']})."}
            proposals.append(dict(
                kind="create", account_id=None, slug=loc["slug"], payload=payload,
                summary=f"Create account for {loc['name']} ({loc['city']}, {loc['state']})",
                evidence={"location": loc, "closest_non_matches": [
                    {"account": snapshot(a), "score": s} for a, s in best]},
                base_updated_at=None))
            continue

        # ---- duplicates: keep the best copy, retire the rest
        hits.sort(key=lambda x: survivor_rank(x[0]), reverse=True)
        survivor, s_score = hits[0]
        for loser, l_score in hits[1:]:
            note = (f"Duplicate of {survivor['account_id']} ({survivor['name']}); "
                    f"same facility as website listing {loc['url']}.")
            if needs_chow(loser):
                note += " NOTE: this copy has revenue and open AR - billing should confirm."
            payload = {"duplicate_of_account": survivor["account_id"], "status": "Inactive",
                       "note": (loser.get("note") + " | " if loser.get("note") else "") + note}
            proposals.append(dict(
                kind="duplicate", account_id=loser["account_id"], slug=loc["slug"],
                payload=payload,
                summary=f"Mark {loser['name']} ({loser['account_id']}) as duplicate of "
                        f"{survivor['account_id']}",
                evidence={"location": loc, "loser": snapshot(loser), "loser_score": l_score,
                          "survivor": snapshot(survivor), "survivor_score": s_score},
                base_updated_at=loser.get("updated_at")))

        # ---- survivor: wrong parent?
        changes = field_diffs(loc, survivor)
        wrong_parent = survivor.get("parent_id") != BELLHAVEN_PARENT_ID
        evidence = {"location": loc, "account": snapshot(survivor), "score": s_score,
                    "before": {k: survivor.get(k) for k in changes}}

        if wrong_parent:
            chow = needs_chow(survivor)
            evidence["sop"] = {
                "lifetime_revenue": survivor.get("lifetime_revenue"),
                "outstanding_ar": survivor.get("outstanding_ar"),
                "path": "CHOW: create new account, old keeps parent, set chow_current_account"
                        if chow else "Re-parent existing account directly",
            }
            payload = {"new_parent_id": BELLHAVEN_PARENT_ID, "field_changes": changes,
                       "new_account": {**site_fields(loc), "parent_id": BELLHAVEN_PARENT_ID,
                                       "status": "Active", "phone": loc.get("phone", "")}}
            old_parent = survivor.get("parent_name") or "no parent"
            proposals.append(dict(
                kind="reparent", account_id=survivor["account_id"], slug=loc["slug"],
                payload=payload,
                summary=f"{'CHOW' if chow else 'Re-parent'}: {survivor['name']} "
                        f"from {old_parent} to Bellhaven",
                evidence=evidence, base_updated_at=survivor.get("updated_at")))
        elif changes:
            proposals.append(dict(
                kind="update", account_id=survivor["account_id"], slug=loc["slug"],
                payload={"field_changes": changes},
                summary=f"Update {survivor['name']}: {', '.join(changes)}",
                evidence=evidence, base_updated_at=survivor.get("updated_at")))
        else:
            confident.append({"location": loc["name"], "account_id": survivor["account_id"]})

    # ---- orphans: under Bellhaven in the CRM, not on the website
    for a in candidates:
        if (a.get("parent_id") == BELLHAVEN_PARENT_ID and a["account_id"] not in matched_ids
                and a.get("status") == "Active"):
            note = (f"Not listed on Bellhaven website as of {now()[:10]}; "
                    f"ownership unknown - verify before re-parenting.")
            payload = {"status": ORPHAN_STATUS,
                       "note": (a.get("note") + " | " if a.get("note") else "") + note}
            proposals.append(dict(
                kind="orphan", account_id=a["account_id"], slug=None, payload=payload,
                summary=f"Flag {a['name']} ({a.get('billing_city')}): no longer on website",
                evidence={"account": snapshot(a),
                          "closest_locations": sorted(
                              ({"name": l["name"], "city": l["city"], **score(l, a)}
                               for l in locations), key=lambda x: -x["name_sim"])[:3]},
                base_updated_at=a.get("updated_at")))

    return proposals, confident


def save(proposals, confident):
    conn = db()
    run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid
    new = skipped = 0
    for p in proposals:
        # Notes contain today's date, so leave them out of the fingerprint
        fp_payload = {k: v for k, v in p["payload"].items() if k != "note"}
        fp = fingerprint(p["kind"], p["account_id"], p["slug"], fp_payload)
        cur = conn.execute(
            """INSERT OR IGNORE INTO proposals
               (fingerprint, run_id, kind, account_id, location_slug, summary, payload,
                evidence, base_updated_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fp, run_id, p["kind"], p["account_id"], p["slug"], p["summary"],
             json.dumps(p["payload"]), json.dumps(p["evidence"]), p["base_updated_at"], now()))
        if cur.rowcount:
            new += 1
        else:
            # A proposal that FAILED to apply (e.g. stale data) is re-queued with
            # fresh evidence. Approved, rejected and applied ones are never touched.
            retried = conn.execute(
                """UPDATE proposals SET status='pending', run_id=?, evidence=?, payload=?,
                   base_updated_at=? WHERE fingerprint=? AND status='failed'""",
                (run_id, json.dumps(p["evidence"]), json.dumps(p["payload"]),
                 p["base_updated_at"], fp)).rowcount
            new += retried
            skipped += 1 - retried
    summary = {"proposals_found": len(proposals), "new": new,
               "already_seen": skipped, "confident_matches": len(confident)}
    conn.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?",
                 (now(), json.dumps(summary), run_id))
    conn.commit()
    (DATA / "confident_matches.json").write_text(json.dumps(confident, indent=2))
    return summary


def main():
    if "--scrape" in sys.argv:
        from scrape import scrape
        locations = scrape()
    else:
        locations = json.loads((DATA / "locations.json").read_text())

    accounts = get_all_accounts()
    print(f"[match] {len(locations)} locations, {len(accounts)} CRM accounts")
    print("[match] care_type values in CRM:",
          sorted({a.get("care_type") for a in accounts if a.get("care_type")}))

    proposals, confident = build_proposals(locations, accounts)
    for p in proposals:
        print(f"  [{p['kind']:9}] {p['summary']}")
    print(f"  ({len(confident)} confident matches, no change needed)")

    summary = save(proposals, confident)
    print(f"\n[match] {summary}")


if __name__ == "__main__":
    main()
