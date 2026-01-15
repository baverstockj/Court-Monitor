# Court Monitor MVP (RSS → Structured Court Mentions)

This is a small MVP I built to show what’s possible using **publicly available local news RSS feeds** to monitor for **court / criminal justice outcomes**, store the results, and extract basic structured fields for later screening/review.

It’s intentionally simple and explainable: it does **not** make decisions. It just ingests, flags, extracts, and stores data so a human can review and decide what’s relevant.

---

## What it does

- Polls multiple UK local news **RSS feeds**
- Logs **HTTP status codes** and feed parsing results (so you can see what’s working / blocked)
- Stores new articles in a local **SQLite database**
- Flags likely relevant items using a **tight “action-word” filter** (e.g. *arrested / charged / convicted / bailed / sentenced*)
- Fetches the full article page for flagged items and extracts (best effort):
  - **Person name**
  - **Age**
  - **Street name** (if mentioned)
- Stores extracted results in a structured table for later searching/analysis
- Includes a simple blocklist to avoid extracting publisher names as “people”

---

## Why I built this

As an insurer, part of risk assessment is understanding potential indicators of fraud and increased risk. Court reporting is one public source of information that can provide useful context, but it’s unstructured and scattered across many sites.

This MVP demonstrates:
- ongoing monitoring
- searchable storage
- explainable keyword-based filtering
- basic entity extraction (name/age/location cues)

All data is taken from **publicly available sources**.

---

## Important notes (MVP reality)

- Accuracy is **not guaranteed** (entity extraction uses heuristics / regex).
- Some publishers may block automated requests, show cookie/consent pages, or change layouts.
- This tool is designed for **human review**, not automated underwriting decisions.
- Make sure use of this kind of data fits your internal governance, legal/compliance framework, and any relevant UK regulations.

---

## Tech overview

- Python 3
- `requests` for fetching RSS + article pages (with a custom User-Agent)
- `feedparser` for parsing RSS XML
- SQLite for storage (single file database)
- Regex-based extraction for name/age/street (MVP-level)

---

## Getting started

### 1) Install dependencies

```bash
pip install feedparser requests
