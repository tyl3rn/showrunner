# automated-shorts

An end-to-end pipeline that turns Reddit stories into narrated vertical videos,
gated by an **LLM-as-judge** curation stage so only high-engagement stories are
ever produced. One command crawls a subreddit, scores every candidate, rewrites
weak endings, and renders finished 1080×1920 videos with burned-in captions and
platform-ready upload copy.

The interesting engineering isn't the video — it's the **evaluation and
multi-model orchestration** that decides *what* gets made and *how* it's written.

---

## Architecture

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                       curate.py                          │
                    │                                                          │
  Reddit  ─────────▶│  1. INGEST          2. JUDGE            3. SCRIPT-DOCTOR │
  (OAuth API or     │  fetch listing +    LLM-as-judge        rewrite ending   │
   RSS fallback)    │  top comments   →   scores every    →   as a twist,      │──┐
                    │  (rate-limited)     candidate 0–100     smooth prose,    │  │
                    │                     on 5 dimensions,    algospeak,       │  │
                    │                     gates on threshold  spoken title     │  │
                    │                                                          │  │
                    │                     4. UPLOAD COPY (TikTok/YT captions)  │  │
                    └──────────────────────────────────────────────────────────┘  │
                                                                                   │
                    ┌──────────────────────────────────────────────────────────┐  │
                    │                        main.py                           │  │
                    │              (renders each qualifying story)             │◀─┘
                    │                                                          │
   narrate.py  ────▶│   TTS voiceover + word-timed captions (edge-tts)         │
   post_card.py ───▶│   fake Reddit post card PNG (avatar, verified, awards)   │
   build_video.py ─▶│   ffmpeg: card overlay + whoosh + captions over footage  │
                    └──────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                             demo/<sub>-<stamp>-N.mp4  +  .upload.json
```

Each stage is a separate module with a clean CLI, so any step can be run or
debugged in isolation. `main.py` orchestrates the full run.

---

## Key design decisions

**LLM-as-judge curation.** Rather than posting the top-upvoted post, a Claude
call scores every candidate on structured dimensions — `hook`,
`emotional_intensity`, `narratability`, `audience_signal`, and an `overall`
0–100 — using Pydantic-validated structured outputs. Stories below a quality
threshold are rejected outright (the run skips rather than ship a dud). This is
the core of the project: an automated evaluation gate that reasons about *why*
content will or won't perform, using the post's own comment thread as a
corroborating audience signal.

**Multi-model cost routing.** High-volume judging and caption-writing run on
`claude-sonnet-5` (cheaper, near-Opus judgment). The single creative
writing call — the "script doctor" that rewrites flat endings into a twist — runs
on `claude-opus-4-8` with adaptive thinking, where the extra quality is worth the
pennies. Scoring 8 candidates costs a fraction of a cent; the one Opus call is
where the budget goes.

**Resilient ingestion.** Reddit blocks anonymous JSON scraping, so the fetcher
uses the official OAuth API when credentials are present and transparently falls
back to public RSS/Atom feeds otherwise — with per-request self-pacing and 429
backoff to respect rate limits. The rest of the pipeline is identical either way.

**Script doctoring as a narration transform.** Raw Reddit text isn't a
script. One Opus pass smooths broken English into natural spoken English, expands
abbreviations for speech (`AITAH` → "am I the asshole"), converts times/numbers
to spoken form (`1800` → "6pm"), applies platform-safe vocabulary, and — when the
judge flags a flat ending — replaces it with a twist that recontextualizes the
story. The card graphic keeps the *original* title for visual authenticity; the
narrator speaks the polished version.

**Batch efficiency.** The expensive part of a run is the rate-limited crawl. So
a single crawl produces *every* story that clears the bar (up to a cap), not just
the top one — amortizing one ~10-minute fetch into a full batch of videos.

---

## Pipeline stages

| Module | Responsibility |
|---|---|
| `reddit_fetch.py` | Fetch listings + comments (OAuth API or self-pacing RSS fallback) |
| `curate.py` | LLM-as-judge scoring, threshold gate, script doctoring, upload-copy generation |
| `narrate.py` | edge-tts voiceover + word-timed `.ass` captions (one word at a time, styled) |
| `post_card.py` | Render a fake Reddit post-card PNG (invented user/avatar, verified badge, award row) |
| `build_video.py` | ffmpeg compositing: background footage + card overlay + whoosh SFX + captions |
| `main.py` | Orchestrate the full crawl → judge → render batch |

---

## Usage

```bash
pip install -r requirements.txt
# needs: ffmpeg on PATH, ANTHROPIC_API_KEY set, a background video file

# one command: crawl a random subreddit, judge, render up to 3 videos
python main.py --background backgrounds/parkour.mp4 --out-dir demo

# force a specific subreddit / deeper time window
python main.py nosleep --time-filter week --background backgrounds/parkour.mp4 --out-dir demo
```

Each finished video lands beside a `.upload.json` containing platform-specific
captions and AI-disclosure flags. If no candidate clears the quality bar, the run
exits cleanly without producing a video.

Optional environment variables: `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`
(enables the fast OAuth path), `REDDIT_RSS_INTERVAL` (RSS pacing seconds).

---

## Stack

Python · Anthropic Claude API (structured outputs, adaptive thinking,
multi-model routing) · edge-tts · ffmpeg/libass · Pydantic · Reddit API + RSS

---

## Notes

Personal-scale project. Content ingestion (Reddit stories, background footage)
touches the usual gray areas of the short-form genre; anything commercial would
need licensed footage and content-rights review.
