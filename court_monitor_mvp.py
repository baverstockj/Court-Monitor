"""
Court Monitor MVP (No spaCy / No NLP dependencies)
==========================================================

What I'm trying to do
---------------------
I built this MVP to prove we can monitor publicly available local news RSS feeds for
court/criminal-justice reporting, store the articles in a database, and extract a few
useful structured fields (name, age, street name if mentioned).

Why "Route 2"?
--------------
I originally planned to use spaCy (an NLP library) for named-entity recognition, but on my
machine I'm running Python 3.14 and spaCy currently fails to install/run due to dependency
compatibility (pydantic/spaCy stack). So Route 2 is a pragmatic approach that works now:

- I keep my tight keyword filtering logic (requires action words like arrested/convicted/bailed)
- I fetch the article page and turn HTML into text
- I extract name/age/street using simple patterns + a scoring system to reduce false positives

How the name extraction works (important)
-----------------------------------------
A simple regex "Firstname Lastname" will often grab publisher names (e.g. "Birmingham Live"),
so instead of taking the first match, I:

1) Collect ALL name-like candidates from title + article text
2) Score each candidate:
   - +5 if the name appears in the title
   - +6 if it's near action words like "arrested/charged/convicted/bailed"
   - +6 if it matches a "Name, 34" or "Name aged 34" pattern
   - +2 if it looks like a normal 2-word name
   - big negative score if it contains known publisher/brand phrases (blocklist)
   - penalties if it contains "news/mail/live/telegraph/record/star/press/echo"
3) Pick the highest scoring candidate (and reject if the score is too low)

This isn't perfect, but it's a strong MVP step and it's explainable.

What gets stored
----------------
SQLite database file: court_monitor_mvp.sqlite

Tables:
- articles: one row per RSS item/article (including keyword matches and whether I flagged it)
- feed_fetch_log: one row per feed fetch attempt (HTTP status, bytes, entry counts)
- mentions: extracted fields for flagged articles (person_name, age, street, confidence)

Requirements
------------
pip install feedparser requests

Run
---
python3 court_monitor_mvp.py

Tip
---
If you change the extraction logic and want to re-extract the same articles, the simplest
is to clear the mentions table:
  DELETE FROM mentions;
then run again.
"""

import os
import re
import sqlite3
import datetime as dt
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict

import feedparser
import requests


# =============================================================================
# CONFIG
# =============================================================================

DB_PATH = "court_monitor_mvp.sqlite"

FEEDS = [
    ("Birmingham Mail - News", "https://www.birminghammail.co.uk/news/?service=rss"),
    ("Express & Star - News", "https://contentstore.azure-api.net/syndication/feeds/rss/007b3543-17ca-4881-aaa9-c68921492916?urlPath=%2Fnews"),
    ("Coventry Telegraph - News", "https://www.coventrytelegraph.net/news/?service=rss"),
    ("Daily Record - News", "https://www.dailyrecord.co.uk/news/?service=rss"),
    ("Manchester Evening News", "https://www.manchestereveningnews.co.uk/?service=rss"),
    ("The Argus - News", "https://www.theargus.co.uk/news/rss/"),
    ("York Press - News", "https://www.yorkpress.co.uk/news/rss/"),
    ("The Northern Echo - News", "https://www.thenorthernecho.co.uk/news/rss/"),
    ("The Bolton News - News", "https://www.theboltonnews.co.uk/news/rss/"),
    ("The Argus - News", "https://www.theargus.co.uk/news/rss/"),
    ("The York Press - News", "https://www.yorkpress.co.uk/news/rss/"),
    ("The Northern Echo - News", "https://www.thenorthernecho.co.uk/news/rss/"),
    ("The News Portsmouth - News", "https://www.portsmouth.co.uk/rss"),
    ("The Scarborough News - News", "https://www.thescarboroughnews.co.uk/rss"),
    ("Wales Online - News", "https://www.walesonline.co.uk/?service=rss"),
    ("The Herald Scotland - News", "https://www.heraldscotland.com/news/rss/"),
    ("Cambridge News - News", "https://www.cambridge-news.co.uk/?service=rss"),
    ("The Grimsby Times - News", "https://www.grimsbytelegraph.co.uk/news/?service=rss"),
    ("The Glasgow Times - News", "https://www.glasgowtimes.co.uk/news/rss/"),
]

HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "CourtMonitorMVP/2.3 (internal demo; contact your-team@company) "
    "Mozilla/5.0"
)

# I keep this True so the MVP actually fetches article pages and attempts extraction.
FETCH_ARTICLE_PAGES = True

# Throttle parsing to reduce the chance of getting blocked by publishers.
MAX_ARTICLES_TO_PARSE_PER_RUN = 40


# =============================================================================
# KEYWORDS (tight filter)
# =============================================================================
# My aim is to reduce noise (potholes/storms) by requiring an "action" word.
# These are deliberately biased toward criminal-justice action/outcomes.

ACTION_KEYWORDS = [
    "arrested", "arrest",
    "charged", "charge",
    "convicted", "conviction",
    "bailed", "released on bail",
    "pleaded guilty", "pleaded not guilty",
    "found guilty",
    "jailed", "imprisoned",
    "sentenced", "sentenced to",
    "remanded", "remand",
]

# Context words are useful but not required. I use them mainly to help avoid edge cases.
CONTEXT_KEYWORDS = [
    "crown court",
    "magistrates",
    "magistrates' court",
    "appeared in court",
    "appeared before",
    "hearing",
    "trial",
    "judge",
    "jury",
    "prosecutor",
    "prosecution",
    "defence",
    "defense",
]

# Ignore list: common local-news topics that cause false positives.
# Rule: if there are ignore words AND no context words, reject (unless action+context suggests it's real).
IGNORE_KEYWORDS = [
    "pothole", "potholes",
    "storm", "storms",
    "met office",
    "weather warning", "amber warning", "yellow warning", "red warning",
    "flood", "flooding",
    "snow", "ice", "freezing",
    "wind", "gales",
    "roadworks", "lane closure", "closure",
    "traffic", "congestion",
    "planning application",
    "council meeting",
]


# =============================================================================
# EXTRACTION PATTERNS (Name / Age / Street)
# =============================================================================

# Person-like name: Firstname Lastname (or First Middle Last)
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")

# Court-report style name+age (helps confidence):
# "John Smith, 34, ..."
# "John Smith, aged 34, ..."
NAME_NEAR_AGE_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b\s*,?\s*(?:aged\s+)?(\d{1,2})\b",
    re.I
)

# Age patterns
AGE_RES = [
    re.compile(r"\baged\s+(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})-year-old\b", re.I),
    re.compile(r"\bage\s+(\d{1,2})\b", re.I),
]

# Streets: look for "Something Road" / "Something Street" etc.
STREET_SUFFIXES = r"(Street|St|Road|Rd|Avenue|Ave|Close|Cl|Lane|Ln|Drive|Dr|Way|Crescent|Cr|Place|Pl|Gardens|Gdns|Court|Ct|Terrace|Tce|Grove|Gr|Hill)"
STREET_RE = re.compile(rf"\b([A-Z][A-Za-z' -]+?\s+{STREET_SUFFIXES})\b")

# Blocklist: phrases that I know are NOT people (publishers/site phrases).
# I can keep adding to this as I encounter new false positives.
BLOCKLIST_PHRASES = [
    "birmingham live",
    "birmingham mail",
    "manchester evening news",
    "coventry telegraph",
    "daily record",
    "express & star",
    "express and star",
    "the argus",
    "york press",
    "the northern echo",
    "the bolton news",
    "reach plc",
    "newsquest",
    "newsletter",
    "sign up",
    "cookie policy",
    "privacy policy",
]

# These words often appear in publisher names and can contaminate "Firstname Lastname" matches.
PUBLISHERISH_WORDS = ["news", "live", "mail", "telegraph", "record", "star", "press", "echo"]


# =============================================================================
# DATABASE SCHEMA
# =============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  feed_url TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  title TEXT,
  published_at TEXT,
  snippet TEXT,
  keyword_hits INTEGER DEFAULT 0,
  matched_keywords TEXT,
  ignore_keywords TEXT,
  is_court_candidate INTEGER DEFAULT 0,
  created_at TEXT,
  article_http_status INTEGER,
  article_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS feed_fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  feed_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  http_status INTEGER,
  bytes INTEGER,
  entries_found INTEGER,
  error TEXT
);

CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL,
  person_name TEXT,
  age INTEGER,
  street TEXT,
  confidence REAL,
  extracted_at TEXT,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db_and_migrate() -> None:
    """
    I keep migrations lightweight: create tables, and add columns if missing.
    This stops the script breaking when I add fields later.
    """
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)

        # Ensure newer columns exist if the DB was created by older script versions.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles);").fetchall()]

        def add_col_if_missing(col: str, col_def: str):
            if col not in cols:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col_def};")

        add_col_if_missing("article_http_status", "article_http_status INTEGER")
        add_col_if_missing("article_fetched_at", "article_fetched_at TEXT")


# =============================================================================
# RSS + KEYWORD HELPERS
# =============================================================================

def safe_str(x) -> str:
    return (x or "").strip()


def parse_feed_date(entry) -> Optional[str]:
    """
    RSS feeds vary. I try a few common fields and store a best-effort published timestamp.
    """
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            d = dt.datetime(*t[:6], tzinfo=timezone.utc)
            return d.replace(microsecond=0).isoformat()

    for key in ("published", "updated"):
        v = entry.get(key)
        if v:
            return safe_str(v)[:80]

    return None


def keyword_match_list(text_lower: str, keywords: List[str]) -> List[str]:
    found = []
    for kw in keywords:
        if kw in text_lower and kw not in found:
            found.append(kw)
    return found


def find_keyword_matches(text: str) -> Tuple[List[str], List[str], List[str]]:
    t = text.lower()
    actions = keyword_match_list(t, ACTION_KEYWORDS)
    context = keyword_match_list(t, CONTEXT_KEYWORDS)
    ignore = keyword_match_list(t, IGNORE_KEYWORDS)
    return actions, context, ignore


def decide_court_candidate(actions: List[str], context: List[str], ignore: List[str]) -> bool:
    """
    Tight gating:
    - MUST have at least 1 action keyword
    - If it looks like weather/roads/council and has NO court context, reject
    """
    if len(actions) == 0:
        return False
    if len(ignore) > 0 and len(context) == 0:
        return False
    return True


# =============================================================================
# FETCH RSS WITH HTTP DEBUG (status codes)
# =============================================================================

def fetch_rss_with_debug(
    source: str,
    feed_url: str
) -> Tuple[feedparser.FeedParserDict, Optional[int], int, Optional[str]]:
    try:
        r = requests.get(
            feed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True
        )
        status = r.status_code
        size = len(r.content or b"")
        print(f"[{source}] HTTP {status} | {size} bytes")

        if status != 200 or size == 0:
            return feedparser.parse(b""), status, size, f"Non-200 or empty response: {status}"

        feed = feedparser.parse(r.content)

        if getattr(feed, "bozo", 0) == 1:
            bozo_exc = getattr(feed, "bozo_exception", None)
            return feed, status, size, f"Feed parse warning (bozo): {bozo_exc}"

        return feed, status, size, None

    except requests.exceptions.Timeout:
        print(f"[{source}] ERROR: Timeout")
        return feedparser.parse(b""), None, 0, "Timeout"
    except requests.exceptions.RequestException as e:
        print(f"[{source}] ERROR: RequestException: {e}")
        return feedparser.parse(b""), None, 0, f"RequestException: {e}"
    except Exception as e:
        print(f"[{source}] ERROR: Unexpected: {e}")
        return feedparser.parse(b""), None, 0, f"Unexpected: {e}"


# =============================================================================
# ARTICLE FETCH + HTML -> TEXT
# =============================================================================

def fetch_article_html(url: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Fetch the article web page. Some publishers may block automated requests.
    I store the HTTP status in the articles table so I can spot blocking.
    """
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True
        )
        return (r.text if r.status_code == 200 else None), r.status_code
    except Exception:
        return None, None


def html_to_text(html: str) -> str:
    """
    Very simple MVP HTML -> text conversion:
    - remove scripts/styles
    - remove tags
    - normalise whitespace
    """
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", html).strip()


# =============================================================================
# EXTRACTION (Route 2): Name scoring + regex for age/street
# =============================================================================

ACTION_WORDS_FOR_PROXIMITY = [
    "arrested", "charged", "convicted", "bailed", "sentenced",
    "jailed", "imprisoned", "remanded", "pleaded"
]


def is_blocked_name(candidate: str) -> bool:
    """
    Reject obvious non-person matches: publisher/site phrases, consent UI text, etc.
    """
    c = candidate.strip().lower()
    if not c:
        return True

    for phrase in BLOCKLIST_PHRASES:
        if phrase in c:
            return True

    # If it's basically a brand-like phrase containing one of these words, it's suspicious.
    # This is intentionally blunt for MVP.
    for w in PUBLISHERISH_WORDS:
        if w in c:
            return True

    return False


def find_all_name_candidates(text: str) -> List[str]:
    """
    Find all Firstname Lastname-style candidates and return them de-duplicated.
    """
    candidates: List[str] = []
    for m in NAME_RE.finditer(text):
        cand = m.group(1).strip()
        if cand not in candidates:
            candidates.append(cand)
    return candidates


def near_any(text_lower: str, target: str, keywords: List[str], window: int = 160) -> bool:
    """
    True if any keyword appears within a character window around target.
    I use characters rather than words because it's simple and fast.
    """
    idx = text_lower.find(target.lower())
    if idx == -1:
        return False
    start = max(0, idx - window)
    end = min(len(text_lower), idx + len(target) + window)
    chunk = text_lower[start:end]
    return any(k in chunk for k in keywords)


def score_name_candidate(candidate: str, title: str, text: str) -> int:
    """
    Score how likely this candidate is the defendant/subject of the court report.
    """
    if is_blocked_name(candidate):
        return -999

    c_low = candidate.lower()
    title_low = title.lower()
    text_low = text.lower()

    score = 0

    # Title is strong signal: headlines often include the defendant.
    if c_low in title_low:
        score += 5

    # Court report action words near the candidate name is a strong signal.
    if near_any(text_low, candidate, ACTION_WORDS_FOR_PROXIMITY, window=200):
        score += 6

    # If candidate appears in a "Name, 34" pattern, boost heavily.
    for m in NAME_NEAR_AGE_RE.finditer(text):
        if m.group(1).strip().lower() == c_low:
            score += 6
            break

    # Prefer typical 2-3 word names.
    wc = len(candidate.split())
    if wc == 2:
        score += 2
    elif wc == 3:
        score += 1
    else:
        score -= 2

    # Extra penalty for suspicious words (belt and braces).
    for bad in PUBLISHERISH_WORDS:
        if bad in c_low:
            score -= 6

    return score


def pick_best_name(title: str, text: str) -> Optional[str]:
    """
    Gather candidates from title+text, score them, and pick the best.
    """
    combined = f"{title} {text}"
    candidates = find_all_name_candidates(combined)
    if not candidates:
        return None

    scored = [(score_name_candidate(c, title, text), c) for c in candidates]
    scored.sort(reverse=True, key=lambda x: x[0])

    best_score, best_name = scored[0]
    # Threshold: if we can't score at least 2, it's probably garbage.
    if best_score < 2:
        return None

    return best_name


def extract_age(text: str) -> Optional[int]:
    for ar in AGE_RES:
        m = ar.search(text)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
    return None


def extract_street(text: str) -> Optional[str]:
    m = STREET_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def extract_name_age_street(text: str, title: str = "") -> Dict[str, Optional[object]]:
    """
    Main extraction function used by the MVP.
    """
    name = pick_best_name(title, text)
    age = extract_age(text)
    street = extract_street(text)

    # Simple confidence score for MVP demos (explainable and consistent)
    conf = 0.0
    conf += 0.55 if name else 0.0
    conf += 0.30 if age is not None else 0.0
    conf += 0.15 if street else 0.0

    return {"name": name, "age": age, "street": street, "confidence": round(conf, 2)}


# =============================================================================
# INGEST RSS -> articles table
# =============================================================================

def ingest_rss_feeds() -> None:
    with get_db() as conn:
        for source, feed_url in FEEDS:
            print("\n" + "-" * 70)
            print(f"Reading feed: {source}")
            print(f"URL: {feed_url}")

            feed, status, size, error = fetch_rss_with_debug(source, feed_url)
            entries_count = len(feed.entries) if hasattr(feed, "entries") else 0

            print(f"[{source}] Entries found: {entries_count}")
            if error:
                print(f"[{source}] NOTE: {error}")

            # Always log the fetch attempt (helps debugging)
            conn.execute(
                """
                INSERT INTO feed_fetch_log
                (source, feed_url, fetched_at, http_status, bytes, entries_found, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source, feed_url, now_iso(), status, size, entries_count, error)
            )

            if entries_count == 0:
                continue

            inserted_count = 0
            court_candidate_count = 0

            for entry in feed.entries:
                url = safe_str(entry.get("link"))
                if not url:
                    continue

                title = safe_str(entry.get("title"))
                snippet = safe_str(entry.get("summary") or entry.get("description"))
                published_at = parse_feed_date(entry)

                combined = f"{title} {snippet}".strip()

                actions, context, ignore = find_keyword_matches(combined)

                matched_keywords_str = "|".join(actions + context)
                ignore_keywords_str = "|".join(ignore)
                keyword_hits = len(actions) + len(context)

                court_flag = 1 if decide_court_candidate(actions, context, ignore) else 0

                try:
                    conn.execute(
                        """
                        INSERT INTO articles
                        (source, feed_url, url, title, published_at, snippet,
                         keyword_hits, matched_keywords, ignore_keywords,
                         is_court_candidate, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source, feed_url, url, title, published_at, snippet,
                            keyword_hits, matched_keywords_str, ignore_keywords_str,
                            court_flag, now_iso()
                        )
                    )
                    inserted_count += 1
                    if court_flag == 1:
                        court_candidate_count += 1
                except sqlite3.IntegrityError:
                    # Already stored (same URL)
                    pass

            print(f"[{source}] New articles inserted: {inserted_count}")
            print(f"[{source}] Court candidates flagged: {court_candidate_count}")


# =============================================================================
# Parse flagged articles -> mentions table
# =============================================================================

def parse_court_candidate_articles() -> None:
    """
    I only parse articles that:
      - are flagged as court candidates
      - have not already been extracted into mentions

    This keeps each run incremental and avoids hammering the same sites.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.url, a.title
            FROM articles a
            WHERE a.is_court_candidate = 1
              AND a.id NOT IN (SELECT DISTINCT article_id FROM mentions)
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (MAX_ARTICLES_TO_PARSE_PER_RUN,)
        ).fetchall()

        if not rows:
            print("\nNo new court-candidate articles to parse.")
            return

        print(f"\nParsing {len(rows)} court-candidate article pages...")

        parsed_ok = 0
        parsed_fail = 0

        for article_id, url, title in rows:
            html, status = fetch_article_html(url)

            # Record the HTTP status so I can see blocking issues per publisher
            conn.execute(
                "UPDATE articles SET article_http_status = ?, article_fetched_at = ? WHERE id = ?",
                (status, now_iso(), article_id)
            )

            if not html:
                parsed_fail += 1
                continue

            text = html_to_text(html)

            extracted = extract_name_age_street(text, title=title)

            # If extraction got nothing useful, I don't insert a mention row.
            if extracted["name"] is None and extracted["age"] is None and extracted["street"] is None:
                parsed_fail += 1
                continue

            conn.execute(
                """
                INSERT INTO mentions(article_id, person_name, age, street, confidence, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (article_id, extracted["name"], extracted["age"], extracted["street"], extracted["confidence"], now_iso())
            )

            parsed_ok += 1

        print(f"Article parsing complete. Extracted mentions: {parsed_ok}, failed/no-data: {parsed_fail}")


# =============================================================================
# Quick stats + demo queries
# =============================================================================

def print_quick_stats() -> None:
    with get_db() as conn:
        total_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        total_candidates = conn.execute("SELECT COUNT(*) FROM articles WHERE is_court_candidate = 1").fetchone()[0]
        total_mentions = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        total_logs = conn.execute("SELECT COUNT(*) FROM feed_fetch_log").fetchone()[0]

        print("\n" + "=" * 70)
        print("QUICK STATS")
        print(f"Database file: {os.path.abspath(DB_PATH)}")
        print(f"Total articles stored: {total_articles}")
        print(f"Total court candidates: {total_candidates}")
        print(f"Total extracted mentions: {total_mentions}")
        print(f"Total feed fetch logs: {total_logs}")


def main() -> None:
    print("Initialising / migrating database...")
    init_db_and_migrate()

    print("\nIngesting RSS feeds...")
    ingest_rss_feeds()

    if FETCH_ARTICLE_PAGES:
        parse_court_candidate_articles()

    print_quick_stats()

    print("\nDone.")
    print("\nDB Browser demo queries (copy/paste):")

    print("\n1) Latest extracted mentions:")
    print("""
SELECT m.extracted_at, a.source, m.person_name, m.age, m.street, m.confidence, a.title, a.url
FROM mentions m
JOIN articles a ON a.id = m.article_id
ORDER BY m.id DESC
LIMIT 50;
""".strip())

    print("\n2) Latest court candidates:")
    print("""
SELECT source, title, matched_keywords, ignore_keywords, published_at, url
FROM articles
WHERE is_court_candidate = 1
ORDER BY id DESC
LIMIT 50;
""".strip())

    print("\n3) Articles that failed to fetch (blocked or not 200):")
    print("""
SELECT source, title, article_http_status, url
FROM articles
WHERE is_court_candidate = 1
  AND article_http_status IS NOT NULL
  AND article_http_status != 200
ORDER BY id DESC
LIMIT 50;
""".strip())

    print("\n4) If I want to re-run extraction from scratch (careful!):")
    print("   DELETE FROM mentions;")


if __name__ == "__main__":
    main()
