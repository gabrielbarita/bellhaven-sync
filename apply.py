"""
apply.py - Step 4: write ONE approved proposal to the CRM.

Only called from the review app when a reviewer clicks Approve.
The billing SOP is enforced here, against the account's LIVE state,
not the snapshot taken when the proposal was made.
"""

import json

from common import (BELLHAVEN_PARENT_ID, create_account, db, get_account, load,
                    needs_chow, now, patch_account)

# Fields that, if they changed since we proposed, mean our evidence is stale
GUARD_FIELDS = ["parent_id", "status", "lifetime_revenue", "outstanding_ar",
                "chow_current_account", "duplicate_of_account"]


class StaleProposal(Exception):
    pass


def check_not_stale(live, evidence):
    snap = evidence.get("account") or evidence.get("loser") or {}
    changed = [f for f in GUARD_FIELDS + list(evidence.get("before", {}))
               if f in snap and str(snap.get(f) or "") != str(live.get(f) or "")]
    if changed:
        raise StaleProposal(f"CRM changed since this was proposed ({', '.join(changed)}). "
                            f"Re-run match.py to get a fresh proposal.")


def apply_reparent(p, payload, evidence):
    old = get_account(p["account_id"])

    # Retry after a partial CHOW (new account created, link step failed):
    # reuse the account we already created instead of making a second one.
    prior = json.loads(p["result"]) if p["result"] else {}
    if prior.get("chow_new_account_id"):
        new_id = prior["chow_new_account_id"]
        linked = patch_account(old["account_id"], {"chow_current_account": new_id})
        return {"path": "CHOW (resumed)", "chow_new_account_id": new_id, "old_account": linked}

    check_not_stale(old, evidence)

    if needs_chow(old):
        # SOP: revenue history AND open AR -> do NOT touch the old account's parent.
        # Create a new account under Bellhaven and point the old one at it.
        new_fields = dict(payload["new_account"])
        new_fields["note"] = (f"Created via change of ownership from {old['account_id']} "
                              f"(previously under {old.get('parent_name') or 'no parent'}).")
        new = create_account(new_fields)
        # Record the new id immediately, so a failure below can be resumed safely
        conn = db()
        conn.execute("UPDATE proposals SET result=? WHERE id=?",
                     (json.dumps({"chow_new_account_id": new["account_id"]}), p["id"]))
        conn.commit()
        linked = patch_account(old["account_id"], {"chow_current_account": new["account_id"]})
        return {"path": "CHOW", "chow_new_account_id": new["account_id"],
                "new_account": new, "old_account": linked}

    # No revenue history or no open AR -> re-parent the existing account directly
    fields = {"parent_id": BELLHAVEN_PARENT_ID, **payload.get("field_changes", {})}
    return {"path": "re-parent", "account": patch_account(old["account_id"], fields)}


def apply_proposal(proposal_id):
    conn = db()
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if p is None or p["status"] not in ("approved", "failed"):
        raise ValueError(f"Proposal {proposal_id} is not approved")
    payload, evidence = load(p, "payload"), load(p, "evidence")

    try:
        if p["kind"] == "reparent":
            result = apply_reparent(p, payload, evidence)
        elif p["kind"] == "create":
            result = {"new_account": create_account(payload)}
        else:  # update | duplicate | orphan -> plain field changes on one account
            live = get_account(p["account_id"])
            check_not_stale(live, evidence)
            fields = payload.get("field_changes", payload)
            result = {"account": patch_account(p["account_id"], fields)}
        status = "applied"
    except Exception as e:  # keep the error so the reviewer can see it
        # Keep anything saved mid-way (e.g. the CHOW new account id) alongside the error
        saved = conn.execute("SELECT result FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        result = json.loads(saved["result"]) if saved and saved["result"] else {}
        result["error"] = str(e)
        status = "failed"

    conn.execute("UPDATE proposals SET status=?, applied_at=?, result=? WHERE id=?",
                 (status, now(), json.dumps(result), proposal_id))
    conn.commit()
    return status, result
