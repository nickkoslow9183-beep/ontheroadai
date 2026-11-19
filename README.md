# OnTheRoadAI

A local content operations system for **On The Road Media** (sports content) and the
**GTA 6 Content Network**. It runs on your own computer with Python and SQLite. It has no
paid services and no platform logins, and it **never posts anything automatically**.

> **Status: Milestone 2.**
> - From Milestone 1: idea management, transparent scoring, footage-rights tracking,
>   orchestration and human approval.
> - New in Milestone 2: a working **Analytics Agent**. It handles CSV import with column
>   mapping, manual entry and corrections, retention and follower-conversion analysis, video
>   comparison and data-backed recommendations.
> - Six agents are still *planned*, and the dashboard says so (see [Agents](#the-nine-agents)).

## Start it (no typing needed)

1. Install **Python 3.10 or newer** for free from <https://www.python.org/downloads/>.
   On Windows, tick **"Add python.exe to PATH"** during setup.
2. Download this project: on GitHub, use **Code → Download ZIP**, then unzip it.
3. Double-click the start file:
   - **Windows:** `Start OnTheRoadAI.bat`
   - **Mac:** `Start OnTheRoadAI.command`. The first time, right-click it and choose **Open**,
     because macOS asks before running downloaded files.
4. A window opens and your browser opens the dashboard. The first run takes about a minute
   while it installs Flask. **Keep the window open** while you work, and close it to stop.

Everything else happens in the browser. Your data is saved in `data/ontheroadai.db` inside the
project folder. Back it up by copying that file.

<details><summary>Prefer a terminal?</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
python -m ontheroadai run            # http://127.0.0.1:5000
```
</details>

## Analytics (Milestone 2)

Open **Analytics** in the top menu, or the **Open … analytics** button on a business page.
Each business has its own page:

- **Import CSV.**
  1. Pick the account (for example *On The Road Media · TikTok*) and the date the numbers are
     from, then upload the platform's CSV export.
  2. **Match the columns.** Each column is pre-matched from common export names; check each one.
     Anything you leave as "not in this file" is stored as *not available*. It is never
     guessed and never treated as 0.
  3. **Preview** to see what will happen to every row without saving anything, then **Import**.
  4. Your column choices are remembered for files with the same columns.
  - **Rejected:** non-CSV files (such as Excel; save them as CSV first), empty files, files over
    5 MB, and the exact same file a second time.
  - **Skipped and listed with line numbers:** rows with unreadable values (such as `"lots"` in
    a views column), duplicate rows, and "Total" rows.
  - **History:** a newer export of the same videos adds a dated snapshot, so you can see the
    numbers grow. Importing the same date twice counts as a duplicate.
  - **Undo:** each import has an *Undo this import* button.
- **Enter by hand.** On the Analytics page, leave any box empty if the platform doesn't show
  that number.
- **Fix mistakes.** Open a video to correct or delete any snapshot, or to edit its title,
  length, format, topic, publish time or linked idea. Imports never overwrite your corrections.
- **Tag formats.** Exports don't say whether a video was a street interview or a fan reaction.
  Tick videos in the table and use *Set format*.
- **Video table.** Click any column header to sort. Use the filters for account, platform,
  format, length and date range.
- **Compare.** Tick 2 to 6 videos and click **Compare selected**. The page separates
  **what the numbers show** from **possible reasons (not proven)**, and lists caveats such as
  different platforms or numbers captured at different ages.
- **Recommendations.** Each one shows its evidence, how many videos it rests on, and a
  confidence label:
  - Under 5 videos: *Anecdotal*.
  - 5–14 videos: *Directional*.
  - 15–29 videos: *Moderate*.
  - 30 or more: *Larger sample (still not a controlled test)*.

  None of them claims statistical certainty.

### What is measured

| Metric | How it's calculated |
|---|---|
| Views, reach, likes, comments, shares, saves, followers gained, average watch time, completion rate, skip rate, video length, publish date/time | Straight from your export or entry. Missing values stay missing. |
| **Follows per 1,000 views** | followers gained ÷ views × 1,000. Group figures are *pooled*: total follows ÷ total views. |
| **Engagement rate** | (likes + comments + shares + saves) ÷ views. Marked `*` when some of these are missing from the export. |
| **Share watched** | average watch time ÷ video length. 7 s on a 15 s video = 47%; 7 s on a 60 s video = 12%. |
| **Retention vs similar-length videos** | share watched ÷ the median of videos in the same length band (≤15 s, 16–30 s, 31–60 s, 61–90 s, 91–180 s, over 3 min). It uses the same platform when there are at least 3 such videos. |
| **Loses viewers early** | retention ≤ 0.75× similar-length videos, **or** skip rate ≥ 10 points above them. |
| **Holds attention** | retention ≥ 1.25×, **or** skip rate ≥ 10 points below. |

Each video uses its **latest** snapshot. The Analytics Agent also works inside the
Orchestrator:
- When you mark an idea published, a video record is created from the post link.
- Its post-publish *collect performance* task waits until numbers arrive (by import or by
  hand), then writes a report automatically.
- It re-runs whenever newer numbers are added.

The **What the data says** box on each idea page shows how that format has performed before.
That information is for you; scores are not changed automatically.

> Example files for trying the importer are in `tests/fixtures/`. They're labelled **EXAMPLE**
> and are not real data. If you import them, use *Undo this import* afterwards.

## Ideas, scoring and publishing (Milestone 1)

1. **Submit an idea** on *On The Road Media* or *GTA 6 Content Network*. Set the six 0–10 sliders,
   and the idea is scored right away.
2. **Read the score.** Each idea page shows every factor, its weight, the points it earned and why.
3. **Record footage rights** under *Footage rights*. A source can only be marked **granted**
   once you write down where the proof is.
4. **Start production.** The Orchestrator creates one task per agent step. Steps for agents that
   aren't built yet appear as **manual required**: do the work, type your name and click *Mark done*.
5. **Quality Control** runs automatically and blocks the idea if rights, hook, title or platforms
   are missing.
6. **Human review.** A named reviewer approves, requests changes or rejects.
7. **Mark as published.** Post it yourself first, then record it here. This is refused without
   approval, cleared rights and a passed QC check. A database trigger also enforces approval.

### Opportunity score (0–100)

`score = Σ (input ÷ 10 × weight)`:

| Factor | Weight |
|---|---:|
| Trend momentum | 20 |
| Hook strength | 20 |
| Audience fit | 15 |
| Follower conversion potential | 15 |
| Timeliness | 10 |
| Production ease | 10 |
| Rights clarity | 10 |

Rights clarity is automatic:
- 10 points: own footage only, or every linked source granted.
- 5 points: permission requested.
- 2 points: rights unknown.
- 0 points: no source linked, or a source denied or expired.

## The nine agents

| Agent | Status | What it does today |
|---|---|---|
| Orchestrator | **implemented** | Builds the pipeline, runs implemented agents, sends the other steps to a person, tracks status, runs post-publish agents |
| Analytics Agent | **implemented** | CSV import and mapping, manual entry and corrections, retention, conversion, comparison, recommendations, post-publish performance reports |
| Quality Control | **partial** | Rule-based checks for rights, hook, title, platforms and earlier steps. It does **not** inspect video yet |
| Trend Scout | planned | — (manual step) |
| Scriptwriter | planned | — |
| Clip Finder | planned | — |
| Video Editor | planned | — |
| Publishing Coordinator | planned | — (and will never auto-post) |
| Growth Optimizer | planned | — (manual post-publish step) |

## Project layout

```
launch.py, Start OnTheRoadAI.bat/.command   double-click starters
ontheroadai/
  db.py              SQLite schema + automatic upgrade from Milestone 1
  analytics/
    fields.py        tracked metrics, column-name matching, value parsers
    store.py         videos and snapshots: create, correct, delete
    csv_import.py    upload -> map -> preview -> import -> undo
    insights.py      retention, conversion, comparison, recommendations
  agents/            orchestrator, quality_control, analytics_agent, registry
  ideas.py, scoring.py, rights.py, approvals.py, publishing.py
  web/               Flask dashboard (templates, CSS, a small sorting script)
tests/               pytest suite + example CSV files
```

Tables:
- **Milestone 1:** `businesses`, `brands`, `accounts`, `ideas`, `agent_tasks`,
  `source_permissions`, `idea_sources`, `approvals`, `analytics` (old manual entries, migrated
  automatically), `activity_log`.
- **Milestone 2:** `videos`, `video_snapshots`, `analytics_imports`, `mapping_presets`.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Safety rules built in

- No automatic publishing, no platform APIs, no paid services, and nothing fetched from the
  internet.
- Publishing needs a named human approval, cleared footage rights and a passed QC check.
- Missing metrics are never estimated. Every recommendation shows its evidence and sample size.
- The dashboard only listens on your own computer (`127.0.0.1`). It is a single-user tool with
  no login, so don't expose it to the internet.
- No credentials are stored. `.env`, keys and the database are git-ignored.
