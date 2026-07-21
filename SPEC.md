# JobPipe — Cloud Job Scraper + Web Dashboard

**Handoff spec for Claude Code (web).** Put this in a new GitHub repo as `SPEC.md`, then tell Claude Code: *"Read SPEC.md and build Phase 1."*

---

## What this is

A fully cloud-hosted job pipeline. GitHub Actions scrapes remote job listings daily, scores them against my resume with Claude, and publishes the results to a **web dashboard on GitHub Pages** that I open from any browser (work, phone, anywhere). I never run anything locally.

**Discovery + tailoring only — I click "apply" myself.** No auto-apply.

## About me (scoring context)

- 10+ years in Customer Success, technical onboarding, IT support
- Target titles: Customer Success Manager, Onboarding Specialist, Implementation Specialist, Technical Account Manager, Client Success
- Target industries: SaaS, fintech/payments, legal tech, healthcare tech
- Remote (US), full-time
- Market rate: ~$59K–$99K, avg ~$83K — flag anything under $55K as "low comp"

## Architecture — everything in the cloud

```
GitHub Actions (daily cron)
  └─ scraper.py  → normalize → dedupe → data/jobs.json
  └─ scoring step (Claude)   → adds score + reasons to jobs.json
  └─ git commit + push
GitHub Pages (auto-deploy)
  └─ index.html  ← the dashboard I actually use
```

- **Runner:** GitHub Actions, free tier (scheduled workflow, `cron: "0 12 * * *"` = 6am MT)
- **Storage:** flat `data/jobs.json` committed to the repo (no database server; git history IS the history)
- **UI hosting:** GitHub Pages, free, `https://<user>.github.io/jobpipe/`
- **My cost: $0/month**

### Scraping (runs inside Actions)
- `python-jobspy` for Indeed, LinkedIn, ZipRecruiter, Glassdoor, Google Jobs
- RemoteOK public JSON API, We Work Remotely RSS
- Queries: "Customer Success Manager", "Customer Onboarding Specialist", "Implementation Specialist", "Technical Account Manager", "Client Success" (+ SaaS/fintech/healthcare variants), remote/US/fulltime
- Dedupe key: sha256(lower(title)+lower(company)); keep first URL, list alt sources
- Rate-limit gently (2–5s random delays); one dead source must not kill the run
- **Known risk:** job boards sometimes block datacenter IPs. Build retries + per-source failure logging. If a source is consistently blocked from Actions, fall back to RemoteOK/WWR (API/RSS — never blocked) and note it in the dashboard header.

### Scoring with my Max subscription (no API key)
- Use the **claude-code-action** GitHub Action authenticated with a `CLAUDE_CODE_OAUTH_TOKEN` repo secret (generated via `claude setup-token` — uses my subscription, not API billing)
- Prompt per batch of 10 jobs, return strict JSON: `{id, score (0-100), strengths[], gaps[], flags[], one_line_pitch}`
- Only score jobs not already scored (check jobs.json)
- Flags to detect: below-market comp, "remote" but location-restricted, sales-quota roles disguised as CS, seniority mismatch
- If token/auth fails, pipeline still publishes unscored jobs with a warning banner rather than failing

### The dashboard (`index.html` — the part I use)
Single static page, no build step, vanilla JS + fetch of `data/jobs.json`. Mobile-friendly (I'll check it from my phone at work).

- **Ranked cards**, score ≥70 by default: title (links to posting), company, comp, source, score badge, one-line pitch, top strength, top gap/flag. ⭐ on 85+
- **Filters:** min score slider, source, comp floor, posted-within-X-days, search box
- **Status tracking:** buttons per card — Applied / Hide / Save. Stored in browser localStorage (solo user, good enough; survives across visits on the same device)
- **Header:** last run date, totals (scraped / new / high-matches), any source-failure warnings
- Dark theme, dense layout, fast. No frameworks needed.

### Phase plan
1. **Phase 1:** Actions workflow (scrape → score → commit) + dashboard with ranked cards, filters, localStorage status. Definition of done: cron runs green, dashboard URL shows real scored listings with working links.
2. **Phase 2:** "Tailor" button per job → opens a pre-written prompt I can paste into Claude to generate a tuned resume/cover letter; applied-status export; per-company research links (Glassdoor/LinkedIn)
3. **Phase 3:** email/Discord ping when a ≥85 job appears; trend view (score distribution over time); optional private repo + Pages access control

### Guardrails
- Never auto-submit applications
- Respect robots.txt and rate limits
- Repo secrets only (`CLAUDE_CODE_OAUTH_TOKEN`); nothing hardcoded
- Repo can be public (Pages free) — but jobs.json is just public job listings + scores, no personal data. Resume used for scoring stays in the repo → make repo **private** and use Pages on private repo IF my plan allows; otherwise keep resume content in a repo secret/gist and inject at runtime. Claude Code: implement the secret-injection version by default.
