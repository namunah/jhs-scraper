#!/usr/bin/env python3
"""
Resumable scraper for villageinfo.in — single state, full village detail.

Hierarchy on the site:
    /<state>/                          -> list of districts
    /<state>/<district>/               -> list of subdivisions
    /<state>/<district>/<subdivision>/ -> list of villages (name, category, gram panchayat)
    /<state>/<district>/<subdivision>/<village>/ -> full village detail page

Usage:
    pip install requests beautifulsoup4 --break-system-packages

    python3 scrape_jharkhand.py                 # run/resume the full crawl
    python3 scrape_jharkhand.py --test           # only crawl ONE district, to sanity check parsing
    python3 scrape_jharkhand.py --export out.csv # dump current DB contents to CSV (no crawling)
    python3 scrape_jharkhand.py --stats          # show progress counts

Run it inside tmux/screen or with nohup so it survives the Termux app being
backgrounded, e.g.:
    nohup python3 scrape_jharkhand.py > crawl.log 2>&1 &

It is safe to Ctrl+C and re-run any time — already-scraped villages are
skipped (checked against the SQLite DB) so nothing is re-fetched.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import random
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://villageinfo.in"
STATE_SLUG = "jharkhand"
STATE_URL = f"{BASE}/{STATE_SLUG}/"

DB_PATH = "jharkhand_villages.db"
LOG_PATH = "crawl.log"

# Be polite: delay range (seconds) between requests, plus retry/backoff settings.
DELAY_MIN = 1.2
DELAY_MAX = 2.5
MAX_RETRIES = 4
TIMEOUT = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-research-script/1.0; contact: n/a)"
}

session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------------------------------
# DB setup
# --------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS districts (
            url TEXT PRIMARY KEY,
            name TEXT,
            done INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subdivisions (
            url TEXT PRIMARY KEY,
            district_url TEXT,
            name TEXT,
            done INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS villages (
            url TEXT PRIMARY KEY,
            subdivision_url TEXT,
            name TEXT,
            category TEXT,
            gram_panchayat_hint TEXT,
            scraped INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS village_details (
            url TEXT PRIMARY KEY,
            state TEXT,
            district TEXT,
            subdivision TEXT,
            village_name TEXT,
            village_code TEXT,
            pincode TEXT,
            gram_panchayat TEXT,
            area_hectares TEXT,
            population_total TEXT,
            population_male TEXT,
            population_female TEXT,
            households TEXT,
            child_population TEXT,
            literate_population TEXT,
            illiterate_population TEXT,
            sc_population TEXT,
            st_population TEXT,
            nearest_railway TEXT,
            nearest_airport TEXT,
            raw_json TEXT
        )
    """)
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def get_soup(url):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            elif resp.status_code == 404:
                log(f"404 Not Found: {url}")
                return None
            else:
                last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        wait = (2 ** attempt) + random.uniform(0, 1)
        log(f"Retry {attempt}/{MAX_RETRIES} for {url} ({last_err}); waiting {wait:.1f}s")
        time.sleep(wait)
    log(f"FAILED after {MAX_RETRIES} retries: {url} ({last_err})")
    return None


# --------------------------------------------------------------------------
# Link extraction (depth-based, robust to markup changes)
# --------------------------------------------------------------------------

def path_depth(url):
    """Number of non-empty path segments after the domain."""
    path = url.replace(BASE, "").strip("/")
    if not path:
        return 0
    return len(path.split("/"))


def extract_child_links(soup, current_url, expected_depth):
    """Find <a> tags in the main content whose href is a same-site path at
    exactly expected_depth, and is a descendant of current_url's path."""
    links = {}
    cur_path = current_url.replace(BASE, "").strip("/")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(BASE + "/", href)
        if not full.startswith(BASE):
            continue
        full_path = full.replace(BASE, "").strip("/")
        if not full_path.startswith(cur_path + "/") if cur_path else False:
            # still allow top-level (state page) case
            if cur_path and not full_path.startswith(cur_path + "/"):
                continue
        if path_depth(full) != expected_depth:
            continue
        # normalize with trailing slash
        norm = full.rstrip("/") + "/"
        name = a.get_text(strip=True)
        if norm not in links:
            links[norm] = name
    return links


# --------------------------------------------------------------------------
# Step 1: districts
# --------------------------------------------------------------------------

def crawl_districts(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM districts")
    if c.fetchone()[0] > 0:
        log("Districts already loaded, skipping fetch of state page.")
        return
    log(f"Fetching state page: {STATE_URL}")
    soup = get_soup(STATE_URL)
    if soup is None:
        log("Could not load state page — aborting.")
        sys.exit(1)
    links = extract_child_links(soup, STATE_URL, expected_depth=2)
    for url, name in links.items():
        c.execute("INSERT OR IGNORE INTO districts (url, name) VALUES (?, ?)", (url, name))
    conn.commit()
    log(f"Found {len(links)} districts.")


# --------------------------------------------------------------------------
# Step 2: subdivisions
# --------------------------------------------------------------------------

def crawl_subdivisions(conn, limit_districts=None):
    c = conn.cursor()
    c.execute("SELECT url, name FROM districts WHERE done = 0")
    districts = c.fetchall()
    if limit_districts:
        districts = districts[:limit_districts]

    for durl, dname in districts:
        log(f"District: {dname} ({durl})")
        soup = get_soup(durl)
        polite_sleep()
        if soup is None:
            continue
        links = extract_child_links(soup, durl, expected_depth=3)
        for url, name in links.items():
            c.execute(
                "INSERT OR IGNORE INTO subdivisions (url, district_url, name) VALUES (?, ?, ?)",
                (url, durl, name),
            )
        c.execute("UPDATE districts SET done = 1 WHERE url = ?", (durl,))
        conn.commit()
        log(f"  -> {len(links)} subdivisions")


# --------------------------------------------------------------------------
# Step 3: villages (list page has name/category/gram panchayat already)
# --------------------------------------------------------------------------

def parse_village_list_table(soup):
    """Find the table with columns like Village Name / Category / Gram Panchayat."""
    results = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [td.get_text(strip=True).lower() for td in first_row.find_all("td")]
        if any("village" in h for h in headers) and any("panchayat" in h or "category" in h for h in headers):
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if not cells:
                    continue
                link = row.find("a", href=True)
                if not link:
                    continue
                url = urljoin(BASE + "/", link["href"]).rstrip("/") + "/"
                name = link.get_text(strip=True)
                texts = [td.get_text(strip=True) for td in cells]
                category = texts[2] if len(texts) > 2 else ""
                gp = texts[3] if len(texts) > 3 else ""
                results.append((url, name, category, gp))
            break
    return results


def crawl_villages(conn):
    c = conn.cursor()
    c.execute("SELECT url FROM subdivisions WHERE done = 0")
    subs = [r[0] for r in c.fetchall()]

    for surl in subs:
        soup = get_soup(surl)
        polite_sleep()
        if soup is None:
            continue
        rows = parse_village_list_table(soup)
        if not rows:
            # fallback: depth-based link extraction
            links = extract_child_links(soup, surl, expected_depth=path_depth(surl) + 1)
            rows = [(url, name, "", "") for url, name in links.items()]
        for url, name, category, gp in rows:
            c.execute(
                "INSERT OR IGNORE INTO villages (url, subdivision_url, name, category, gram_panchayat_hint) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, surl, name, category, gp),
            )
        c.execute("UPDATE subdivisions SET done = 1 WHERE url = ?", (surl,))
        conn.commit()
        log(f"Subdivision {surl} -> {len(rows)} villages")


# --------------------------------------------------------------------------
# Step 4: village detail pages
# --------------------------------------------------------------------------

NUM_RE = re.compile(r"[\d,]+")


def clean_num(s):
    if not s:
        return None
    m = NUM_RE.search(s.replace(",", ""))
    return m.group(0) if m else s.strip()


def parse_village_detail(soup):
    """Generic parser: walk headings + tables in document order, build a
    nested dict, and also pull out well-known fields for flat columns."""
    structured = {}
    current_heading = "top"
    structured[current_heading] = []

    for el in soup.find_all(["h2", "table"]):
        if el.name == "h2":
            current_heading = el.get_text(strip=True)
            structured.setdefault(current_heading, [])
        elif el.name == "table":
            rows = el.find_all("tr")
            parsed_rows = []
            for row in rows:
                cells = row.find_all(["td", "th"])
                texts = [c.get_text(" ", strip=True) for c in cells]
                if texts:
                    parsed_rows.append(texts)
            if not parsed_rows:
                continue

            # Some tables (e.g. "<Village> - Overview", "Travel to <Village>")
            # carry their own title as the table's FIRST row rather than a
            # preceding <h2>. Detect that: first row has exactly one
            # non-empty cell -> treat it as this table's own heading and
            # parse the remaining rows under that heading instead.
            table_heading = current_heading
            data_rows = parsed_rows
            first_row = parsed_rows[0]
            non_empty = [c for c in first_row if c.strip()]
            if len(non_empty) == 1:
                table_heading = non_empty[0]
                structured.setdefault(table_heading, [])
                data_rows = parsed_rows[1:]

            if not data_rows:
                continue
            # 2-column table -> key:value dict
            if all(len(r) == 2 for r in data_rows):
                kv = {r[0]: r[1] for r in data_rows}
                structured.setdefault(table_heading, []).append(kv)
            else:
                structured.setdefault(table_heading, []).append(data_rows)

    flat = {
        "village_code": None,
        "pincode": None,
        "gram_panchayat": None,
        "area_hectares": None,
        "population_total": None,
        "population_male": None,
        "population_female": None,
        "households": None,
        "child_population": None,
        "literate_population": None,
        "illiterate_population": None,
        "sc_population": None,
        "st_population": None,
        "nearest_railway": None,
        "nearest_airport": None,
    }

    # Overview table (heading varies, e.g. "<Name> - Overview")
    for heading, blocks in structured.items():
        if "overview" in heading.lower():
            for b in blocks:
                if isinstance(b, dict):
                    for k, v in b.items():
                        kl = k.lower().replace(" ", "")
                        if "pincode" in kl or "pin:" in kl:
                            flat["pincode"] = clean_num(v)
                        elif "grampanchayat" in kl:
                            flat["gram_panchayat"] = v
                        elif "geographicalarea" in kl or "totalarea" in kl:
                            flat["area_hectares"] = v
                        elif "railway" in kl:
                            flat["nearest_railway"] = v
                        elif "airport" in kl:
                            flat["nearest_airport"] = v

    # Population table: multi-row, columns [Category, Total, Male, Female]
    for heading, blocks in structured.items():
        if "population" in heading.lower():
            for b in blocks:
                if isinstance(b, list):
                    for row in b:
                        if len(row) < 4:
                            continue
                        label = row[0].lower()
                        total, male, female = row[1], row[2], row[3]
                        if "total population" in label:
                            flat["population_total"] = clean_num(total)
                            flat["population_male"] = clean_num(male)
                            flat["population_female"] = clean_num(female)
                        elif "child population" in label:
                            flat["child_population"] = clean_num(total)
                        elif "scheduled caste" in label or label.strip() == "sc":
                            flat["sc_population"] = clean_num(total)
                        elif "scheduled tribe" in label or label.strip() == "st":
                            flat["st_population"] = clean_num(total)
                        elif "literate population" in label and "illiterate" not in label:
                            flat["literate_population"] = clean_num(total)
                        elif "illiterate population" in label:
                            flat["illiterate_population"] = clean_num(total)

    # Households + village code often appear only in the narrative text
    body_text = soup.get_text(" ", strip=True)
    m = re.search(r"village code of [\w\s]+ is (\d+)", body_text, re.I)
    if m:
        flat["village_code"] = m.group(1)
    m = re.search(r"has (\d[\d,]*) households", body_text, re.I)
    if m:
        flat["households"] = clean_num(m.group(1))

    return flat, structured


def crawl_village_details(conn, limit=None):
    c = conn.cursor()
    c.execute("SELECT url, name, subdivision_url FROM villages WHERE scraped = 0")
    todo = c.fetchall()
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} villages left to scrape in detail.")

    for i, (url, name, surl) in enumerate(todo, 1):
        soup = get_soup(url)
        polite_sleep()
        if soup is None:
            continue
        flat, structured = parse_village_detail(soup)

        # derive state/district/subdivision names from the URL path
        parts = url.replace(BASE, "").strip("/").split("/")
        state = parts[0] if len(parts) > 0 else STATE_SLUG
        district = parts[1] if len(parts) > 1 else ""
        subdivision = parts[2] if len(parts) > 2 else ""

        c.execute(
            """INSERT OR REPLACE INTO village_details (
                url, state, district, subdivision, village_name, village_code,
                pincode, gram_panchayat, area_hectares, population_total,
                population_male, population_female, households,
                child_population, literate_population, illiterate_population,
                sc_population, st_population, nearest_railway, nearest_airport,
                raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                url, state, district, subdivision, name, flat["village_code"],
                flat["pincode"], flat["gram_panchayat"], flat["area_hectares"],
                flat["population_total"], flat["population_male"], flat["population_female"],
                flat["households"], flat["child_population"], flat["literate_population"],
                flat["illiterate_population"], flat["sc_population"], flat["st_population"],
                flat["nearest_railway"], flat["nearest_airport"],
                json.dumps(structured, ensure_ascii=False),
            ),
        )
        c.execute("UPDATE villages SET scraped = 1 WHERE url = ?", (url,))
        conn.commit()

        if i % 25 == 0:
            log(f"  ...{i}/{len(todo)} done")

    log("Village detail crawl complete.")


# --------------------------------------------------------------------------
# Stats / export
# --------------------------------------------------------------------------

def show_stats(conn):
    c = conn.cursor()
    for table in ["districts", "subdivisions", "villages", "village_details"]:
        c.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"{table}: {c.fetchone()[0]}")
    c.execute("SELECT COUNT(*) FROM villages WHERE scraped = 0")
    print(f"villages remaining to scrape: {c.fetchone()[0]}")


def export_csv(conn, path):
    c = conn.cursor()
    c.execute("SELECT * FROM village_details")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Only crawl 1 district as a sanity check")
    parser.add_argument("--stats", action="store_true", help="Show progress counts and exit")
    parser.add_argument("--export", metavar="FILE.csv", help="Export village_details to CSV and exit")
    args = parser.parse_args()

    conn = init_db()

    if args.stats:
        show_stats(conn)
        return
    if args.export:
        export_csv(conn, args.export)
        return

    crawl_districts(conn)
    crawl_subdivisions(conn, limit_districts=1 if args.test else None)
    crawl_villages(conn)
    crawl_village_details(conn, limit=30 if args.test else None)

    show_stats(conn)


if __name__ == "__main__":
    main()
