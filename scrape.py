"""
scrape.py - Step 1 of the Bellhaven pipeline.

Pulls every Bellhaven community from the website and saves them to
data/locations.json with: name, street, city, state, zip, care offerings,
phone, administrator, plus source URL and where the link was found.

How the site is laid out (checked by hand):
  /                     homepage - also links the newest community (Findlay),
                        which is NOT in the directory
  /communities?page=N   paginated directory, 16 per page, "Next ->" link
  /communities/<slug>   detail page: Address, Care Offerings, Administrator, Phone

Usage:
    python3 scrape.py
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "https://analyst-assessment-production.up.railway.app"
OUT = Path("data")
OUT.mkdir(exist_ok=True)

LABELS = ["Address", "Care Offerings", "Administrator", "Phone"]
# Known offerings, used only as a fallback if two offerings end up glued together
KNOWN_OFFERINGS = [
    "Short-Term Rehabilitation & Nursing",
    "Assisted Living",
    "Memory Support",
    "Independent Living",
    "Skilled Nursing",
]
CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
DETAIL_PATH = re.compile(r"^/communities/[^/?#]+/?$")

session = requests.Session()
session.headers["User-Agent"] = "bellhaven-crm-sync/1.0"


def get_soup(url, retries=3):
    """GET a page with simple retries, return parsed HTML."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"  retrying {url} ({e})")
            time.sleep(2 * (attempt + 1))


def detail_links(soup, base_url):
    """Every link on the page that points at a community detail page."""
    links = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        if urlparse(url).netloc == urlparse(HOST).netloc and DETAIL_PATH.match(urlparse(url).path):
            links.add(url.rstrip("/"))
    return links


# ------------------------------------------------------------ discovery
def discover():
    """
    Collect detail-page URLs from the homepage AND every directory page.
    Returns {url: "directory" | "homepage_only"} so we keep track of where
    each community was found (useful evidence in the review app).
    """
    found = {}

    # 1. Directory: follow "Next" links until there are none
    url, pages = f"{HOST}/communities", 0
    while url:
        soup = get_soup(url)
        pages += 1
        for link in detail_links(soup, url):
            found[link] = "directory"
        nxt = soup.find("a", string=re.compile(r"Next", re.I))
        url = urljoin(url, nxt["href"]) if nxt else None
        if pages > 50:  # safety valve against a pagination loop
            raise RuntimeError("Directory pagination did not end")
    print(f"[discover] {pages} directory pages, {len(found)} communities")

    # 2. Homepage: can link communities the directory doesn't list
    home = get_soup(HOST)
    for link in detail_links(home, HOST):
        if link not in found:
            found[link] = "homepage_only"
            print(f"[discover] found on homepage but not in directory: {link}")

    # The homepage states how many communities Bellhaven runs; use it as a check
    m = re.search(r"serve\s+(\d+)\s+communities", home.get_text(" ", strip=True))
    claimed = int(m.group(1)) if m else None
    return found, claimed


# ------------------------------------------------------------ detail page
def split_sections(lines):
    """Group the text lines under each label: {"Address": [...], "Phone": [...]}."""
    sections, current = {}, None
    for line in lines:
        if line in LABELS:
            current = line
            sections[current] = []
        elif line.startswith("←") or line.startswith("©"):
            current = None
        elif current:
            sections[current].append(line)
    return sections


def split_offerings(values):
    """Offerings are usually one per line; if some got glued together, split them."""
    out = []
    for v in values:
        remaining = v
        pieces = []
        for known in KNOWN_OFFERINGS:
            if known in remaining:
                pieces.append(known)
                remaining = remaining.replace(known, "")
        out.extend(pieces if pieces and not remaining.strip() else [v])
    return sorted(set(out))


def parse_detail(url):
    soup = get_soup(url)
    name = soup.find("h1").get_text(strip=True)
    # "\n" separator puts every tag and <br> on its own line
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    s = split_sections(lines)

    addr = s.get("Address", [])
    street, city, state, zip_ = " ".join(addr), "", "", ""
    if addr:
        m = CITY_STATE_ZIP.match(addr[-1])
        if m:
            street = " ".join(addr[:-1])
            city, state, zip_ = m["city"], m["state"], m["zip"]

    loc = {
        "slug": urlparse(url).path.rstrip("/").split("/")[-1],
        "url": url,
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_,
        "care_offerings": split_offerings(s.get("Care Offerings", [])),
        "administrator": " ".join(s.get("Administrator", [])),
        "phone": " ".join(s.get("Phone", [])),
    }
    problems = [k for k in ("street", "city", "state", "zip") if not loc[k]]
    if not loc["care_offerings"]:
        problems.append("care_offerings")
    loc["parse_problems"] = problems
    return loc


# ------------------------------------------------------------ main
def scrape():
    found, claimed = discover()
    locations = []
    for url in sorted(found):
        loc = parse_detail(url)
        loc["found_via"] = found[url]
        if loc["parse_problems"]:
            print(f"  WARNING {loc['name']}: could not parse {loc['parse_problems']}")
        locations.append(loc)
        time.sleep(0.2)  # be polite to the site

    locations.sort(key=lambda l: l["name"])
    (OUT / "locations.json").write_text(json.dumps(locations, indent=2))

    print(f"\n[scrape] {len(locations)} locations saved to data/locations.json")
    if claimed and claimed != len(locations):
        print(f"[scrape] WARNING homepage says {claimed} communities, scraped {len(locations)}")
    for l in locations:
        tag = "" if l["found_via"] == "directory" else "  <- homepage only"
        print(f"  {l['name']:<50} {l['street']}, {l['city']}, {l['state']} {l['zip']} "
              f"| {', '.join(l['care_offerings'])}{tag}")
    return locations


if __name__ == "__main__":
    scrape()
