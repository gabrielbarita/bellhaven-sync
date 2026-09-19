"""
explore.py - Step 0 of the Bellhaven pipeline.

Pulls every CRM account (handling pagination), saves the API spec and the
website HTML, and prints a summary so you can spot the data traps before
writing any matching logic.

Usage:
    pip install requests beautifulsoup4
    python explore.py

Outputs (in ./data/):
    accounts.json   - every CRM account
    openapi.json    - the API spec (if found)
    site.html       - raw website HTML
"""

import json
import os
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HOST = "https://analyst-assessment-production.up.railway.app"
BASE = f"{HOST}/api/v1"
TOKEN = os.environ.get("CRM_TOKEN")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
OUT = Path("data")
OUT.mkdir(exist_ok=True)


# ---------------------------------------------------------------- API spec
def fetch_spec():
    """Swagger UI loads its spec from a JSON file. Try the usual locations."""
    for path in ["/api/openapi.json", "/openapi.json", "/api/v1/openapi.json"]:
        r = requests.get(HOST + path, headers=HEADERS, timeout=30)
        if r.ok and "paths" in r.text:
            spec = r.json()
            (OUT / "openapi.json").write_text(json.dumps(spec, indent=2))
            print(f"[spec] found at {path}")
            for p, methods in spec["paths"].items():
                for m, info in methods.items():
                    params = [x["name"] for x in info.get("parameters", [])]
                    print(f"  {m.upper():6} {p}  params={params}")
            return spec
    print("[spec] not found - check the link under the title on /api/docs")
    return None


# ---------------------------------------------------------------- accounts
def extract_items(body):
    """APIs wrap lists differently. Handle a bare list or common wrapper keys."""
    if isinstance(body, list):
        return body, None
    for key in ("items", "data", "results", "accounts"):
        if key in body:
            nxt = body.get("next") or body.get("next_cursor") or body.get("next_page")
            return body[key], nxt
    raise ValueError(f"Unknown response shape, keys: {list(body.keys())}")


def get_all_accounts(page_size=50):
    """The API paginates with page + page_size (default 50)."""
    accounts, page = [], 1
    while True:
        r = requests.get(f"{BASE}/accounts", headers=HEADERS,
                         params={"page": page, "page_size": page_size}, timeout=30)
        r.raise_for_status()
        batch, _ = extract_items(r.json())
        if not batch:
            break
        accounts.extend(batch)
        if len(batch) < page_size:  # short page = last page
            break
        page += 1
    (OUT / "accounts.json").write_text(json.dumps(accounts, indent=2))
    print(f"\n[crm] pulled {len(accounts)} accounts (expecting ~120)")
    return accounts


# ---------------------------------------------------------------- summary
def summarize(accounts):
    print("\n[crm] first account, in full:")
    print(json.dumps(accounts[0], indent=2))

    candidates = ("id", "account_id", "uuid", "crm_id", "external_id")
    id_key = next((k for k in candidates if k in accounts[0]), None)
    if id_key is None:
        print("\n[crm] couldn't find an ID field - look at the account above")
        return
    print(f"\n[crm] ID field is '{id_key}'")
    by_id = {a[id_key]: a for a in accounts}

    print("\n[crm] fields on an account:")
    print("  ", sorted(accounts[0].keys()))

    print("\n[crm] status values:", Counter(a.get("status") for a in accounts))

    # Anything that looks like a Bellhaven parent (there may be more than one!)
    parents = Counter(a.get("parent_id") for a in accounts if a.get("parent_id"))
    print("\n[crm] parent accounts by number of children:")
    for pid, n in parents.most_common(10):
        name = by_id.get(pid, {}).get("name", "<parent not in CRM>")
        print(f"  {pid}: {name}  ({n} children)")

    bh = [a for a in accounts if "bellhaven" in str(a.get("name", "")).lower()]
    print(f"\n[crm] {len(bh)} accounts with 'bellhaven' in the name:")
    for a in bh:
        parent = by_id.get(a.get("parent_id"), {}).get("name", a.get("parent_id"))
        print(f"  {a[id_key]}: {a.get('name')} | parent={parent} | status={a.get('status')}")

    # SOP check: revenue history AND outstanding AR > 0 -> CHOW path
    def num(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    chow = [a for a in accounts
            if num(a.get("lifetime_revenue")) > 0 and num(a.get("outstanding_ar")) > 0]
    print(f"\n[crm] {len(chow)} accounts with revenue AND AR > 0 (CHOW if re-parented):")
    for a in chow:
        print(f"  {a[id_key]}: {a.get('name')}  rev={a.get('lifetime_revenue')} "
              f"ar={a.get('outstanding_ar')}")

    # Possible duplicates: same zip + similar-ish name start
    keys = Counter((str(a.get("zip", ""))[:5], str(a.get("name", "")).lower()[:12])
                   for a in accounts)
    dupes = [k for k, n in keys.items() if n > 1]
    print(f"\n[crm] {len(dupes)} possible duplicate groups (same zip + name prefix):")
    for k in dupes:
        print("  ", k)


# ---------------------------------------------------------------- website
def fetch_site():
    r = requests.get(HOST, timeout=30)
    (OUT / "site.html").write_text(r.text)
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    print(f"\n[site] {len(r.text)} bytes of HTML, {len(text)} chars of visible text")
    print("[site] links:", sorted({a["href"] for a in soup.find_all("a", href=True)})[:30])
    if len(text) < 500:
        print("[site] very little text -> page is probably JS-rendered; "
              "check the browser Network tab for a JSON endpoint")
    else:
        print("[site] preview:", text[:500])


if __name__ == "__main__":
    fetch_spec()
    accts = get_all_accounts()
    if accts:
        summarize(accts)
    fetch_site()
