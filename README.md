# automated-shorts

Turns Reddit stories into narrated vertical videos (the TikTok "reddit story
over gameplay footage" format), with an LLM deciding which stories are
actually worth making. One command crawls a subreddit, scores every candidate
post, rewrites weak endings, and renders finished 1080x1920 videos with
burned-in captions and ready-to-paste upload text.

The video rendering is the boring part. The point of the project is the
curation layer: most posts on any given day are mediocre, and a bot that
posts mediocre content is worthless. So everything runs through a judge that
scores candidates and refuses to produce anything below a quality bar.

## How it works

```
                    +----------------------------------------------------------+
                    |                       curate.py                          |
                    |                                                          |
  Reddit  --------->|  1. INGEST          2. JUDGE            3. SCRIPT DOCTOR |
  (OAuth API or     |  fetch listing +    scores every        rewrites flat    |---+
   RSS fallback)    |  top comments       candidate 0-100,    endings, fixes   |   |
                    |  (rate limited)     gates on threshold  grammar, swaps   |   |
                    |                                         banned words     |   |
                    |                                                          |   |
                    |                     4. writes TikTok/YouTube captions    |   |
                    +----------------------------------------------------------+   |
                                                                                    |
                    +----------------------------------------------------------+   |
                    |                        main.py                           |   |
                    |               renders each story that passed             |<--+
                    |                                                          |
   narrate.py  ---->|   TTS voiceover, word-timed captions (edge-tts)          |
   post_card.py --->|   fake Reddit post card PNG for the intro                |
   build_video.py ->|   ffmpeg: card overlay, whoosh, captions over footage    |
                    +----------------------------------------------------------+
                                              |
                                              v
                             demo/<sub>-<stamp>-N.mp4  +  .upload.json
```

Every stage is its own module with a CLI, so each step can be run and
debugged alone. `main.py` chains them.

## Scoring

Each candidate gets a structured scorecard from Claude: an overall 0-100
score, sub-scores for hook, emotional intensity, narratability, and audience
signal (how strongly the post's real comment thread reacted), a category, a
"needs ending fix" flag, and a one-line reason.

The overall score is a judgment call by the model, not a weighted average of
the sub-scores. The rubric puts hard rules on it: a story that opens slow
gets capped below 50 no matter how good the rest is, because viewers decide
in the first few seconds. A weak ending does not lower the score, since the
script doctor can fix endings, it just sets the flag. Unsafe content gets
zeroed. If nothing clears the bar (default 65), the run skips instead of
producing a dud. Scores drift a few points between runs since it's a
judgment, and the threshold accounts for that.

There's also a feedback loop. The web UI lets you rate finished videos 1-5
with a note. Ratings go to `ratings.json`, and every later run builds a
taste profile from them (per-subreddit track record, plus examples of what
got rated up or down and why) that gets prepended to the judge's prompt and
the script doctor's prompt. Not fine-tuning, just a memory file, but it
means the pipeline gets more aligned with what you actually want the more
you rate.

## Model choices

Scoring and caption writing run on Sonnet, which is cheap enough to score a
whole listing for a fraction of a cent and good enough for judgment work.
The script doctor runs on Opus with thinking enabled, because rewriting an
ending so the twist actually lands is the one real writing task in the
pipeline and it only runs once per posted story. A day's batch costs a few
cents total.

The script doctor does more than endings: it smooths broken English into
something a narrator can read, expands abbreviations (AITAH becomes "am I
the asshole", 1800 becomes "6pm"), and swaps words that trip platform
moderation for the substitutes the genre uses. The intro card keeps the
original title for authenticity, the narrator reads the polished one.

## Reddit access

Reddit blocks anonymous JSON scraping, and API app creation is gated behind
an approval process now. So the fetcher uses OAuth when credentials exist
and otherwise falls back to public RSS feeds with self-pacing (about one
request per minute before Reddit starts throwing 429s). Slow, but fine for a
scheduled bot, and the crawl cost gets amortized: one crawl produces every
story that clears the bar, up to a cap, not just the best one.

## Modules

| Module | Does |
|---|---|
| `reddit_fetch.py` | listings + comments, OAuth or paced RSS fallback |
| `curate.py` | judging, threshold gate, script doctor, upload copy |
| `narrate.py` | TTS + word-timed .ass captions, one word at a time |
| `post_card.py` | fake Reddit post card PNG (invented user, awards, verified badge) |
| `build_video.py` | ffmpeg compositing, card overlay + whoosh + captions |
| `main.py` | runs the whole thing |
| `web/server.py` | local FastAPI console: generate, watch progress, rate videos |
| `feedback.py` | ratings store + taste profile for the judge and doctor |

## Running it

```bash
pip install -r requirements.txt
# needs ffmpeg on PATH, ANTHROPIC_API_KEY set, and a background video file

# crawl a random subreddit from the pool, render up to 3 videos
python main.py --background backgrounds/parkour.mp4 --out-dir demo

# or pick the subreddit and go deeper in time
python main.py nosleep --time-filter week --background backgrounds/parkour.mp4 --out-dir demo

# or use the web console
python -m uvicorn web.server:app --port 8000
```

Each video lands next to a `.upload.json` with platform captions and AI
disclosure flags, and a `.meta.json` with the full scorecard. If nothing
clears the bar the run exits clean.

Optional: `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` for the fast OAuth
path, `REDDIT_RSS_INTERVAL` to tune RSS pacing.

## Stack

Python, Anthropic API (structured outputs, thinking, two models routed by
task), edge-tts, ffmpeg/libass, Pydantic, FastAPI, Reddit API + RSS.

## Notes

Personal-scale project. Reddit stories and gameplay footage sit in the usual
gray areas of this genre; anything commercial would need licensed footage
and a content-rights pass.
