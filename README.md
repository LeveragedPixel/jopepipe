# JobPipe

Cloud job pipeline: GitHub Actions scrapes remote job listings daily, scores
them against my profile with Claude, and publishes a dashboard to GitHub
Pages. Nothing runs locally, $0/month. Full design in [SPEC.md](SPEC.md).

**Discovery + tailoring only — apply manually. No auto-apply.**

```
GitHub Actions (daily, 6am MT)
  scraper.py  → normalize → dedupe → data/jobs.json
  score.py    → Claude adds score/strengths/gaps/flags → jobs.json
  commit + push
GitHub Pages
  index.html  ← the dashboard
```

## One-time setup

1. **Claude token** — on any machine with Claude Code installed, run
   `claude setup-token` and copy the OAuth token. In this repo:
   *Settings → Secrets and variables → Actions → New repository secret*,
   name `CLAUDE_CODE_OAUTH_TOKEN`. (Uses your Claude subscription, not API
   billing. If missing, the pipeline still publishes unscored jobs with a
   dashboard warning.)
2. **Resume context (recommended)** — add a second secret `RESUME_CONTEXT`
   containing your resume as plain text. Scoring uses it at runtime; without
   it, a generic profile from SPEC.md is used. This is the secret-injection
   approach — the resume never needs to live in the repo.
3. **GitHub Pages** — *Settings → Pages → Source: Deploy from a branch*,
   pick the default branch, folder `/ (root)`. Dashboard appears at
   `https://<user>.github.io/<repo>/`.
4. **First run** — *Actions → JobPipe daily pipeline → Run workflow*. After
   it goes green, refresh the dashboard. It then runs daily at
   `0 12 * * *` UTC (6am MT).

> ⚠️ **Privacy:** `resume.md` in this repo is a real resume with contact
> info. If the repo is (or becomes) public, delete it and rely on the
> `RESUME_CONTEXT` secret instead — deleting also requires scrubbing git
> history (`git filter-repo`) since past commits keep the content.

## Files

| File | Purpose |
|---|---|
| `.github/workflows/pipeline.yml` | Daily cron: scrape → score → commit |
| `scraper.py` | jobspy boards + RemoteOK API + We Work Remotely RSS (stdlib XML parse) → `data/jobs.json` |
| `score.py` | Batches of 10 unscored jobs → Claude CLI → score/strengths/gaps/flags/pitch |
| `index.html` | Dashboard: ranked cards, filters, Applied/Save/Hide (localStorage) |
| `data/jobs.json` | Flat-file store; git history is the history |

## Behavior notes

- **Dedupe:** `sha256(lower(title)+lower(company))` — first URL wins, other
  boards land in `alt_sources`.
- **Blocked sources:** each board call retries then logs to
  `meta.source_failures`; the dashboard header shows a warning. RemoteOK/WWR
  (API/RSS) keep working even when the big boards block datacenter IPs.
- **Scoring cap:** 12 batches (120 jobs) per run; the backlog note appears in
  the dashboard and the next run continues.
- **Status buttons** (Applied / Save / Hide) live in your browser's
  localStorage — per-device, survives visits, no server.
