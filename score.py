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

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jobs.json")

BATCH_SIZE = 10
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


def load_data():
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)


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


def main():
    try:
        data = load_data()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[warn] cannot load {DATA_PATH}: {exc}; nothing to score")
        return

    meta = data.setdefault("meta", {})
    jobs = data.get("jobs") or []
    jobs_by_id = {j["id"]: j for j in jobs}
    unscored = [j for j in jobs if j.get("score") is None]

    if not unscored:
        meta["scoring_warning"] = None
        meta["high_matches"] = sum(1 for j in jobs if (j.get("score") or 0) >= 85)
        save_data(data)
        print("[done] nothing to score")
        return

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        meta["scoring_warning"] = ("Scoring skipped: CLAUDE_CODE_OAUTH_TOKEN secret is not set. "
                                   "Run `claude setup-token` and add it as a repo secret.")
        save_data(data)
        print("[warn] no CLAUDE_CODE_OAUTH_TOKEN; publishing unscored jobs")
        return

    token = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    wellformed = bool(re.fullmatch(r"sk-ant-oat\d{2}-[A-Za-z0-9_\-]+", token))
    print(f"[info] token: {len(token)} chars, wellformed={wellformed}")
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    print(f"[info] claude CLI: {version.stdout.strip() or version.stderr.strip()}")

    profile = os.environ.get("RESUME_CONTEXT") or DEFAULT_PROFILE
    warning = None
    scored_total = 0
    batches = [unscored[i:i + BATCH_SIZE] for i in range(0, len(unscored), BATCH_SIZE)]
    if len(batches) > MAX_BATCHES:
        print(f"[info] {len(batches)} batches pending, capping at {MAX_BATCHES} this run")
        batches = batches[:MAX_BATCHES]

    for idx, batch in enumerate(batches, 1):
        try:
            results = score_batch(profile, batch)
        except Exception as exc:  # noqa: BLE001 — degrade to unscored, never fail the run
            warning = f"Scoring stopped at batch {idx}/{len(batches)}: {str(exc)[:200]}"
            print(f"[warn] {warning}")
            break
        applied = apply_scores(jobs_by_id, results)
        scored_total += applied
        print(f"[ok] batch {idx}/{len(batches)}: scored {applied}/{len(batch)}")
        save_data(data)  # checkpoint after every batch

    remaining = sum(1 for j in jobs if j.get("score") is None)
    if warning is None and remaining:
        warning = f"{remaining} jobs still unscored (batch cap); next run will continue."
    meta["scoring_warning"] = warning
    meta["high_matches"] = sum(1 for j in jobs if (j.get("score") or 0) >= 85)
    jobs.sort(key=lambda j: ((j.get("score") or -1), j.get("first_seen") or ""), reverse=True)
    data["jobs"] = jobs
    save_data(data)
    print(f"[done] scored {scored_total} jobs, {remaining} remaining, warning={warning!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] scoring crashed ({exc}); exiting 0 so the pipeline still publishes")
        sys.exit(0)
