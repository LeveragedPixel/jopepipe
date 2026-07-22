#!/usr/bin/env python3
"""JobPipe scoring — runs inside GitHub Actions after scraper.py.

Scores unscored jobs in data/jobs.json against the resume context using the
Claude Code CLI authenticated with CLAUDE_CODE_OAUTH_TOKEN (subscription
token from `claude setup-token`, not API billing).

Failure policy (see SPEC.md): if auth/scoring fails, write a warning into
meta.scoring_warning and exit 0 — the pipeline still publishes unscored jobs
and the dashboard shows a warning banner instead of the run going red.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
PROFILES_PATH = os.path.join(ROOT, "profiles.json")

BATCH_SIZE = 10
# Total scoring batches per run, shared across all profiles, to bound token spend.
MAX_BATCHES = int(os.environ.get("MAX_SCORE_BATCHES", "12"))
BATCH_TIMEOUT_S = 300
NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Fallback profile from SPEC.md. Set the RESUME_CONTEXT repo secret to inject
# the full resume at runtime instead (secret-injection version — the resume
# itself is never required in the repo).
DEFAULT_PROFILE = """\
- 10+ years in Customer Success, technical onboarding, and IT support
- Target titles: Customer Success Manager, Onboarding Specialist,
  Implementation Specialist, Technical Account Manager, Client Success
- Target industries: SaaS, fintech/payments, legal tech, healthcare tech
- Remote (US), full-time only
- Market rate: ~$59K-$99K, average ~$83K; anything under $55K is low comp
- Tools: ServiceNow, Salesforce, SQL, Active Directory, Google Workspace
"""

PROMPT_TEMPLATE = """\
You are scoring job listings for a job seeker. Their profile:

{profile}

Score each job below 0-100 for fit with this profile. Consider title match,
industry, seniority, remote availability, and compensation vs. market rate.

Flags to detect (include in "flags" only when applicable):
- "low comp": stated compensation below $55K/year
- "location-restricted": says remote but restricts to specific states/cities
- "sales-quota role": a quota-carrying sales role disguised as customer success
- "seniority mismatch": clearly too junior or too senior (director/VP) for the profile

Jobs to score:
{jobs_json}

Reply with ONLY a JSON array, no markdown fences, no commentary. One object
per job, exactly this shape:
[{{"id": "<id from input>", "score": 0-100, "strengths": ["..."], "gaps": ["..."], "flags": ["..."], "one_line_pitch": "one sentence on why this fits"}}]
"""


def load_data(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_data(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)


def load_profiles():
    """Which profiles to score, each with its own resume secret and data file."""
    try:
        with open(PROFILES_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        profs = [p for p in cfg.get("profiles", [])
                 if p.get("search_terms") and
                 not any("EDIT ME" in t for t in p["search_terms"])]
        if profs:
            return profs
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return [{"id": "leigh", "name": "Leigh", "resume_secret": "RESUME_CONTEXT"}]


def job_for_prompt(job):
    return {
        "id": job["id"],
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "compensation": job.get("comp_text") or "not stated",
        "source": job["source"],
        "description": (job.get("description") or "")[:1200],
    }


def extract_json_array(text):
    """Pull the first JSON array out of a model response, fences and all."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in response")
    return json.loads(text[start:end + 1])


def score_batch(profile, batch):
    prompt = PROMPT_TEMPLATE.format(
        profile=profile,
        jobs_json=json.dumps([job_for_prompt(j) for j in batch], ensure_ascii=False),
    )
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=BATCH_TIMEOUT_S,
    )
    if result.returncode != 0:
        # The CLI often reports errors (auth, usage limits) on stdout, not stderr
        detail = (result.stderr.strip() or result.stdout.strip())[:300]
        raise RuntimeError(f"claude exited {result.returncode}: {detail}")
    return extract_json_array(result.stdout)


def apply_scores(jobs_by_id, results):
    applied = 0
    for row in results:
        job = jobs_by_id.get(row.get("id"))
        if job is None:
            continue
        try:
            score = max(0, min(100, int(row["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        job["score"] = score
        job["strengths"] = [str(s) for s in row.get("strengths") or []][:5]
        job["gaps"] = [str(g) for g in row.get("gaps") or []][:5]
        job["flags"] = [str(f) for f in row.get("flags") or []][:5]
        job["one_line_pitch"] = str(row.get("one_line_pitch") or "") or None
        job["scored_at"] = NOW_ISO
        applied += 1
    return applied


def score_profile(profile, resume, budget):
    """Score up to `budget` batches for one profile. Returns batches consumed."""
    pid = profile["id"]
    path = os.path.join(DATA_DIR, f"jobs-{pid}.json")
    try:
        data = load_data(path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[warn] profile '{pid}': cannot load {path}: {exc}; skipping")
        return 0

    meta = data.setdefault("meta", {})
    jobs = data.get("jobs") or []
    jobs_by_id = {j["id"]: j for j in jobs}
    unscored = [j for j in jobs if j.get("score") is None]

    def finalize(warning):
        remaining = sum(1 for j in jobs if j.get("score") is None)
        if warning is None and remaining:
            warning = f"{remaining} jobs still unscored (batch cap); next run will continue."
        meta["scoring_warning"] = warning
        meta["high_matches"] = sum(1 for j in jobs if (j.get("score") or 0) >= 85)
        jobs.sort(key=lambda j: ((j.get("score") or -1), j.get("first_seen") or ""), reverse=True)
        data["jobs"] = jobs
        save_data(path, data)

    if not unscored:
        finalize(None)
        print(f"[done] profile '{pid}': nothing to score")
        return 0

    all_batches = [unscored[i:i + BATCH_SIZE] for i in range(0, len(unscored), BATCH_SIZE)]
    batches = all_batches[:budget]
    print(f"[info] profile '{pid}': {len(all_batches)} batches pending, running {len(batches)}")

    warning, consumed = None, 0
    for idx, batch in enumerate(batches, 1):
        try:
            results = score_batch(resume, batch)
        except Exception as exc:  # noqa: BLE001 — degrade to unscored, never fail the run
            warning = f"Scoring stopped at batch {idx}: {str(exc)[:200]}"
            print(f"[warn] profile '{pid}': {warning}")
            break
        applied = apply_scores(jobs_by_id, results)
        consumed += 1
        print(f"[ok] profile '{pid}' batch {idx}/{len(batches)}: scored {applied}/{len(batch)}")
        save_data(path, data)  # checkpoint after every batch
    finalize(warning)
    return consumed


def main():
    profiles = load_profiles()

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        warn = ("Scoring skipped: CLAUDE_CODE_OAUTH_TOKEN secret is not set. "
                "Run `claude setup-token` and add it as a repo secret.")
        for p in profiles:
            path = os.path.join(DATA_DIR, f"jobs-{p['id']}.json")
            try:
                data = load_data(path)
                data.setdefault("meta", {})["scoring_warning"] = warn
                save_data(path, data)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        print("[warn] no CLAUDE_CODE_OAUTH_TOKEN; publishing unscored jobs")
        return

    token = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    wellformed = bool(re.fullmatch(r"sk-ant-oat\d{2}-[A-Za-z0-9_\-]+", token))
    print(f"[info] token: {len(token)} chars, wellformed={wellformed}")
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    print(f"[info] claude CLI: {version.stdout.strip() or version.stderr.strip()}")

    # Shared batch budget across profiles keeps daily token spend bounded.
    budget = MAX_BATCHES
    for profile in profiles:
        if budget <= 0:
            print(f"[info] batch budget exhausted; profile '{profile['id']}' waits for next run")
            break
        secret = profile.get("resume_secret", "RESUME_CONTEXT")
        resume = os.environ.get(secret) or DEFAULT_PROFILE
        if not os.environ.get(secret):
            print(f"[info] profile '{profile['id']}': {secret} not set, using generic profile")
        budget -= score_profile(profile, resume, budget)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] scoring crashed ({exc}); exiting 0 so the pipeline still publishes")
        sys.exit(0)
