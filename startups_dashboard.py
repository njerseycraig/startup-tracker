#!/usr/bin/env python3
"""
Bootstrapped Startup Tracker
=====================================================
Finds solo/bootstrapped startups doing $100K+/month from vetted sources.
Sources: Starter Story · Failory · Hacker News · Indie Hackers
Run daily via Task Scheduler. Results → startups.html

Usage:
  python startups_dashboard.py            # use 24h cache
  python startups_dashboard.py --refresh  # force re-fetch all sources
  python startups_dashboard.py --no-browser  # don't auto-open (for Task Scheduler)
"""

import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timedelta

# Force UTF-8 on Windows consoles so Unicode arrows/symbols print correctly
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from html import escape

import requests
import urllib3
from bs4 import BeautifulSoup

# SSL certs missing from this Python install — suppress the warning and use verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
SSL_VERIFY = False

# ── Configuration ─────────────────────────────────────────────────────────────

DIR           = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE    = os.path.join(DIR, "startup_cache.json")
CUSTOM_FILE   = os.path.join(DIR, "custom_startups.json")
OUTPUT_FILE   = os.path.join(DIR, "index.html")
CACHE_HOURS   = 24      # scraped sources refresh daily
MIN_MONTHLY   = 100_000  # $100K/month minimum

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}

# ── Revenue Utilities ─────────────────────────────────────────────────────────

def parse_monthly_revenue(text: str) -> float | None:
    """
    Extract monthly revenue from text. Handles:
      $120K/mo, $120k/month, $1.2M MRR, $1.5M/year, $1M ARR, 120k monthly
    Returns dollars per month, or None.
    """
    if not text:
        return None
    t = text.lower().replace(",", "").replace(" ", "")

    # Patterns: (regex, multiplier-for-K/M, divide-by-12-if-annual)
    patterns = [
        # Monthly with K/M
        (r'\$(\d+(?:\.\d+)?)b(?:/mo|/month|mrr|monthly)', 1e9, False),
        (r'\$(\d+(?:\.\d+)?)m(?:/mo|/month|mrr|monthly)', 1e6, False),
        (r'\$(\d+(?:\.\d+)?)k(?:/mo|/month|mrr|monthly)', 1e3, False),
        # Annual with K/M
        (r'\$(\d+(?:\.\d+)?)b(?:/yr|/year|arr|annually)', 1e9, True),
        (r'\$(\d+(?:\.\d+)?)m(?:/yr|/year|arr|annually)', 1e6, True),
        (r'\$(\d+(?:\.\d+)?)k(?:/yr|/year|arr|annually)', 1e3, True),
        # Plain with K/M (assume monthly)
        (r'\$(\d+(?:\.\d+)?)b\b', 1e9, False),
        (r'\$(\d+(?:\.\d+)?)m\b', 1e6, False),
        (r'\$(\d+(?:\.\d+)?)k\b', 1e3, False),
    ]

    for pat, mult, annual in patterns:
        m = re.search(pat, t)
        if m:
            try:
                val = float(m.group(1)) * mult
                return val / 12 if annual else val
            except (ValueError, AttributeError):
                pass
    return None


def fmt_mo(monthly: float) -> str:
    if monthly >= 1e6:
        return f"${monthly/1e6:.1f}M/mo"
    if monthly >= 1e3:
        return f"${monthly/1e3:.0f}K/mo"
    return f"${monthly:.0f}/mo"


def fmt_arr(monthly: float) -> str:
    arr = monthly * 12
    if arr >= 1e9:
        return f"${arr/1e9:.1f}B ARR"
    if arr >= 1e6:
        return f"${arr/1e6:.1f}M ARR"
    if arr >= 1e3:
        return f"${arr/1e3:.0f}K ARR"
    return f"${arr:.0f} ARR"


# ── Source: Starter Story ─────────────────────────────────────────────────────

def fetch_starter_story() -> list[dict]:
    """
    Scrape Starter Story for high-revenue bootstrapped case studies.
    Hits 3 pages: homepage (recent episodes), /explore (featured businesses),
    /episodes (all video interviews). Each <a> to /stories/ IS the card and
    contains: company name, article title, revenue display, and relative date.
    """
    print("  -> Starter Story...", end="", flush=True)
    results = []
    seen: set[str] = set()

    pages = [
        "https://www.starterstory.com/",
        "https://www.starterstory.com/explore",
        "https://www.starterstory.com/episodes",
    ]

    for page_url in pages:
        try:
            r = requests.get(page_url, headers=HEADERS, verify=SSL_VERIFY, timeout=25)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not any(seg in href for seg in ["/stories/", "/businesses/"]):
                    continue
                if href in seen:
                    continue
                seen.add(href)

                card_text = a.get_text(" ", strip=True)
                revenue = parse_monthly_revenue(card_text)
                if not revenue or revenue < MIN_MONTHLY:
                    continue

                lines = [ln.strip() for ln in a.get_text().splitlines() if ln.strip()]
                slug = href.rstrip("/").split("/")[-1]

                # Explore-page cards start with "category · location article_title..."
                # The company name only lives in the URL slug. Homepage cards start
                # directly with the company name. Distinguish by the first-line pattern.
                CATEGORY_WORDS = re.compile(
                    r'^(ecommerce|software|saas|service|tech|publish|media|watch|food'
                    r'|fitness|health|education|finance|travel|real.estate)', re.I
                )
                first_line_is_category = bool(lines and ("·" in lines[0] or CATEGORY_WORDS.match(lines[0])))

                if first_line_is_category:
                    company = slug.replace("-", " ").title()
                    # Article title: find the first sentence-start pattern in the card
                    # (after the "category · location" prefix). Common starters: I, How, My,
                    # Building, From, Why, Making, Growing, etc.
                    title_m = re.search(
                        r'\b(I |How |My |Building |From |Why |Making |Growing |We |The )',
                        card_text
                    )
                    if title_m:
                        article_title = card_text[title_m.start():].split("$")[0].strip()[:200]
                        # Append the revenue mention back
                        rev_m = re.search(r'\$[\d.]+[KkMm][^\s]*', card_text)
                        if rev_m:
                            article_title = (article_title + " " + rev_m.group(0)).strip()
                    else:
                        article_title = re.sub(r'^.*·\s*', '', card_text, flags=re.S).strip()[:200]
                else:
                    # Homepage-style: first line is company name
                    company = lines[0] if lines else slug.replace("-", " ").title()
                    article_title = lines[1] if len(lines) > 1 else ""

                # Parse relative date
                date_str = ""
                date_m = re.search(r"(\d+)\s+days?\s+ago", card_text)
                if date_m:
                    days = int(date_m.group(1))
                    date_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                else:
                    # Some cards show months ago
                    mo_m = re.search(r"(\d+)\s+months?\s+ago", card_text)
                    if mo_m:
                        months = int(mo_m.group(1))
                        date_str = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

                url = href if href.startswith("http") else f"https://www.starterstory.com{href}"
                results.append(_make_entry(
                    name=company[:80],
                    monthly=revenue,
                    description=article_title[:200] or card_text[:200],
                    source="Starter Story",
                    source_url="https://www.starterstory.com",
                    article_url=url,
                    published=date_str,
                    founder="",
                    tags=["bootstrapped", "interview", "vetted"],
                ))

        except Exception as e:
            pass  # One page failing shouldn't stop others

    print(f" {len(results)} found")
    return results


def _walk_for_stories(obj, depth=0) -> list[dict]:
    """Recursively walk JSON, return dicts that look like startup entries."""
    found = []
    if depth > 10:
        return found
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and any(k in item for k in ["name", "title", "slug", "revenue", "monthlyRevenue"]):
                found.append(item)
            else:
                found.extend(_walk_for_stories(item, depth + 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_walk_for_stories(v, depth + 1))
    return found


# ── Source: Failory ───────────────────────────────────────────────────────────

# Revenue bracket midpoints (Failory shows ranges, not exact figures)
FAILORY_BRACKETS = {
    "$100k-$500k/mo": 150_000,
    "$500k-$1m/mo":   600_000,
    "$1m+/mo":      1_200_000,
}

def fetch_failory(max_pages: int = 5) -> list[dict]:
    """
    Scrape Failory interview pages, filter to $100K+/month brackets.
    Failory hasn't published new interviews since late 2023, so results are
    backfill content. Cached the same as other sources.
    """
    print("  -> Failory...", end="", flush=True)
    results = []
    seen_slugs: set[str] = set()

    for page_num in range(1, max_pages + 1):
        url = "https://www.failory.com/interviews" if page_num == 1 else f"https://www.failory.com/interviews?page={page_num}"
        try:
            r = requests.get(url, headers=HEADERS, verify=SSL_VERIFY, timeout=20)
            if r.status_code == 404:
                break
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            page_results = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/interview/" not in href:
                    continue
                slug = href.rstrip("/").split("/")[-1]
                if slug in seen_slugs:
                    continue

                # Get card text
                card_text = ""
                node = a
                for _ in range(6):
                    node = getattr(node, "parent", None)
                    if not node:
                        break
                    txt = node.get_text(" ", strip=True)
                    if len(txt) > 50:
                        card_text = txt
                        break

                card_lower = card_text.lower()

                # Check revenue bracket
                monthly = None
                for bracket, midpoint in FAILORY_BRACKETS.items():
                    if bracket.lower().replace(" ", "") in card_lower.replace(" ", ""):
                        monthly = midpoint
                        break

                if monthly is None:
                    monthly = parse_monthly_revenue(card_text)

                if not monthly or monthly < MIN_MONTHLY:
                    continue

                seen_slugs.add(slug)
                company = a.get_text(strip=True)
                if not company or len(company) > 80:
                    company = slug.replace("-", " ").title()

                full_url = href if href.startswith("http") else f"https://www.failory.com{href}"

                # Try to get a date from card text
                date_m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d+,\s+\d{4}", card_text)
                date_str = date_m.group(0) if date_m else ""

                results.append(_make_entry(
                    name=company,
                    monthly=monthly,
                    description=card_text[:220],
                    source="Failory",
                    source_url="https://www.failory.com",
                    article_url=full_url,
                    published=date_str,
                    founder="",
                    tags=["interview", "vetted", "founder-story"],
                ))
                page_results += 1

            # Stop if page returned nothing new or pagination broke
            if page_results == 0 and page_num > 1:
                break

        except Exception as e:
            if page_num == 1:
                print(f" ERROR: {e}")
            break

    print(f" {len(results)} found")
    return results


# ── Source: Hacker News (Algolia) ─────────────────────────────────────────────

def fetch_hacker_news() -> list[dict]:
    """
    Search HN via the Algolia API for posts about bootstrapped businesses
    hitting revenue milestones. Searches last 12 months by default.
    """
    print("  -> Hacker News...", end="", flush=True)
    results = []
    seen: set[str] = set()

    since_ts = int((datetime.now() - timedelta(days=365)).timestamp())

    # HN titles rarely include "$100K/mo" but do include patterns like
    # "I made $X in Y months", "MRR", "ARR", "revenue". Search broadly
    # then filter by the revenue regex on the title text.
    queries = [
        ("I built profitable app MRR revenue", "story"),
        ("bootstrapped profitable solopreneur revenue", "story"),
        ("solo founder profitable SaaS MRR", "story"),
        ("indie hacker profitable revenue month", "story"),
        ("I quit job built profitable SaaS", "story"),
        ("bootstrapped profitable indie", "show_hn"),
        ("solopreneur profitable launched revenue", "story"),
        ("made million dollars bootstrapped", "story"),
    ]

    for q_text, tags in queries:
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": q_text,
                    "tags": tags,
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": 30,
                },
                verify=SSL_VERIFY, timeout=12,
            )
            resp.raise_for_status()
            data = resp.json()

            for hit in data.get("hits", []):
                obj_id = hit.get("objectID", "")
                if obj_id in seen:
                    continue

                title = hit.get("title", "")
                body  = hit.get("story_text") or ""
                combined = title + " " + body

                monthly = parse_monthly_revenue(combined)
                if not monthly or monthly < MIN_MONTHLY:
                    continue

                seen.add(obj_id)
                article_url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"

                # Check for bootstrapped / no-VC signals
                is_bootstrapped = any(
                    kw in combined.lower()
                    for kw in ["bootstrap", "no vc", "self-fund", "solo founder", "indie hacker", "solopreneur"]
                )
                tags_list = ["hacker-news", "community"]
                if is_bootstrapped:
                    tags_list.append("bootstrapped")

                date_str = (hit.get("created_at") or "")[:10]

                results.append(_make_entry(
                    name=title[:100],
                    monthly=monthly,
                    description=title,
                    source="Hacker News",
                    source_url="https://news.ycombinator.com",
                    article_url=article_url,
                    published=date_str,
                    founder=hit.get("author", ""),
                    tags=tags_list,
                ))

        except Exception:
            pass

    print(f" {len(results)} found")
    return results


# ── Source: Indie Hackers ─────────────────────────────────────────────────────

def fetch_indie_hackers() -> list[dict]:
    """Try to scrape Indie Hackers top revenue-verified products page."""
    print("  -> Indie Hackers...", end="", flush=True)
    results = []
    try:
        # IH products page (server-side rendered portion)
        r = requests.get(
            "https://www.indiehackers.com/products?revenueVerified=true&sorting=revenue",
            headers=HEADERS,
            verify=SSL_VERIFY, timeout=20,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Look for Next.js data blob
        nd_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if nd_tag:
            nd = json.loads(nd_tag.string or "{}")
            items = _walk_for_stories(nd)
            for item in items:
                rev = (
                    item.get("revenueAmount") or
                    item.get("revenue") or
                    item.get("mrr") or
                    parse_monthly_revenue(str(item.get("revenue", "")))
                )
                if not rev:
                    continue
                try:
                    rev = float(str(rev).replace(",", "").replace("$", ""))
                except ValueError:
                    continue
                if rev < MIN_MONTHLY:
                    continue
                slug = item.get("slug") or item.get("id") or ""
                results.append(_make_entry(
                    name=item.get("name") or item.get("title") or slug,
                    monthly=float(rev),
                    description=item.get("tagline") or item.get("description") or "",
                    source="Indie Hackers",
                    source_url="https://www.indiehackers.com",
                    article_url=f"https://www.indiehackers.com/product/{slug}",
                    published=item.get("createdAt", "")[:10],
                    founder=item.get("founderName") or "",
                    tags=["indie-hackers", "revenue-verified", "bootstrapped"],
                ))

        # Fallback: parse visible product cards
        if not results:
            for a in soup.find_all("a", href=True):
                if "/product/" not in a["href"]:
                    continue
                card_text = ""
                node = a
                for _ in range(5):
                    node = getattr(node, "parent", None)
                    if not node:
                        break
                    txt = node.get_text(" ", strip=True)
                    if len(txt) > 30:
                        card_text = txt
                        break
                monthly = parse_monthly_revenue(card_text)
                if not monthly or monthly < MIN_MONTHLY:
                    continue
                url = a["href"] if a["href"].startswith("http") else f"https://www.indiehackers.com{a['href']}"
                results.append(_make_entry(
                    name=a.get_text(strip=True)[:80],
                    monthly=monthly,
                    description=card_text[:200],
                    source="Indie Hackers",
                    source_url="https://www.indiehackers.com",
                    article_url=url,
                    published="",
                    founder="",
                    tags=["indie-hackers", "bootstrapped"],
                ))

        print(f" {len(results)} found")
    except Exception as e:
        print(f" skipped ({e})")
    return results


# ── Custom User Entries ───────────────────────────────────────────────────────

CUSTOM_TEMPLATE = [
    {
        "_instructions": (
            "Add startups you've found manually. All fields except 'name' and "
            "'monthly_revenue' are optional. Remove this _instructions entry when done."
        ),
        "_example": {
            "name": "My Cool App",
            "monthly_revenue": 120000,
            "revenue_display": "$120K/mo",
            "description": "One-line description of what the startup does.",
            "source": "Bootstrapped Giants",
            "source_url": "https://bootstrappedgiants.com",
            "article_url": "https://bootstrappedgiants.com/p/article-slug",
            "published_date": "2025-06-01",
            "founder": "Jane Doe",
            "founded_year": 2025,
            "tags": ["solo-founder", "AI", "SaaS"],
        },
    }
]


def load_custom() -> list[dict]:
    if not os.path.exists(CUSTOM_FILE):
        with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
            json.dump(CUSTOM_TEMPLATE, f, indent=2)
        print(f"  → Created {CUSTOM_FILE} — add your own finds there")
        return []

    with open(CUSTOM_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    entries = []
    for e in raw:
        if "_instructions" in e or "_example" in e:
            continue
        name = e.get("name", "").strip()
        rev  = e.get("monthly_revenue", 0)
        if not name or not rev:
            continue
        if not e.get("revenue_display"):
            e["revenue_display"] = fmt_mo(float(rev))
        if not e.get("source"):
            e["source"] = "Custom"
        entries.append(e)

    if entries:
        print(f"  → Custom: {len(entries)} entries")
    return entries


# ── Entry Builder ─────────────────────────────────────────────────────────────

def _make_entry(
    name, monthly, description, source, source_url, article_url,
    published, founder, tags
) -> dict:
    return {
        "name":            (name or "").strip()[:120],
        "monthly_revenue": round(float(monthly), 2),
        "revenue_display": fmt_mo(float(monthly)),
        "arr_display":     fmt_arr(float(monthly)),
        "description":     (description or "").strip()[:280],
        "source":          source,
        "source_url":      source_url,
        "article_url":     article_url or source_url,
        "published_date":  published or "",
        "founder":         (founder or "").strip()[:80],
        "tags":            tags or [],
    }


# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache() -> list[dict] | None:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01T00:00:00"))
        age_h = (datetime.now() - cached_at).total_seconds() / 3600
        if age_h < CACHE_HOURS:
            print(f"  Cache {age_h:.1f}h old (refreshes at {CACHE_HOURS}h). Use --refresh to force.")
            return data.get("startups", [])
    except Exception:
        pass
    return None


def save_cache(startups: list[dict]):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"cached_at": datetime.now().isoformat(), "startups": startups},
            f, indent=2, default=str,
        )


def dedup(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.get("article_url") or item.get("name") or ""
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ── HTML Generation ───────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "Starter Story": "#7c3aed",
    "Failory":        "#dc2626",
    "Hacker News":    "#ea580c",
    "Indie Hackers":  "#2563eb",
    "Custom":         "#16a34a",
}

def _badge(text: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}">{escape(text)}</span>'

def _tag(text: str) -> str:
    return f'<span class="tag">{escape(text)}</span>'

def _card_html(s: dict) -> str:
    rev   = s.get("monthly_revenue", 0)
    color = SOURCE_COLORS.get(s.get("source", ""), "#6b7280")
    tags  = "".join(_tag(t) for t in s.get("tags", [])[:4])
    src   = _badge(s.get("source", ""), color)
    ft    = f'<span class="meta-item">👤 {escape(s["founder"])}</span>' if s.get("founder") else ""
    dt    = f'<span class="meta-item">📅 {escape(s["published_date"])}</span>' if s.get("published_date") else ""
    yr    = f'<span class="meta-item">🗓 Founded {s["founded_year"]}</span>' if s.get("founded_year") else ""
    desc  = escape(s.get("description") or "")
    return f"""
<div class="card" data-revenue="{rev}" data-source="{escape(s.get('source',''))}" data-date="{escape(s.get('published_date',''))}">
  <div class="card-top">
    <div class="co-name">{escape(s.get('name',''))}</div>
    <div class="rev-pill">{escape(s.get('revenue_display',''))}</div>
  </div>
  <div class="arr">{escape(s.get('arr_display',''))}</div>
  <p class="desc">{desc}</p>
  <div class="meta">{ft}{dt}{yr}{tags}{src}</div>
  <a class="read-btn" href="{escape(s.get('article_url','#'))}" target="_blank" rel="noopener">Read Story →</a>
</div>"""


def generate_html(startups: list[dict]) -> str:
    startups = sorted(startups, key=lambda x: x.get("monthly_revenue", 0), reverse=True)

    total = len(startups)
    top   = max((s.get("monthly_revenue", 0) for s in startups), default=0)
    avg   = sum(s.get("monthly_revenue", 0) for s in startups) / max(total, 1)
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")

    sources = sorted({s.get("source", "") for s in startups})
    src_opts = '<option value="">All Sources</option>' + "".join(
        f'<option value="{escape(s)}">{escape(s)}</option>' for s in sources
    )
    cards = "".join(_card_html(s) for s in startups)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bootstrapped Startup Tracker</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
       background: #f8fafc; color: #1e293b; min-height: 100vh; }}

/* ── Header ── */
.header {{ background: #fff; border-bottom: 2px solid #e2e8f0; padding: 18px 32px;
           display: flex; align-items: center; justify-content: space-between;
           position: sticky; top: 0; z-index: 100; box-shadow: 0 1px 4px rgba(0,0,0,.05); }}
.header h1 {{ font-size: 21px; font-weight: 800; color: #0f172a; letter-spacing: -.3px; }}
.header h1 em {{ color: #7c3aed; font-style: normal; }}
.upd {{ font-size: 11px; color: #94a3b8; text-align: right; line-height: 1.6; }}

/* ── Stats bar ── */
.stats {{ display: flex; gap: 0; background: #fff; border-bottom: 1px solid #e2e8f0; }}
.stat {{ flex: 1; padding: 16px 24px; text-align: center; border-right: 1px solid #e2e8f0; }}
.stat:last-child {{ border-right: none; }}
.stat-val {{ font-size: 26px; font-weight: 800; color: #7c3aed; }}
.stat-lbl {{ font-size: 11px; color: #64748b; text-transform: uppercase;
             letter-spacing: .06em; margin-top: 3px; }}

/* ── Controls ── */
.controls {{ background: #fff; border-bottom: 1px solid #e2e8f0;
             padding: 12px 32px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
.ctrl-lbl {{ font-size: 12px; color: #64748b; white-space: nowrap; }}
select, input[type=range] {{ border: 1px solid #cbd5e1; border-radius: 6px;
    padding: 5px 10px; font-size: 13px; color: #1e293b;
    outline: none; background: #fff; cursor: pointer; }}
select:focus {{ border-color: #7c3aed; box-shadow: 0 0 0 2px #ede9fe; }}
.sort-btn {{ padding: 5px 14px; border: 1px solid #cbd5e1; border-radius: 6px;
             background: #fff; font-size: 12px; cursor: pointer; color: #475569;
             font-weight: 500; transition: all .15s; }}
.sort-btn:hover {{ background: #f8fafc; }}
.sort-btn.active {{ background: #7c3aed; color: #fff; border-color: #7c3aed; }}
#count {{ font-size: 12px; color: #64748b; margin-left: auto; }}

/* ── Grid ── */
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
         gap: 18px; padding: 24px 32px; }}

/* ── Card ── */
.card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px;
         display: flex; flex-direction: column; gap: 9px;
         transition: box-shadow .15s, transform .1s; }}
.card:hover {{ box-shadow: 0 6px 20px rgba(0,0,0,.09); transform: translateY(-1px); }}
.card.hidden {{ display: none; }}

.card-top {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }}
.co-name {{ font-size: 15px; font-weight: 700; color: #0f172a; line-height: 1.3; flex: 1; }}
.rev-pill {{ background: #f0fdf4; color: #15803d; font-size: 14px; font-weight: 700;
             padding: 3px 10px; border-radius: 20px; white-space: nowrap; flex-shrink: 0; }}
.arr {{ font-size: 11px; color: #94a3b8; }}
.desc {{ font-size: 13px; color: #475569; line-height: 1.55; flex: 1; }}

.meta {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin-top: 2px; }}
.meta-item {{ font-size: 11px; color: #64748b; }}
.badge {{ font-size: 11px; font-weight: 600; color: #fff; padding: 2px 8px;
          border-radius: 4px; white-space: nowrap; }}
.tag {{ font-size: 11px; background: #f1f5f9; color: #475569; padding: 2px 7px;
        border-radius: 4px; }}

.read-btn {{ display: inline-block; margin-top: 4px; color: #7c3aed; font-size: 12px;
             font-weight: 600; text-decoration: none; }}
.read-btn:hover {{ text-decoration: underline; }}

/* ── Empty ── */
.empty {{ grid-column: 1/-1; text-align: center; padding: 60px 20px; color: #94a3b8; }}
.empty h3 {{ font-size: 17px; margin-bottom: 6px; color: #475569; }}

/* ── Note ── */
.note {{ padding: 12px 32px; background: #fffbeb; border-bottom: 1px solid #fde68a;
         font-size: 12px; color: #92400e; }}
.note a {{ color: #92400e; font-weight: 600; }}

/* ── Footer ── */
.footer {{ padding: 24px 32px; text-align: center; font-size: 12px; color: #94a3b8;
           border-top: 1px solid #e2e8f0; line-height: 1.8; }}
.footer a {{ color: #7c3aed; text-decoration: none; }}
.footer a:hover {{ text-decoration: underline; }}

@media (max-width: 640px) {{
  .header, .stats, .controls, .grid, .note, .footer {{ padding-left: 16px; padding-right: 16px; }}
  .stats {{ flex-wrap: wrap; }}
  .stat {{ flex: 1 0 45%; }}
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
  <h1>🚀 <em>Bootstrapped</em> Startup Tracker</h1>
  <div class="upd">Updated {now}<br>Auto-refreshes every 24 hours</div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-val">{total}</div><div class="stat-lbl">Companies</div></div>
  <div class="stat"><div class="stat-val">{fmt_mo(top)}</div><div class="stat-lbl">Top Monthly Revenue</div></div>
  <div class="stat"><div class="stat-val">{fmt_arr(top)}</div><div class="stat-lbl">Top ARR</div></div>
  <div class="stat"><div class="stat-val">{fmt_mo(avg)}</div><div class="stat-lbl">Avg Monthly Revenue</div></div>
  <div class="stat"><div class="stat-val">{len(sources)}</div><div class="stat-lbl">Sources</div></div>
</div>

<div class="note">
  ℹ️ <strong>Filter tip:</strong> All companies shown have ≥$100K/month revenue from vetted reporter-compiled sources.
  To confirm "under 1 year old", check the article — founding dates vary.
  Add your own finds to <a href="{escape(CUSTOM_FILE)}">custom_startups.json</a>.
  Browse more at <a href="https://bootstrappedgiants.com" target="_blank">Bootstrapped Giants</a> and
  <a href="https://www.starterstory.com" target="_blank">Starter Story</a>.
</div>

<div class="controls">
  <span class="ctrl-lbl">Source:</span>
  <select id="src-sel" onchange="filter()">
    {src_opts}
  </select>
  <span class="ctrl-lbl">Min revenue:</span>
  <select id="rev-sel" onchange="filter()">
    <option value="100000">$100K+/mo</option>
    <option value="200000">$200K+/mo</option>
    <option value="500000">$500K+/mo</option>
    <option value="1000000">$1M+/mo</option>
  </select>
  <span class="ctrl-lbl">Sort:</span>
  <button class="sort-btn active" id="btn-rev" onclick="sort('revenue')">Revenue ↓</button>
  <button class="sort-btn" id="btn-date" onclick="sort('date')">Date ↓</button>
  <span id="count">{total} results</span>
</div>

<div class="grid" id="grid">
  {cards}
  <div class="empty hidden" id="empty">
    <h3>No results match your filters</h3>
    <p>Try lowering the minimum revenue or selecting a different source.</p>
  </div>
</div>

<div class="footer">
  Sources checked daily:
  <a href="https://www.starterstory.com" target="_blank">Starter Story</a> ·
  <a href="https://www.failory.com/interviews" target="_blank">Failory</a> ·
  <a href="https://news.ycombinator.com" target="_blank">Hacker News</a> ·
  <a href="https://www.indiehackers.com" target="_blank">Indie Hackers</a> ·
  <a href="{escape(CUSTOM_FILE)}" target="_blank">Your Custom List</a><br>
  Criteria: ≥$100K/month revenue · Bootstrapped / no VC · Solo or small founder ·
  Articles from vetted reporter-compiled sources only.
</div>

<script>
let curSort = 'revenue';

function filter() {{
  const src = document.getElementById('src-sel').value;
  const minR = +document.getElementById('rev-sel').value;
  const cards = document.querySelectorAll('.card');
  let n = 0;
  cards.forEach(c => {{
    const ok = (!src || c.dataset.source === src) && +c.dataset.revenue >= minR;
    c.classList.toggle('hidden', !ok);
    if (ok) n++;
  }});
  document.getElementById('count').textContent = n + ' results';
  document.getElementById('empty').classList.toggle('hidden', n > 0);
}}

function sort(by) {{
  curSort = by;
  document.getElementById('btn-rev').classList.toggle('active', by === 'revenue');
  document.getElementById('btn-date').classList.toggle('active', by === 'date');
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');
  const cards = [...grid.querySelectorAll('.card:not(#empty)')];
  cards.sort((a, b) => by === 'revenue'
    ? +b.dataset.revenue - +a.dataset.revenue
    : (b.dataset.date || '').localeCompare(a.dataset.date || ''));
  cards.forEach(c => grid.insertBefore(c, empty));
}}
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    no_browser    = "--no-browser"    in sys.argv
    force_refresh = "--refresh"       in sys.argv

    print("=" * 54)
    print(" Bootstrapped Startup Tracker")
    print(" Criteria: >=$100K/mo | no VC | solo/small founder")
    print("=" * 54)

    # ── Scraped sources (cached 24 h) ──
    if not force_refresh:
        print("Cache check:")
        scraped = load_cache()
    else:
        print("Forcing refresh of all sources...")
        scraped = None

    if scraped is None:
        print("Fetching sources:")
        scraped = []
        scraped.extend(fetch_starter_story())
        scraped.extend(fetch_failory())
        scraped.extend(fetch_hacker_news())
        scraped.extend(fetch_indie_hackers())
        scraped = dedup(scraped)
        save_cache(scraped)
        print(f"  Cache saved ({len(scraped)} entries)")

    # ── Custom entries (always fresh) ──
    print("Custom entries:")
    custom = load_custom()

    all_startups = dedup(scraped + custom)
    print(f"\n✓ {len(all_startups)} companies total (≥$100K/mo)")

    # ── Generate & open ──
    html = generate_html(all_startups)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ Dashboard → {OUTPUT_FILE}")

    if not no_browser:
        webbrowser.open(OUTPUT_FILE)


if __name__ == "__main__":
    main()
