"""
COURT MONITORING MVP (RSS + ARTICLE PARSING + NAME/AGE/STREET EXTRACTION)
========================================================================
Improvement:
- Prevent person_name incorrectly capturing newspaper/site names by:
  - Using a blocklist of publisher phrases
  - Rejecting matches that contain blocked phrases
  - Preferring names near age/action patterns (court-report style)
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
]

HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "CourtMonitorMVP/2.1 (internal demo; contact your-team@company) "
    "Mozilla/5.0"
)

FETCH_ARTICLE_PAGES = True
MAX_ARTICLES_TO_PARSE_PER_RUN = 40


# =============================================================================
# KEYWORDS (tight filter)
# =============================================================================

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
]

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
# EXTRACTION PATTERNS
# =============================================================================

# Generic person-like name (Firstname Lastname or First Middle Last)
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")

# Court-report style: Name near age
# Examples:
#   "John Smith, 34, ..."
#   "John Smith, aged 34, ..."
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

# Street suffixes
STREET_SUFFIXES = r"(Street|St|Road|Rd|Avenue|Ave|Close|Cl|Lane|Ln|Drive|Dr|Way|Crescent|Cr|Place|Pl|Gardens|Gdns|Court|Ct|Terrace|Tce|Grove|Gr|Hill)"
STREET_RE = re.compile(rf"\b([A-Z][A-Za-z' -]+?\s+{STREET_SUFFIXES})\b")

# Blocklist for non-person matches (brands/publishers/common site phrases)
# Add to this list whenever you spot a bad capture.
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
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles);").fetchall()]

        def add_col_if_missing(col: str, col_def: str):
            if col not in cols:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col_def};")

        add_col_if_missing("article_http_status", "article_http_status INTEGER")
        add_col_if_missing("article_fetched_at", "article_fetched_at TEXT")


# =============================================================================
# HELPERS
# =============================================================================

def safe_str(x) -> str:
    return (x or "").strip()


def parse_feed_date(entry) -> Optional[str]:
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
    if len(actions) == 0:
        return False
    if len(ignore) > 0 and len(context) == 0:
        return False
    return True


# =============================================================================
# FETCH RSS WITH DEBUG
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
# ARTICLE FETCH + TEXT CLEAN
# =============================================================================

def fetch_article_html(url: str) -> Tuple[Optional[str], Optional[int]]:
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
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", html).strip()


# =============================================================================
# EXTRACTION (Name / Age / Street) WITH BLOCKLIST
# =============================================================================

def is_blocked_name(candidate: str) -> bool:
    """
    Returns True if candidate looks like a publisher/site phrase rather than a person.
    """
    c = candidate.strip().lower()
    if not c:
        return True

    # Exact or contains any blocked phrase
    for phrase in BLOCKLIST_PHRASES:
        if phrase in c:
            return True

    # Very common "non-person" patterns
    if "news" in c and len(c.split()) >= 2:
        # e.g. "Evening News" (often brand)
        return True

    return False


def extract_name_age_street(text: str, title: str = "") -> Dict[str, Optional[object]]:
    """
    Improved extraction:
    1) Prefer names that appear near an age (court reporting style)
    2) Otherwise try a generic name match
    3) Reject names that match publisher/site phrases (blocklist)
    """
    name = None
    age = None
    street = None

    # 1) Prefer "Name, 34" or "Name aged 34"
    # Search in title+text because title sometimes has the defendant
    combined_for_name = f"{title} {text}"

    nm_age = NAME_NEAR_AGE_RE.search(combined_for_name)
    if nm_age:
        cand_name = nm_age.group(1).strip()
        cand_age = nm_age.group(2).strip()

        if not is_blocked_name(cand_name):
            name = cand_name
            try:
                age = int(cand_age)
            except Exception:
                pass

    # 2) If still no age from the near-age pattern, try regular age patterns
    if age is None:
        for ar in AGE_RES:
            am = ar.search(text)
            if am:
                try:
                    age = int(am.group(1))
                except Exception:
                    pass
                break

    # 3) If still no name, try generic name match, but blocklist it
    if name is None:
        m = NAME_RE.search(title) or NAME_RE.search(text)
        if m:
            cand_name = m.group(1).strip()
            if not is_blocked_name(cand_name):
                name = cand_name

    # 4) Street
    sm = STREET_RE.search(text)
    if sm:
        street = sm.group(1).strip()

    # Confidence score (simple but explainable)
    conf = 0.0
    conf += 0.50 if name else 0.0
    conf += 0.35 if age is not None else 0.0
    conf += 0.15 if street else 0.0

    return {"name": name, "age": age, "street": street, "confidence": round(conf, 2)}


# =============================================================================
# INGESTION
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
                    pass

            print(f"[{source}] New articles inserted: {inserted_count}")
            print(f"[{source}] Court candidates flagged: {court_candidate_count}")


# =============================================================================
# PARSE ARTICLES + STORE MENTIONS
# =============================================================================

def parse_court_candidate_articles() -> None:
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

            conn.execute(
                "UPDATE articles SET article_http_status = ?, article_fetched_at = ? WHERE id = ?",
                (status, now_iso(), article_id)
            )

            if not html:
                parsed_fail += 1
                continue

            text = html_to_text(html)

            extracted = extract_name_age_street(text, title=title)

            # Only store if we got something useful
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
# QUICK STATS
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


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    print("Initialising / migrating database...")
    init_db_and_migrate()

    print("\nIngesting RSS feeds...")
    ingest_rss_feeds()

    if FETCH_ARTICLE_PAGES:
        parse_court_candidate_articles()

    print_quick_stats()

    print("\nDone.")
    print("\nDB Browser demo queries:")

    print("\n1) Show newest extracted people:")
    print("""
SELECT m.extracted_at, a.source, m.person_name, m.age, m.street, m.confidence, a.title, a.url
FROM mentions m
JOIN articles a ON a.id = m.article_id
ORDER BY m.id DESC
LIMIT 50;
""".strip())

    print("\n2) Show rows where we extracted a blocked-looking name (should be NONE after this change):")
    print("""
SELECT m.extracted_at, a.source, m.person_name, a.title, a.url
FROM mentions m
JOIN articles a ON a.id = m.article_id
WHERE m.person_name IS NOT NULL
  AND (LOWER(m.person_name) LIKE '%news%' OR LOWER(m.person_name) LIKE '%mail%')
ORDER BY m.id DESC
LIMIT 50;
""".strip())


if __name__ == "__main__":
    main()
