"""
common.py - shared config, CRM API client, and database helpers.
Every other script imports from here, so settings live in one place.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------------------------------------------ config
HOST = "https://analyst-assessment-production.up.railway.app"
BASE = f"{HOST}/api/v1"
TOKEN = os.environ.get("CRM_TOKEN")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"   # "Bellhaven Senior Living (Parent Account)"

# What happens to accounts under Bellhaven that the website no longer lists.
# We don't know who owns them now, so we flag them rather than guess a new parent.
ORPHAN_STATUS = "Needs Review"

# The website and the CRM name care types differently. Translate website -> CRM.
CARE_MAP = {
    "assisted living": "Assisted Living",
    "independent living": "Independent Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
}
# care_type holds ONE value. If a community offers several, write the
# highest-acuity one (e.g. Assisted Living + Memory Support -> Memory Care).
CARE_PRIORITY = ["Skilled Nursing", "Memory Care", "Assisted Living", "Independent Living"]


def crm_care_types(offerings):
    """Website offerings -> set of CRM care_type values."""
    return {CARE_MAP.get(o.strip().lower(), o) for o in offerings}


def primary_care_type(offerings):
    mapped = crm_care_types(offerings)
    for c in CARE_PRIORITY:
        if c in mapped:
            return c
    return sorted(mapped)[0] if mapped else ""

DATA = Path("data")
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "pipeline.db"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ------------------------------------------------------------ CRM API
def _check(r):
    if not r.ok:
        raise RuntimeError(f"{r.request.method} {r.url} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def get_all_accounts(page_size=50):
    accounts, page = [], 1
    while True:
        r = requests.get(f"{BASE}/accounts", headers=HEADERS,
                         params={"page": page, "page_size": page_size}, timeout=30)
        body = _check(r)
        batch = body if isinstance(body, list) else next(
            body[k] for k in ("items", "data", "results", "accounts") if k in body)
        if not batch:
            break
        accounts.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return accounts


def get_account(account_id):
    return _check(requests.get(f"{BASE}/accounts/{account_id}", headers=HEADERS, timeout=30))


def patch_account(account_id, fields):
    return _check(requests.patch(f"{BASE}/accounts/{account_id}", headers=HEADERS,
                                 json=fields, timeout=30))


def create_account(fields):
    return _check(requests.post(f"{BASE}/accounts", headers=HEADERS, json=fields, timeout=30))


def num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def needs_chow(account):
    """The billing SOP: revenue history AND outstanding AR -> preserve old account."""
    return num(account.get("lifetime_revenue")) > 0 and num(account.get("outstanding_ar")) > 0


# ------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,          -- same finding => same fingerprint => never re-proposed
    run_id INTEGER,
    kind TEXT,                        -- update | reparent | create | duplicate | orphan
    account_id TEXT,
    location_slug TEXT,
    summary TEXT,
    payload TEXT,                     -- JSON: what we intend to write
    evidence TEXT,                    -- JSON: why
    base_updated_at TEXT,             -- account's updated_at when proposed (stale check)
    status TEXT DEFAULT 'pending',    -- pending | approved | rejected | applied | failed
    created_at TEXT, decided_at TEXT, applied_at TEXT,
    result TEXT                       -- JSON: API responses / error
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load(row, field):
    return json.loads(row[field]) if row[field] else None
