# OnTheRoadAI

A local content operations system for **On The Road Media** (sports content) and the
**GTA 6 Content Network**. It runs on your own computer with Python and SQLite. It has no
paid services and no platform logins, and it **never posts anything automatically**.

> **Status: Milestone 4.**
> - From Milestone 1: idea management, transparent scoring, footage-rights tracking,
>   orchestration and human approval.
> - New in Milestone 2: a working **Analytics Agent**. It handles CSV import with column
>   mapping, manual entry and corrections, retention and follower-conversion analysis, video
>   comparison and data-backed recommendations.
> - New in Milestone 3: a working **Trend Scout**. It reads real public sports and gaming
>   feeds, then groups, verifies, scores and turns stories into content recommendations
>   (see [Trend Scout](#trend-scout-milestone-3)).
> - New in Milestone 4: a working **Video Studio**. Upload a podcast, transcribe it locally, find clip
>   moments, render real 1080x1920 MP4 drafts with captions, run video QC, then approve
>   (see [Video Studio](#video-studio-milestone-4)).
> - Three agents are still *planned*, and the dashboard says so (see [Agents](#the-nine-agents)).

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

## Video Studio (Milestone 4)

Open **Video Studio** in the top menu. The whole workflow runs in your browser, on your own computer.

1. **Upload recording.** Choose an MP4, MOV or M4V file (up to 20 GB) and say what it is (podcast,
   sports story, gameplay, or someone else's footage).
   - The file is sent in 8 MB pieces with a progress bar, so large files never have to fit in memory.
   - FFmpeg checks the real video and audio streams. The file extension alone isn't trusted.
   - Uploading the exact same file twice is refused.
2. **Processing jobs** run in the background, one at a time, so the dashboard stays usable.
   - The steps are inspect → transcribe → find clips.
   - Each shows Queued / Processing / Needs review / Failed / Completed, with progress and error details.
   - Jobs interrupted by closing the app restart automatically next time, up to 2 attempts.
3. **Transcript.** Transcription runs locally with **faster-whisper** (open source, CPU, int8).
   - You get word-level timestamps, and low-confidence words are underlined so you can check them.
   - Correct any line. If you change a line's words, its word timings are removed; they are never
     re-guessed.
   - Speakers are **not** detected automatically, because both hosts share one audio track. You can
     label lines "Left host" / "Right host" by hand.
   - If transcription can't run, import an `.srt`, `.vtt` or `.json` transcript instead. Those have
     line-level timing only.
4. **Clip candidates.** The Clip Finder scores moments 0–100 and explains every point. Factors:
   - opening hook, debate/opinion and energy;
   - story or game structure;
   - pacing and dead air;
   - whether the clip makes sense on its own, and whether it reaches its payoff;
   - length fit (from your analytics when there's enough data);
   - topical match with Trend Scout research.

   Rules it follows:
   - It never starts a clip mid-sentence ("and…", "but…") and never runs across a topic change
     ("next segment").
   - It ends on the end of a thought. It runs past the target length rather than cutting off the payoff.
   - It flags context problems, such as a clip that starts with "he/they" or refers to "like I said earlier".
5. **Clip editor.** Adjust start and end (or click "start here / end here" on transcript lines), and pick
   the layout:
   - **Focus left host / Focus right host / Center / Manual position** (a 9:16 crop);
   - **Center both** (the whole frame over a blurred background);
   - **Top/bottom split** (left host on top, right host below).

   **Framing changes** switch the layout at chosen moments, for example left host for the question,
   right host for the answer. A box over the video previews the crop. Framing is **manual**: there is
   no automatic speaker tracking. Also in the editor:
   - **Captions:** font (from the fonts installed on your computer), size, colours, outline, position
     and a word highlight. Captions appear exactly when each word is spoken (from real word timings),
     never before.
   - **Text overlays:** for example a 4-level draft "board" that keeps all four levels on screen and
     reveals each pick only at the moment it's actually said. If a phrase isn't found in the
     transcript, QC reports it instead of guessing.
   - **Tighten pauses:** cuts detected silences longer than 0.7 s. Speech is never cut.
   - **Presets:** Clean podcast Reel, Sports storytelling Reel (puts the hook on screen for 3 s),
     GTA gameplay clip.
6. **Video drafts.** Rendering produces a real **MP4 (H.264 + AAC), 1080x1920, 9:16**, loudness-normalised,
   with fast start. Preview it in the browser and **Download MP4**.
7. **Quality control** runs automatically on every draft:
   - the file opens, every frame decodes, and there's an audio track;
   - 1080x1920 at 9:16, and the expected duration;
   - black frames, long silences, and clipping or unusual loudness;
   - missing captions and caption timing (too early, too short, too long, overlapping);
   - web-ready fast start.

   Each problem comes with a suggested fix. Passing QC doesn't mean a video is good. That's your call.
8. **Approval queue.**
   - Approving needs your name. QC failures block approval, and QC warnings need a tick to confirm
     you've reviewed them.
   - Someone else's footage can't be rendered at all until written permission is recorded in
     Footage rights.
   - An approved draft can be sent into the idea pipeline. Clip Finder, Video Editor and Quality
     Control then complete their steps from the real draft, publishing still needs the normal human
     approval, and **you post it yourself**.

**GTA 6 footage** uses the same engine with the "GTA gameplay clip" preset. Clip finding uses speech
only: visual moments without talking (chases, crashes) are **not** detected, so mark those by hand.
Streamer footage is never treated as cleared.

### Where files are stored

Everything stays on your computer in `data/media/` inside the project folder (set `ONTHEROADAI_MEDIA`
to use another folder, such as an external drive):

```
data/media/uploads/      partial uploads (deleted when finished or after 24 h)
data/media/recordings/   the original files
data/media/work/         temporary audio (deleted after transcription) + detected pauses
data/media/drafts/       rendered MP4 drafts and their caption files
```

This folder is **git-ignored**. Videos, audio, transcripts and drafts are never committed to GitHub.
**Delete recording** removes all of its files. In a temporary cloud workspace (like the one this
was built in), everything in this folder is gone when the workspace is deleted. Keep your real
recordings and drafts on your own PC or a backup drive.

### Installing FFmpeg

The Video Studio needs **FFmpeg** (free). Everything else works without it.
- **Windows:** `Start OnTheRoadAI.bat` detects that FFmpeg is missing and offers to install it with
  Windows Package Manager (winget). You can also download a build from https://www.gyan.dev/ffmpeg/builds/,
  unzip it into `tools/ffmpeg/` inside the project, and the app finds `tools/ffmpeg/bin`.
- **Mac:** `brew install ffmpeg`.

The Video Studio page shows whether FFmpeg, libx264, libass and faster-whisper were found.

### Performance: what's known and what isn't

Measured in the cloud workspace used to build this (4 virtual CPUs, Linux):
- `base.en` transcribed 10.7 minutes of audio in about 30 seconds.
- A 30-second 1080x1920 draft from minute 8.7 rendered and passed QC in about 27 seconds ("balanced").

**This doesn't predict speed on your Windows PC** (Ryzen 5 5600G, 12 GB RAM); it hasn't been run there.
- A rough expectation is that `base.en` handles an hour of podcast audio in a few minutes, and
  `small.en` (more accurate) takes roughly 3x longer. Try `base.en` first.
- Rendering only decodes the part of the recording it needs, but 4K sources render much slower than 1080p.
- Transcribing a 1-hour recording holds about 250 MB of audio in memory. The video itself is never
  loaded into memory.
- The Whisper model downloads once (about 140 MB for `base.en`) the first time you transcribe.

To try the workflow without a real recording, make a **synthetic** test video (computer voices, two
coloured panels labelled LEFT HOST / RIGHT HOST). This needs espeak-ng:
`python scripts/make_sample_podcast.py sample.mp4`

## Trend Scout (Milestone 3)

Open **Trend Scout** in the top menu.

1. **Run Trend Scout** fetches the sources that are due. **Run now (all sources)** fetches every
   enabled source immediately. A run takes about 30–60 seconds, and a report opens when it
   finishes.
2. **Sports opportunities** and **GTA 6 opportunities** list the stories found, highest score first.
   Filter by sport, account, topic, freshness, verification, source reliability, minimum score and
   production status.
3. Open an opportunity to see:
   - the summary, why it matters, every source link and the verification status;
   - three hook options, an interview question, and the suggested format and platform;
   - effort, urgency and B-roll ideas;
   - for GTA, the recommended account, an original-content plan, footage permission and
     duplication risk;
   - the full score breakdown.
4. **Create idea** turns an opportunity into a draft idea with one click. Clicking again never
   creates a duplicate, and an existing idea with the same headline is linked instead.
5. **Add your own research.** Paste a link, a headline, several lines (`headline | link`), or
   upload a `.txt` / `.md` / `.csv` research file.
6. **Research sources** shows each source's real status (working, error with the reason,
   needs credentials, disabled, never fetched). There you can test, enable or disable sources,
   change URLs and add feeds.
7. **Creator watchlists.** Add creators and streamers.
   - YouTube channels (by channel ID) are read through YouTube's public channel feed.
   - Twitch, TikTok, Instagram and X profiles are saved links you check by hand.
   - Patterns come only from posts the feeds actually returned.
8. **Research history** lists every run, and **Trending topics** shows the most-mentioned names
   and words.

**Automatic research.** Choose "Every N h" on the Trend Scout page. Runs happen in the background
**only while the app is open on your computer**. Nothing runs when the computer is off or asleep or
the app is closed, and having the code on GitHub doesn't run anything. From a terminal you can
also run one pass with `python -m ontheroadai research --all`.

### Sources (checked live on 2026-11-19)

| Source | Result |
|---|---|
| ESPN (top, NBA, NFL, MLB, NHL, MMA) RSS | working |
| CBS Sports (NBA, NFL, MLB, NHL, MMA) RSS | working |
| MMA Fighting RSS | working (`/rss/index.xml`; the old `/rss/current` URL is gone) |
| GameSpot, PC Gamer, Polygon, Kotaku, IGN RSS | working (non-GTA stories are filtered out) |
| Reddit r/GTA6 RSS | **off by default**: Reddit's robots.txt disallows automated reading, so the fetcher refuses it |
| Rockstar Newswire | unavailable: its feed URL returned HTTP 500. Add Rockstar's YouTube channel ID in Watchlists instead |
| X, TikTok, Instagram, Twitch, YouTube Data API | need paid plans, app review or credentials. Listed on the Sources page; not used |

**How fetching stays polite:**
- robots.txt is checked before every fetch.
- Each source has a minimum interval, and repeated failures back off.
- There's a 2-second gap between requests to the same host.
- Feeds are re-read with conditional requests (ETag / Last-Modified).
- Logins, cookies and paywalls are never used.

Only headlines, summaries and links are stored. Nothing is downloaded or re-uploaded, and a feed
being public never makes its footage reusable.

### How opportunities are built

- **Group:** related headlines become one story. Grouping needs a shared person's name with
  keyword overlap, or several shared names; a team name alone isn't enough. A later run that
  adds items to a story marks it **updated**, and new stories are marked **new**.
- **Classify:** sport, topic type (debate, rivalry, trade, unexpected performance, controversy,
  press conference, game day, athlete story, injury, roundup) and GTA concept (moment, discovery,
  streamer story, news) come from readable keyword rules in `trends/classify.py`.
- **Sensitive news** (arrests, deaths, legal cases) gets no hooks or debate angles and isn't
  converted automatically.
- **Verification labels:**
  - Confirmed (official source)
  - 2+ independent reliable outlets
  - Single reliable report
  - Credible report, not officially confirmed
  - Reported, not confirmed (uses "reportedly" / sources)
  - Community or creator post: unverified
  - Rumor / speculation
  - Added by you
- **Quality Control** blocks an idea built from an unverified story until someone either confirms
  it or acknowledges that the video presents it as unconfirmed.
- **GTA footage** from a streamer is never marked cleared. Converting a streamer story attaches a
  footage-rights record with status *unknown*, so QC blocks it until written permission is recorded.
- **Hooks** are drafted from the headlines with fixed templates (no AI model and no paid API). They
  only use names that appear in the source headlines. Edit them before filming.

### Opportunity score (0–100)

| Factor | Weight | | Factor | Weight |
|---|---:|---|---|---:|
| Topic freshness | 14 | | Source reliability | 10 |
| Audience relevance | 12 | | Production feasibility | 8 |
| Debate potential | 10 | | Share potential | 8 |
| Hook potential | 10 | | Originality | 7 |
| Story strength | 9 | | Your format's past performance* | 6 |
| | | | Your format's follower conversion* | 6 |

\* These two use your Milestone 2 analytics when you have at least 3 videos of the recommended
format. Until then they stay at a neutral 5/10, and the score is labelled **heuristic**. With
enough data it reads **heuristic + your analytics**, and those factors show your real numbers.
The score ranks opportunities for review. It does not predict views and does not model any
platform's algorithm.

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
| Clip Finder | **implemented** | Local Whisper transcription (or SRT/VTT import), explained 0–100 clip scoring, payoff protection, context flags, Trend Scout + analytics inputs |
| Video Editor | **implemented** | FFmpeg 1080x1920 H.264/AAC drafts, manual framing with timed changes, word-timed captions, overlays, loudness normalisation, pause tightening |
| Trend Scout | **implemented** | Live feed research, grouping, verification labels, transparent scoring, recommendations, idea conversion, "trend context" pipeline step |
| Analytics Agent | **implemented** | CSV import and mapping, manual entry and corrections, retention, conversion, comparison, recommendations, post-publish performance reports |
| Quality Control | **implemented** | Technical video checks on every draft (decode, audio, size/aspect, duration, black frames, silence, loudness, captions, fast start), plus rights, unverified-claim, hook, title, platform and step checks. It never judges whether a video is engaging |
| Scriptwriter | planned | — |
| Publishing Coordinator | planned | — (and will never auto-post) |
| Growth Optimizer | planned | — (manual post-publish step) |

## Project layout

```
launch.py, Start OnTheRoadAI.bat/.command   double-click starters (also check for FFmpeg)
scripts/make_sample_podcast.py              synthetic test podcast generator
ontheroadai/
  db.py              SQLite schema + automatic upgrade from Milestone 1
  analytics/
    fields.py        tracked metrics, column-name matching, value parsers
    store.py         videos and snapshots: create, correct, delete
    csv_import.py    upload -> map -> preview -> import -> undo
    insights.py      retention, conversion, comparison, recommendations
  studio/
    media.py         ffprobe/FFmpeg: validation, audio, silence/black/loudness detection
    transcript.py    faster-whisper transcription, SRT/VTT/JSON import, safe editing
    clipfinder.py    moment scoring and selection
    framing.py       crop math, layouts, framing changes, pause tightening
    captions.py      word-timed captions and overlays (ASS subtitles)
    render.py        FFmpeg rendering with progress
    videoqc.py       technical checks on drafts
    jobs.py          background job queue, worker, recovery
    pipeline.py      what each job does
    service.py       uploads, library, clips, drafts, approval
    storage.py       where media files live
  trends/
    sources.py       default feeds, user feeds, platforms that need credentials
    fetcher.py       polite HTTP: robots.txt, rate limits, conditional GET
    parser.py        RSS/Atom parsing, URL/date normalisation
    text.py          tokens, names, story matching
    classify.py      sport/topic/GTA concept, verification labels
    recommend.py     hooks, formats, B-roll, GTA plans, hand-offs for future agents
    scoring.py       0-100 opportunity score (uses your analytics when available)
    job.py           the research run
    opportunities.py filters, saving, idea conversion, trending topics
    watchlist.py     creator watchlists
    scheduler.py     automatic runs while the app is open
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
- **Milestone 4:** `studio_uploads`, `studio_recordings`, `studio_transcripts`, `studio_clips`,
  `studio_drafts`, `studio_jobs`.
- **Milestone 3:** `research_sources`, `research_runs`, `research_run_sources`, `research_items`,
  `research_stories`, `creators`, `app_settings`.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Safety rules built in

- No automatic publishing, no paid services, and no logins to any platform.
- Video, audio and transcripts stay on your computer (`data/media/`, git-ignored). Transcription runs
  locally, so nothing is sent to a transcription service.
- The only internet access is Trend Scout reading public RSS/Atom feeds, politely, as described above.
- Publishing needs a named human approval, cleared footage rights and a passed QC check.
- Missing metrics are never estimated. Every recommendation shows its evidence and sample size.
- The dashboard only listens on your own computer (`127.0.0.1`). It is a single-user tool with
  no login, so don't expose it to the internet.
- No credentials are stored. `.env`, keys and the database are git-ignored.
