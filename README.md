# JobRadar

Automated, near-zero-cost job finder. Scrapes 10 sources daily, scores every
listing with Gemini 2.0 Flash (experience gate, visa check, role-fit), writes
scored results to a Google Sheet, and emails a digest — all on free tiers +
~$5/month of Apify.

Two pipelines: **India** (≥25 LPA floor) and **International** (UK/UAE/Europe/
Remote, visa-NO hard-dropped). Target: 50–75 scored, relevant jobs/day.

```
scrape → pre-filter → dedup → LLM score → Google Sheet + email digest
```

## Layout

```
jobRadar/
├── config/            sources.yaml, pipelines.yaml, scoring.yaml, .env.example
├── scrapers/          base + Apify (Indeed/LinkedIn/Naukri/Bayt/Glassdoor) +
│                      Adzuna, Reed, RemoteOK, Serper, Gmail alerts
├── processing/        models (Pydantic) · prefilter · dedup · scorer (Gemini)
├── output/            sheets_writer · digest_builder · gmail_sender
├── scripts/           gmail_authorize.py (one-time OAuth token)
├── main.py            orchestrator
├── setup_tracker.py   one-time Google Sheet creation
└── .github/workflows/daily_scan.yml   cron @ 4:00 AM IST
```

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config/.env.example .env        # then fill in your keys
```

### 1. Get the free API keys

| Secret | Where | Notes |
|---|---|---|
| `APIFY_TOKEN` | apify.com → Settings → API tokens | ~$5/mo credit |
| `GEMINI_API_KEY` | aistudio.google.com → Get API key | free tier |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | developer.adzuna.com | free |
| `REED_API_KEY` | reed.co.uk/developers | free |
| `SERPER_API_KEY` | serper.dev | 2,500/mo free |
| `GOOGLE_SHEETS_CREDENTIALS` | Cloud Console → Service Account → JSON key | paste full JSON |
| `GMAIL_APP_PASSWORD` | Google Account → App Passwords (needs 2FA) | for sending |
| `GMAIL_OAUTH_TOKEN` | `python scripts/gmail_authorize.py` | optional (alerts) |

Nothing is committed — locally these live in `.env`, in CI they are GitHub
Actions secrets.

### 2. Create the tracker (once)

Enable the Google Sheets + Drive APIs on your Cloud project. Because a **service
account has no Drive storage of its own**, it cannot create a spreadsheet — you
create it and let the service account write into it:

1. In your own Google Drive, create a blank Google Sheet.
2. Share it as **Editor** with the service account email (the `client_email`
   field in your JSON — `setup_tracker.py` also prints it).
3. Copy the id from the URL
   `https://docs.google.com/spreadsheets/d/<ID>/edit` and set
   `JOBRADAR_SHEET_ID=<ID>` in `.env` (and the GitHub secret).
4. Apply the schema + formatting:

```bash
python setup_tracker.py
```

`setup_tracker.py` opens the sheet by `JOBRADAR_SHEET_ID`, adds the `Tracker`
worksheet, writes the headers, freezes the header row, hides the dedup column,
and applies the conditional formatting. Re-running is safe.

### 3. Run it

```bash
python main.py --dry-run            # scrape+filter+score, print top 15, no writes
python main.py --limit 40           # cap scorer input for a cheap real run
python main.py                      # full run: writes Sheet + sends digest
```

Useful flags: `--no-email`, `--no-sheets`.

The run **degrades instead of crashing**: any source missing its key is skipped,
any failing API is logged and ignored, and if `GEMINI_API_KEY` is absent the
scorer falls back to a deterministic rule-based scorer so you still get output.

Results are written to **two tabs** in the tracker spreadsheet — `India` and
`International` — and the email digest keeps the same split.

### 4. Automate — pick one

**A. Windows Task Scheduler (local; simplest, uses your `.env`).** Runs only
while the PC is on. `run_daily.ps1` invokes the venv Python and logs to `logs/`.
Register a 4:00 AM daily task (run once, in PowerShell):

```powershell
schtasks /Create /SC DAILY /ST 04:00 /TN "JobRadar" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\college\PROJECTS\ML\jobRadar\run_daily.ps1\""
```

Test it immediately with `schtasks /Run /TN "JobRadar"`; remove it with
`schtasks /Delete /TN "JobRadar" /F`.

**B. GitHub Actions (cloud; runs even when your PC is off).** Push to GitHub, add
every secret under **Settings → Secrets and variables → Actions**, and the
workflow runs daily at 4:00 AM IST (`workflow_dispatch` triggers it manually).
For `GOOGLE_SHEETS_CREDENTIALS` paste the **JSON contents** (not a file path —
the `credentials/` folder is git-ignored). Logs upload as an artifact.

## Diagnostics

- `python scripts/gemini_check.py` — verifies your Gemini key authenticates and
  the configured model is reachable (distinguishes a bad key from a wrong/retired
  model name), and lists the models your key can actually use. Flash models get
  retired periodically; `scoring.yaml` defaults to `gemini-flash-latest` so the
  scorer keeps working across retirements.
- Apify cost/coverage: `apify.queries_per_run` and each actor's `max_items` in
  `config/sources.yaml`. More queries = wider coverage, higher spend.

## Tuning

- **Which titles pass** — `title_keywords_regex` / `title_exclusion_regex` in
  `config/pipelines.yaml`.
- **Geography routing & salary floor** — `pipelines:` block, same file.
- **Visa hard-drop language** — `visa_no_patterns`, same file.
- **Weights & thresholds** — `config/scoring.yaml` (`minimum_composite`,
  `strong_match`). Raise `minimum_composite` to 50–60 if the digest is noisy.
- **Scoring prompt** — `_PROMPT_HEADER` in `processing/scorer.py`.

## Cost

Apify ~$5/mo; Gemini, Adzuna, Reed, RemoteOK, Serper, Gmail, Google Sheets, and
GitHub Actions all on free tiers.

## Notes on the Apify scrapers

Actor input/output schemas drift over time. `scrapers/apify_scraper.py` builds a
tolerant common-denominator input and maps many possible output field names. If
a specific actor returns nothing, check the actor's current input schema on
Apify and adjust `_build_input` for that actor id — that's the one place
per-actor quirks belong.
```
