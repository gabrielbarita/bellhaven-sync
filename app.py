"""
app.py - Step 3: review app.

    pip3 install flask
    python3 app.py        # then open http://127.0.0.1:5000

Every proposal shows its evidence. Approve -> apply.py writes it to the CRM.
Reject -> recorded, never re-proposed. Nothing writes without a click.
"""

import json

from flask import Flask, redirect, render_template_string, request, url_for

from apply import apply_proposal
from common import BELLHAVEN_PARENT_ID, crm_care_types, db, load, now

app = Flask(__name__)

KIND_ORDER = ["reparent", "duplicate", "create", "update", "orphan"]
KIND_LABEL = {
    "reparent": "Wrong parent", "duplicate": "Duplicate", "create": "Missing from CRM",
    "update": "Outdated fields", "orphan": "Not on website",
}
COMPARE_FIELDS = [("name", "name"), ("billing_street", "street"), ("billing_city", "city"),
                  ("billing_state", "state"), ("billing_zip", "zip"),
                  ("care_type", "care_offerings")]

PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven CRM review</title>
<style>
 body{font:14px/1.45 -apple-system,system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d2330;background:#f6f7f9}
 h1{margin:0 0 4px} .muted{color:#6b7280} a{color:#2451b7}
 .tabs a{margin-right:14px;text-decoration:none} .tabs a.on{font-weight:700;border-bottom:2px solid #2451b7}
 .card{background:#fff;border:1px solid #e3e6eb;border-radius:10px;padding:14px 16px;margin:12px 0}
 .kind{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;background:#eef1f6;margin-right:8px}
 .reparent{background:#fde7d9}.duplicate{background:#f3e3fb}.create{background:#dff3e4}.orphan{background:#fdf1c7}.update{background:#e1ecfd}
 table{border-collapse:collapse;width:100%;margin-top:8px;font-size:13px}
 td,th{border-bottom:1px solid #eef0f3;padding:5px 8px;text-align:left;vertical-align:top}
 th{color:#6b7280;font-weight:600;width:140px} tr.diff td{background:#fff6d6}
 .sop{margin-top:10px;padding:8px 10px;border-radius:8px;background:#fff2f0;border:1px solid #f5c2bb}
 .sop.ok{background:#eefaf1;border-color:#bfe5c9}
 .btns{margin-top:12px} button{font:inherit;padding:6px 14px;border-radius:7px;border:1px solid #cfd4dc;cursor:pointer;margin-right:6px}
 .approve{background:#1f8a4c;color:#fff;border-color:#1f8a4c} .reject{background:#fff}
 details{margin-top:8px} pre{background:#f3f4f6;padding:8px;border-radius:6px;overflow-x:auto;font-size:12px}
 .status{float:right;font-size:12px;font-weight:600} .applied{color:#1f8a4c}.failed{color:#c0392b}.rejected{color:#6b7280}
 .flash{background:#e1ecfd;padding:8px 12px;border-radius:8px}
</style></head><body>
<h1>Bellhaven CRM review</h1>
<div class="muted">Last run: {{ run.finished_at if run else "never" }}
 {% if run %}· {{ run_summary }}{% endif %}</div>
<p class="tabs">
 {% for s in ["pending","applied","rejected","failed"] %}
  <a href="{{ url_for('index', status=s) }}" class="{{ 'on' if s==status }}">{{ s|capitalize }} ({{ counts.get(s,0) }})</a>
 {% endfor %}
</p>
{% if msg %}<div class="flash">{{ msg }}</div>{% endif %}
{% if not items %}<p class="muted">Nothing here.</p>{% endif %}

{% for p in items %}
<div class="card" id="p{{ p.id }}">
 <span class="status {{ p.status }}">{{ p.status }}</span>
 <span class="kind {{ p.kind }}">{{ labels[p.kind] }}</span><b>{{ p.summary }}</b>
 <div class="muted">#{{ p.id }} · account {{ p.account_id or "(new)" }} · proposed {{ p.created_at }}</div>

 {% if p.compare %}
 <table><tr><th>Field</th><th>CRM now</th><th>Website / proposed</th></tr>
  {% for row in p.compare %}
   <tr class="{{ 'diff' if row.diff }}"><th>{{ row.field }}</th><td>{{ row.crm }}</td><td>{{ row.site }}</td></tr>
  {% endfor %}
 </table>
 {% endif %}

 {% if p.sop %}
 <div class="sop {{ '' if p.sop.path.startswith('CHOW') else 'ok' }}">
  <b>Billing SOP:</b> lifetime revenue {{ p.sop.lifetime_revenue }}, outstanding AR {{ p.sop.outstanding_ar }}
  → <b>{{ p.sop.path }}</b> <span class="muted">(re-checked against live data on approve)</span>
 </div>
 {% endif %}

 {% if p.extra %}<div style="margin-top:8px">{{ p.extra|safe }}</div>{% endif %}

 <details><summary>Full evidence and payload</summary>
  <b>Will write:</b><pre>{{ p.payload_json }}</pre>
  <b>Evidence:</b><pre>{{ p.evidence_json }}</pre>
  {% if p.result_json %}<b>Result:</b><pre>{{ p.result_json }}</pre>{% endif %}
 </details>

 {% if p.status in ["pending","failed"] %}
 <form class="btns" method="post" action="{{ url_for('decide', pid=p.id) }}">
  <button class="approve" name="decision" value="approve">{{ "Retry" if p.status=="failed" else "Approve & write to CRM" }}</button>
  <button class="reject" name="decision" value="reject">Reject</button>
 </form>
 {% endif %}
</div>
{% endfor %}
</body></html>
"""


def fmt(v):
    if isinstance(v, list):
        return "; ".join(v)
    return "" if v is None else v


def build_view(row):
    p = dict(row)
    payload, ev = load(row, "payload"), load(row, "evidence")
    p["payload_json"] = json.dumps(payload, indent=2)
    p["evidence_json"] = json.dumps(ev, indent=2)
    p["result_json"] = json.dumps(load(row, "result"), indent=2) if row["result"] else ""
    p["sop"], p["compare"], p["extra"] = ev.get("sop"), [], ""
    loc = ev.get("location")

    if p["kind"] in ("update", "reparent", "create") and loc:
        acct = ev.get("account", {})
        for crm_f, site_f in COMPARE_FIELDS:
            crm, site = fmt(acct.get(crm_f)), fmt(loc.get(site_f))
            diff = str(crm).strip().lower() != str(site).strip().lower()
            if crm_f == "care_type":
                ok = crm_care_types(loc["care_offerings"])
                diff = acct.get("care_type") not in ok
                site = f"{site}  (CRM terms: {', '.join(sorted(ok))})"
            p["compare"].append({"field": crm_f, "crm": crm if acct else "—", "site": site,
                                 "diff": diff})
        if acct:
            parent_ok = acct.get("parent_id") == BELLHAVEN_PARENT_ID
            p["compare"].append({"field": "parent", "crm": acct.get("parent_name") or "(none)",
                                 "site": "Bellhaven Senior Living", "diff": not parent_ok})
            p["compare"].append({"field": "status", "crm": acct.get("status"),
                                 "site": "Active", "diff": acct.get("status") != "Active"})
            s = ev.get("score", {})
            p["extra"] = (f"<span class='muted'>Match: {s.get('tier')} · street sim "
                          f"{s.get('street_sim')} · name sim {s.get('name_sim')} · "
                          f"source <a href='{loc['url']}' target=_blank>{loc['url']}</a>"
                          f"{' · found only on homepage' if loc.get('found_via')=='homepage_only' else ''}</span>")
        else:
            near = ev.get("closest_non_matches", [])
            p["extra"] = "<b>Closest CRM accounts (rejected as matches):</b><br>" + "<br>".join(
                f"{n['account']['name']} — {n['account']['billing_street']}, "
                f"{n['account']['billing_city']} (name sim {n['score']['name_sim']})" for n in near)

    elif p["kind"] == "duplicate":
        lo, su = ev["loser"], ev["survivor"]
        for f in ["account_id", "name", "parent_name", "billing_street", "billing_zip", "status",
                  "lifetime_revenue", "outstanding_ar", "phone"]:
            p["compare"].append({"field": f, "crm": fmt(lo.get(f)), "site": fmt(su.get(f)),
                                 "diff": lo.get(f) != su.get(f)})
        p["extra"] = ("<span class='muted'>Left: copy to retire (Inactive + duplicate_of_account). "
                      "Right: surviving account. Both match the same website address.</span>")

    elif p["kind"] == "orphan":
        a = ev["account"]
        p["extra"] = (f"<b>{a['name']}</b> — {a.get('billing_street')}, {a.get('billing_city')}, "
                      f"{a.get('billing_state')} · revenue {a.get('lifetime_revenue')} · "
                      f"AR {a.get('outstanding_ar')}<br><span class='muted'>Closest website "
                      f"locations: " + "; ".join(f"{c['name']} ({c['city']}, name sim {c['name_sim']})"
                                                 for c in ev.get("closest_locations", [])) + "</span>")
    return p


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    conn = db()
    rows = conn.execute("SELECT * FROM proposals WHERE status=?", (status,)).fetchall()
    rows = sorted(rows, key=lambda r: (KIND_ORDER.index(r["kind"]), r["id"]))
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())
    counts["applied"] = counts.get("applied", 0)
    run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return render_template_string(
        PAGE, items=[build_view(r) for r in rows], status=status, counts=counts,
        labels=KIND_LABEL, run=run, run_summary=run["summary"] if run else "",
        msg=request.args.get("msg"))


@app.route("/decide/<int:pid>", methods=["POST"])
def decide(pid):
    conn = db()
    if request.form["decision"] == "reject":
        conn.execute("UPDATE proposals SET status='rejected', decided_at=? WHERE id=?", (now(), pid))
        conn.commit()
        msg = f"#{pid} rejected - it will not be proposed again."
    else:
        conn.execute("UPDATE proposals SET status='approved', decided_at=? WHERE id=?", (now(), pid))
        conn.commit()
        status, result = apply_proposal(pid)
        msg = (f"#{pid} written to CRM." if status == "applied"
               else f"#{pid} FAILED: {result.get('error')}")
    return redirect(url_for("index", status="pending", msg=msg))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
