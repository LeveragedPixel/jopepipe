#!/usr/bin/env python3
"""JobPipe scraper — runs inside GitHub Actions.

Scrapes remote job listings from python-jobspy boards (Indeed, LinkedIn,
ZipRecruiter, Glassdoor, Google Jobs), the RemoteOK public API, and the
We Work Remotely RSS feed. Normalizes, dedupes, merges into data/jobs.json.

Design constraints (see SPEC.md):
- One dead source must never kill the run: every source call is wrapped,
  failures are logged into meta.source_failures for the dashboard header.
- Gentle rate limiting: 2-5s random delay between board calls, with retries.
- Dedupe key: sha256(lower(title)+lower(company)); first URL wins, other
  sources are appended to alt_sources.
- Always exits 0 and always writes data/jobs.json so the publish step runs.
"""

import hashlib
import json
import os
import random
import re
import time
import traceback
from datetime import datetime, timezone

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jobs.json")

SEARCH_TERMS = [
    "Customer Success Manager",
    "Customer Onboarding Specialist",
    "Implementation Specialist",
    "Technical Account Manager",
    "Client Success Manager",
    "Customer Success Manager SaaS",
    "Customer Success Manager fintech",
    "Customer Success Manager healthcare",
]

JOBSPY_SITES = ["indeed", "linkedin", "zip_recruiter", "glassdoor", "google"]

# Titles must look like the target roles to keep API/RSS sources on-topic.
TITLE_PATTERN = re.compile(
    r"customer success|client success|onboarding|implementation"
    r"|technical account manager|customer experience manager",
    re.IGNORECASE,
)

RESULTS_PER_QUERY = int(os.environ.get("RESULTS_PER_QUERY", "20"))
HOURS_OLD = int(os.environ.get("HOURS_OLD", "72"))
MAX_DESCRIPTION_CHARS = 1500

HOURLY_TO_ANNUAL = 2080
now_utc = datetime.now(timezone.utc)
NOW_ISO = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

failures = []  # [{"source": str, "error": str}]


def polite_sleep():
    time.sleep(random.uniform(2, 5))


def job_id(title, company):
    key = (title or "").strip().lower() + (company or "").strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def annualize(amount, interval):
    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    factor = {
        "yearly": 1,
        "hourly": HOURLY_TO_ANNUAL,
        "daily": 260,
        "weekly": 52,
        "monthly": 12,
    }.get((interval or "yearly").lower(), 1)
    value = amount * factor
    # A bare number under 1000 with no interval is almost always an hourly rate.
    if factor == 1 and value < 1000:
        value *= HOURLY_TO_ANNUAL
    return int(round(value))


def clean_text(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text[:MAX_DESCRIPTION_CHARS] or None


def make_job(title, company, url, source, location=None, comp_min=None,
             comp_max=None, comp_text=None, date_posted=None, description=None):
    return {
        "id": job_id(title, company),
        "title": (title or "").strip(),
        "company": (company or "").strip() or "Unknown",
        "location": (location or "").strip() or "Remote",
        "comp_min": comp_min,
        "comp_max": comp_max,
        "comp_text": comp_text,
        "url": url,
        "source": source,
        "alt_sources": [],
        "date_posted": date_posted,
        "first_seen": NOW_ISO,
        "description": clean_text(description),
        "score": None,
        "strengths": [],
        "gaps": [],
        "flags": [],
        "one_line_pitch": None,
        "scored_at": None,
    }


def record_failure(source, exc):
    msg = f"{type(exc).__name__}: {exc}"[:300]
    print(f"[FAIL] {source}: {msg}")
    failures.append({"source": source, "error": msg})


def with_retries(source, fn, attempts=2):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — a dead source must not kill the run
            if attempt == attempts:
                record_failure(source, exc)
                return None
            print(f"[retry] {source} attempt {attempt} failed: {exc}")
            time.sleep(attempt * 5)
    return None


# ---------------------------------------------------------------- jobspy boards

def scrape_jobspy():
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        record_failure("jobspy", exc)
        return []

    jobs = []
    for site in JOBSPY_SITES:
        site_jobs = 0
        for term in SEARCH_TERMS:
            def call(site=site, term=term):
                return scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    location="United States",
                    is_remote=True,
                    job_type="fulltime",
                    results_wanted=RESULTS_PER_QUERY,
                    hours_old=HOURS_OLD,
                    country_indeed="USA",
                    verbose=0,
                )
            df = with_retries(f"{site}:{term}", call)
            polite_sleep()
            if df is None or df.empty:
                continue
            for row in df.to_dict("records"):
                title = row.get("title")
                if not title or not row.get("job_url"):
                    continue
                cmin = annualize(row.get("min_amount"), row.get("interval"))
                cmax = annualize(row.get("max_amount"), row.get("interval"))
                date_posted = row.get("date_posted")
                if date_posted is not None and not isinstance(date_posted, str):
                    date_posted = str(date_posted)[:10]
                jobs.append(make_job(
                    title=title,
                    company=row.get("company"),
                    url=row.get("job_url"),
                    source=site,
                    location=row.get("location"),
                    comp_min=cmin,
                    comp_max=cmax,
                    comp_text=f"${cmin:,}–${cmax:,}" if cmin and cmax else None,
                    date_posted=date_posted if date_posted and date_posted != "NaT" else None,
                    description=row.get("description"),
                ))
                site_jobs += 1
        print(f"[ok] {site}: {site_jobs} jobs")
    return jobs


# ------------------------------------------------------------------- RemoteOK

US_OK = re.compile(r"^\s*$|usa|united states|north america|americas|worldwide|anywhere|remote",
                   re.IGNORECASE)


def scrape_remoteok():
    import requests

    def call():
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobPipe/1.0 (personal job search dashboard)"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    payload = with_retries("remoteok", call)
    if not payload:
        return []

    jobs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("position"):
            continue  # first element is the API legal notice
        title = item["position"]
        if not TITLE_PATTERN.search(title):
            continue
        if not US_OK.search(item.get("location") or ""):
            continue
        cmin = annualize(item.get("salary_min"), "yearly")
        cmax = annualize(item.get("salary_max"), "yearly")
        jobs.append(make_job(
            title=title,
            company=item.get("company"),
            url=item.get("url"),
            source="remoteok",
            location=item.get("location") or "Remote",
            comp_min=cmin,
            comp_max=cmax,
            comp_text=f"${cmin:,}–${cmax:,}" if cmin and cmax else None,
            date_posted=(item.get("date") or "")[:10] or None,
            description=re.sub(r"<[^>]+>", " ", item.get("description") or ""),
        ))
    print(f"[ok] remoteok: {len(jobs)} jobs")
    return jobs


# ------------------------------------------------------------- We Work Remotely

WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "https://weworkremotely.com/remote-jobs.rss",
]


def scrape_wwr():
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    import requests

    def item_text(item, tag):
        # Tags may or may not carry a namespace; match on the local name.
        for el in item.iter():
            if el.tag.rsplit("}", 1)[-1].lower() == tag:
                return (el.text or "").strip()
        return ""

    jobs = []
    seen_links = set()
    for feed_url in WWR_FEEDS:
        def call(feed_url=feed_url):
            resp = requests.get(
                feed_url,
                headers={"User-Agent": "JobPipe/1.0 (personal job search dashboard)"},
                timeout=30,
            )
            resp.raise_for_status()
            return ET.fromstring(resp.content)
        root = with_retries(f"weworkremotely:{feed_url.rsplit('/', 1)[-1]}", call)
        polite_sleep()
        if root is None:
            continue
        for item in root.iter("item"):
            link = item_text(item, "link")
            raw_title = item_text(item, "title")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            # WWR titles look like "Company: Job Title"
            company, _, title = raw_title.partition(":")
            if not title:
                title, company = raw_title, ""
            if not TITLE_PATTERN.search(title):
                continue
            region = item_text(item, "region")
            if region and not US_OK.search(region):
                continue
            date_posted = None
            pub = item_text(item, "pubdate")
            if pub:
                try:
                    date_posted = parsedate_to_datetime(pub).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    pass
            jobs.append(make_job(
                title=title.strip(),
                company=company.strip(),
                url=link,
                source="weworkremotely",
                location=region or "Remote",
                date_posted=date_posted,
                description=re.sub(r"<[^>]+>", " ", item_text(item, "description")),
            ))
    print(f"[ok] weworkremotely: {len(jobs)} jobs")
    return jobs


# ------------------------------------------------------------------ merge/save

def load_existing():
    try:
        with open(DATA_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"meta": {}, "jobs": []}


def merge(existing_jobs, scraped_jobs):
    by_id = {j["id"]: j for j in existing_jobs if j.get("id")}
    new_count = 0
    for job in scraped_jobs:
        if not job["title"]:
            continue
        current = by_id.get(job["id"])
        if current is None:
            by_id[job["id"]] = job
            new_count += 1
            continue
        # Known job: keep first URL/source & score, note the extra source.
        if job["source"] != current["source"] and job["source"] not in current["alt_sources"]:
            current["alt_sources"].append(job["source"])
        for field in ("comp_min", "comp_max", "comp_text", "date_posted", "description"):
            if not current.get(field) and job.get(field):
                current[field] = job[field]
    return list(by_id.values()), new_count


def main():
    scraped = []
    scraped += scrape_jobspy()
    scraped += scrape_remoteok()
    scraped += scrape_wwr()

    data = load_existing()
    jobs, new_count = merge(data["jobs"], scraped)
    jobs.sort(key=lambda j: ((j.get("score") or -1), j.get("first_seen") or ""), reverse=True)

    meta = data.get("meta") or {}
    meta.update({
        "last_run": NOW_ISO,
        "total": len(jobs),
        "scraped_this_run": len(scraped),
        "new_this_run": new_count,
        "high_matches": sum(1 for j in jobs if (j.get("score") or 0) >= 85),
        "source_failures": failures,
        "scoring_warning": meta.get("scoring_warning"),
    })

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "jobs": jobs}, fh, indent=1, ensure_ascii=False)
    print(f"[done] {len(scraped)} scraped, {new_count} new, {len(jobs)} total, "
          f"{len(failures)} source failures")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — never fail the workflow; publish what we have
        traceback.print_exc()
        print("[warn] scraper crashed; exiting 0 so the pipeline still publishes")
