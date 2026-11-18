# OnTheRoadAI

A local content operations system for **On The Road Media** (sports content) and the
**GTA 6 Content Network**. It runs on your own computer with Python and SQLite. It has no
paid services and no platform logins, and it **never posts anything automatically**.

> Status: **Milestone 1**. This milestone covers idea management, transparent scoring, footage-rights
> tracking, orchestration, human approval and manually entered analytics. Most AI agents are still
> *planned*: the dashboard says so, and their steps go to a person (see [Agents](#the-nine-agents)).

## Quick start

You need Python 3.10 or newer.

```bash
git clone https://github.com/nickkoslow9183-beep/ontheroadai.git
cd ontheroadai
python3 -m venv .venv
source .venv/bin/activate          # Windows (cmd): .venv\Scripts\activate.bat
pip install -r requirements.txt
python -m ontheroadai run
```

Then open **http://127.0.0.1:5000** in your browser. The first run creates `data/ontheroadai.db`
and fills in the two businesses, their brands and their planned accounts. `data/` is git-ignored.

Options:

```bash
python -m ontheroadai run --port 8000            # different port
python -m ontheroadai --db path/to/other.db run  # different database file
python -m ontheroadai init-db                    # create/upgrade the database only
```

## Using the dashboard

1. **Submit an idea.** Open *On The Road Media* or *GTA 6 Content Network* and fill in
   *Submit an idea*. Set the six 0–10 sliders. The idea is scored right away.
2. **Read the score.** Each idea page shows every factor, its weight, the points it earned and
   why (see [Scoring](#opportunity-score-0100)).
3. **Record footage rights.** If an idea uses footage you didn't shoot (gameplay, streams,
   broadcast), tick *Uses footage we did not shoot*. Add the source under *Footage rights* and link
   it to the idea. A source can only be marked **granted** once you write down where the proof is.
4. **Start production.** On the idea page, click *Start production (Orchestrator)*. The
   Orchestrator creates one task per agent step. Steps for agents that aren't built yet appear as
   **manual required**. Do the work, type your name and click *Mark done*.
5. **Quality Control** runs automatically. It blocks the idea if footage rights aren't cleared or
   the hook, title or platforms are missing. Fix the problem, then click *Re-run Orchestrator*.
6. **Human review.** When every step is done, the idea is *ready for review*. A named reviewer
   approves it, requests changes or rejects it.
7. **Mark as published.** Post the content yourself on the platform. Then record the platform and
   URL and tick the confirmation box. The system refuses unless the idea is approved, rights are
   cleared and QC has passed. A database trigger also blocks publishing without approval.
8. **Analytics.** Copy numbers from each platform's insights screen into *Analytics*. Each business
   page then shows views, engagement rate and **follows per 1,000 views**.

## Opportunity score (0–100)

`score = Σ (input ÷ 10 × weight)`, with weights that add up to 100:

| Factor | Weight | Source |
|---|---:|---|
| Trend momentum | 20 | you (Trend Scout later) |
| Hook strength | 20 | you |
| Audience fit | 15 | you |
| Follower conversion potential | 15 | you (Analytics/Growth later) |
| Timeliness | 10 | you |
| Production ease | 10 | you (10 = quick to make) |
| Rights clarity | 10 | **automatic**: own footage or all sources granted = 10; requested = 5; unknown = 2; none linked, denied or expired = 0 |

The full breakdown is saved with each idea. The logic is in `ontheroadai/scoring.py`.

## The nine agents

| Agent | Status | What it does today |
|---|---|---|
| Orchestrator | **implemented** | Builds the pipeline, runs implemented agents, sends the other steps to a person, tracks status, creates post-publish tasks |
| Quality Control | **partial** | Rule-based checks: footage rights, hook, title, platforms, earlier steps. It does **not** inspect video yet |
| Trend Scout | planned | — (manual step) |
| Analytics Agent | planned | — (manual entry works; automated analysis does not) |
| Scriptwriter | planned | — |
| Clip Finder | planned | — |
| Video Editor | planned | — |
| Publishing Coordinator | planned | — (and will never auto-post) |
| Growth Optimizer | planned | — |

To add an agent, subclass `Agent` in `ontheroadai/agents/` and return it from
`implementation_for()` in `registry.py`. The Orchestrator will then run it automatically instead
of asking a person.

## Project layout

```
ontheroadai/
  db.py            SQLite schema (tables, checks, publish-approval trigger)
  seed.py          businesses, brands (GTA 6 Moments / Discoveries / Streamer Stories), accounts
  constants.py     platforms, content types, statuses
  scoring.py       transparent 0-100 score
  ideas.py         idea create/update/list
  rights.py        footage permissions
  approvals.py     human review decisions
  publishing.py    publish gate (records manual posts only)
  analytics.py     manual metrics + summaries
  agents/          base classes, registry, orchestrator, quality_control
  web/             Flask dashboard (templates + CSS)
tests/             pytest suite
```

Database tables: `businesses`, `brands`, `accounts`, `ideas`, `agent_tasks`,
`source_permissions`, `idea_sources`, `approvals`, `analytics`, `activity_log`.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Safety rules built in

- No automatic publishing, no platform APIs, no paid services.
- Publishing needs a named human approval, cleared footage rights and a passed QC check.
  The approval rule is enforced in both the code and the database.
- "Granted" footage permission needs recorded evidence. Expired permissions count as not cleared.
- The dashboard listens on `127.0.0.1` only by default. It is a single-user local tool with no
  login, so don't expose it to the internet.
- No credentials are stored. `.env`, keys and the database are git-ignored.
