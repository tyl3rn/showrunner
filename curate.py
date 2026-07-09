"""
curate.py

The story-picking agent. Instead of grabbing the first post that fits a length
window (old reddit_fetch.py behavior), this:

  1. Pulls a batch of candidate posts from the subreddit
  2. Fetches the top comments for each candidate (audience reaction signal)
  3. Asks Claude to score every candidate on short-form-video virality --
     the model judges the STORY ITSELF first (hook, emotional intensity,
     narratability) and uses the comments as corroborating evidence of how
     a real audience reacted
  4. Saves the winner as narration-ready text (same story.txt / .meta.json
     contract as reddit_fetch.py, so narrate.py and build_video.py are
     untouched)

Needs ANTHROPIC_API_KEY in the environment. One Claude call per run (all
candidates scored in a single request), so cost is a fraction of a cent and
it runs fine from a Raspberry Pi.

    python3 curate.py tifu --listing top --time-filter day --out story.txt
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode emoji in story
# titles/captions -- a bare print() would crash the whole run.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import anthropic
from pydantic import BaseModel, Field

from reddit_fetch import clean_text, fetch_comments, fetch_listing, using_oauth

# In RSS mode every comment fetch costs ~1 minute of rate-limit pacing, so we
# only deep-judge the top slice of the (already popularity-ranked) listing.
RSS_CANDIDATE_CAP = 8

# Rotation pool for `subreddit = auto`. Repeats = weighting. The vibe:
# unhinged confessions, moral-outrage AITA chaos, creepy horror, and
# "professionals reveal the case that broke them" question threads.
SUBREDDIT_POOL = [
    "TrueOffMyChest", "TrueOffMyChest",
    "confession", "confessions",
    "AmItheAsshole", "AITAH",
    "tifu",
    "nosleep", "LetsNotMeet",
    "AskReddit", "AskReddit",
]

# Question subs: the post has no body -- the STORY is the top comment
# (e.g. "doctors, what deathbed confession stuck with you?"). We promote the
# best-fitting top comment to be the narration body.
COMMENT_DRIVEN_SUBS = {"askreddit"}

# Sonnet 5: near-Opus judgment quality at a fraction of the cost -- right
# tradeoff for scoring ~25 stories twice a day.
MODEL = "claude-sonnet-5"

# How many comments per candidate to show the judge. Top 6 is plenty of
# signal without bloating the prompt.
COMMENTS_PER_POST = 6

SYSTEM_PROMPT = """\
You are the story curator for a short-form video channel (YouTube Shorts /
TikTok) that narrates Reddit stories over gameplay footage. The audience is
doomscrolling with a ~2-second patience fuse. Your job is to find the story
that STOPS THE SCROLL -- the freaky, unhinged, out-of-pocket, "no way this
happened", "I have to hear where this goes" story. Mild, wholesome, or
merely-relatable content is worthless here no matter how well written.

THE FIRST 5-10 SECONDS RULE (overriding): a viewer only ever hears the title
and the first sentence or two before deciding to swipe. If the title + first
two sentences would not make a stranger physically stop scrolling, the
overall score MUST be below 50 regardless of how good the rest is. A wild
ending cannot save a slow open.

THE LANDING RULE: a banger needs BOTH the hook AND the payoff -- but a weak
ending is FIXABLE in post-production (a script doctor punches up flat
endings before narration). So: score primarily on hook + premise + raw
material quality. If the opening is scroll-stopping but the ending deflates,
DO NOT tank the overall score -- keep it high and set needs_ending_fix=true.
Only score low when the premise itself is mundane; no ending rewrite can
save a boring setup.

How to judge -- in this priority order:

1. YOUR OWN READ OF THE STORY (primary). Judge the text itself:
   - Hook (weight this heaviest): is the title itself a jaw-dropper? Do the
     opening lines promise chaos immediately?
   - Shock/freak factor: is it genuinely unhinged, disturbing, absurd,
     scandalous, enraging, or so weird it demands to be shared? The best
     picks make someone say "what the actual f***" out loud. Funny works
     too, but it must be laugh-out-loud absurd, not chuckle-mild.
   - Arc & payoff: escalation, then a twist, punchline, or gut-punch.
   - Narratability: works READ ALOUD in 60-120 seconds, no images/links.

2. AUDIENCE REACTION (secondary, corroborating evidence). Top comments show
   how real readers reacted. Strong: shock, disbelief, "I gasped", hot
   debate, tagging friends, demanding updates. Weak: polite sympathy,
   advice-only replies, indifference. Confirm or temper your own read with
   this -- never substitute it for judging the story.

Score down hard: stories that are probably fake in a BORING way (obvious
creative-writing homework scores low; wild-but-plausible is fine -- and a
story so entertaining viewers won't care is acceptable), anything sexually
explicit or involving minors in unsafe contexts (score 0 -- unusable for
monetized platforms), and stories needing visuals to land.
"""


class CandidateScore(BaseModel):
    index: int = Field(description="Candidate number, matching the prompt")
    overall: int = Field(description="0-100 overall virality score")
    category: str = Field(description="funny | scary | infuriating | weird | heartwarming | jaw-dropping | other")
    hook: int = Field(description="0-100: does the title + opening grab within 3 seconds")
    emotional_intensity: int = Field(description="0-100: strength of the funny/scary/weird/outrage payload")
    narratability: int = Field(description="0-100: works read aloud in 60-120s, no visuals/links needed")
    audience_signal: int = Field(description="0-100: how strongly the top comments corroborate a big reaction")
    needs_ending_fix: bool = Field(description="true when the hook/premise is strong but the ending is flat and should be punched up before narration")
    reason: str = Field(description="1-2 sentences justifying the score")


class Scorecard(BaseModel):
    scores: list[CandidateScore]
    best_index: int = Field(description="index of the single best candidate")
    verdict: str = Field(description="one sentence on why the winner wins")


class UploadCopy(BaseModel):
    tiktok_caption: str = Field(description=(
        "TikTok caption: first line is a curiosity-gap reaction to the story "
        "(NEVER a summary, NEVER spoil the ending), then 4-6 hashtags mixing "
        "niche (#redditstories, subreddit-specific) and broad (#storytime, "
        "#fyp), then a short comment-bait question."
    ))
    youtube_title: str = Field(description=(
        "YouTube Shorts title: the reddit post title, lightly punched up, "
        "optionally one emoji, ending with '(Reddit Stories)'. Max ~90 chars."
    ))
    youtube_description: str = Field(description=(
        "1-2 line YouTube description ending with hashtags including #shorts "
        "#reddit #redditstories"
    ))


def gather_candidates(subreddit, listing, time_filter, limit, min_len, max_len, seen_ids,
                      allow_empty_body=False):
    posts = fetch_listing(subreddit, listing, time_filter, limit)
    candidates = []
    for post in posts:
        if post.get("stickied") or post.get("id") in seen_ids:
            continue
        body = post.get("selftext", "")
        if body in ("[removed]", "[deleted]"):
            continue
        if not allow_empty_body:
            if not body or not (min_len <= len(body) <= max_len):
                continue
        candidates.append({
            "id": post["id"],
            "title": post["title"],
            "body": clean_text(body),
            "subreddit": post["subreddit"],
            "permalink": f"https://reddit.com{post['permalink']}",
            "score": post.get("score", 0),
            "upvote_ratio": post.get("upvote_ratio", 0.0),
            "num_comments": post.get("num_comments", 0),
        })
    return candidates


def attach_comments(candidates):
    for i, cand in enumerate(candidates):
        try:
            cand["comments"] = fetch_comments(cand["permalink"], limit=COMMENTS_PER_POST)
        except Exception as e:
            print(f"  (comments failed for candidate {i}: {e})", file=sys.stderr)
            cand["comments"] = []
        # Be polite to reddit's public endpoints -- one request per second.
        if i < len(candidates) - 1:
            time.sleep(1.0)


def promote_comments(candidates, min_len, max_len):
    """For question-thread subs: the top comment IS the story. Promote the
    first (= highest-ranked) comment that fits the narration length window
    into the candidate's body; remaining comments stay as audience signal.
    Candidates with no fitting comment are dropped."""
    kept = []
    for cand in candidates:
        for k, cm in enumerate(cand["comments"]):
            if min_len <= len(cm["body"]) <= max_len:
                cand["body"] = clean_text(cm["body"])
                cand["comments"] = cand["comments"][:k] + cand["comments"][k + 1:]
                cand["from_comment"] = True
                kept.append(cand)
                break
    return kept


def build_prompt(candidates):
    blocks = []
    for i, c in enumerate(candidates):
        comments = "\n".join(
            (f'  - ({cm["score"]} points) ' if cm["score"] is not None else "  - ")
            + cm["body"][:400]
            for cm in c["comments"]
        ) or "  (no comments available)"
        if c["score"] is not None:
            meta = (f"Post score: {c['score']} | Upvote ratio: {c['upvote_ratio']} | "
                    f"Comment count: {c['num_comments']}")
        else:
            meta = (f"Vote metadata unavailable; candidates are listed in reddit's "
                    f"top-of-day order, so candidate {i} ranked #{i + 1} today")
        blocks.append(
            f"### Candidate {i}\n"
            f"Subreddit: r/{c['subreddit']} | {meta}\n"
            f"Title: {c['title']}\n"
            f"Story:\n{c['body']}\n"
            f"Top comments:\n{comments}\n"
        )
    return (
        "Score every candidate below for short-form video virality, then pick "
        "the single best one.\n\n" + "\n".join(blocks)
    )


def score_candidates(candidates) -> Scorecard:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(candidates)}],
        output_format=Scorecard,
    )
    return response.parsed_output


class EndingRewrite(BaseModel):
    rewrote: bool = Field(description="true if the ending was changed")
    story: str = Field(description="the full story body, with the punched-up ending if rewritten, otherwise unchanged")


def punch_up_ending(winner: dict, flagged: bool) -> str:
    """Script doctor: if the ending is flat, replace the final stretch with
    something that lands -- a twist, a reveal, a WTF beat. The hook and body
    stay; only the landing changes."""
    client = anthropic.Anthropic()
    hint = (
        "The curator flagged this story's ending as flat -- rewrite it."
        if flagged else
        "The curator thinks the ending is fine -- only rewrite if you strongly disagree."
    )
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=(
            "You are the script doctor for a shorts channel narrating Reddit "
            "stories. Your one job: make sure the ending LANDS. If the ending "
            "is flat, replace the final portion with a shock beat, twist, "
            "reveal, or 'WTF did I just watch' turn that feels like a natural "
            "escalation of everything before it. Rules: keep the first-person "
            "reddit voice and tone; keep everything before the ending intact "
            "except tiny connective edits; stay within +-15% of the original "
            "length; keep it plausible enough to not read as fiction; nothing "
            "sexually explicit, nothing unsafe involving minors. If the "
            "ending already slaps, return the story unchanged."
        ),
        messages=[{
            "role": "user",
            "content": f"{hint}\n\nTitle: {winner['title']}\n\nStory:\n{winner['body']}",
        }],
        output_format=EndingRewrite,
    )
    result = response.parsed_output
    if result.rewrote:
        print("Ending doctor: rewrote the landing.")
        return result.story.strip()
    print("Ending doctor: original ending kept.")
    return winner["body"]


def generate_upload_copy(winner: dict) -> UploadCopy:
    """One extra cheap Claude call: platform-ready caption text for the
    winning story, saved alongside it so the uploaders can post as-is."""
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=(
            "You write upload copy for a shorts channel that narrates wild "
            "Reddit stories over gameplay footage. Your captions create "
            "curiosity gaps that make people watch, never summaries that let "
            "them skip. Match the story's energy (funny/creepy/outrageous)."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Write the upload copy for this story from r/{winner['subreddit']}.\n\n"
                f"Title: {winner['title']}\n\nStory:\n{winner['body']}"
            ),
        }],
        output_format=UploadCopy,
    )
    return response.parsed_output


def main():
    ap = argparse.ArgumentParser(description="Pick the most viral-worthy Reddit story via Claude scoring.")
    ap.add_argument("subreddit", nargs="?", default="auto",
                    help="a subreddit name, or 'auto' to rotate through the built-in pool")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "controversial"])
    ap.add_argument("--time-filter", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=25, help="how many posts to pull as candidates")
    ap.add_argument("--min-len", type=int, default=400)
    ap.add_argument("--max-len", type=int, default=2200)
    ap.add_argument("--min-score", type=int, default=65,
                    help="if the best candidate scores below this, exit 2 (skip this run rather than post a dud)")
    ap.add_argument("--seen-file", default="seen_story_ids.json")
    ap.add_argument("--out", default="story.txt")
    args = ap.parse_args()

    # Fail fast: the Claude call happens AFTER ~9 minutes of rate-limited
    # reddit crawling -- don't start that crawl if we can't score at the end.
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set in this terminal.\n"
            "Fix: open a NEW terminal (the key is saved user-level), or run:\n"
            '  $env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY","User")',
            file=sys.stderr,
        )
        sys.exit(1)

    seen_path = Path(args.seen_file)
    seen_ids = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()

    subreddit = random.choice(SUBREDDIT_POOL) if args.subreddit == "auto" else args.subreddit
    comment_driven = subreddit.lower() in COMMENT_DRIVEN_SUBS

    print(f"Fetching candidates from r/{subreddit} ({args.listing}/{args.time_filter})"
          + (" [question thread: story = top comment]" if comment_driven else "") + "...")
    candidates = gather_candidates(
        subreddit, args.listing, args.time_filter,
        args.limit, args.min_len, args.max_len, seen_ids,
        allow_empty_body=comment_driven,
    )
    if not candidates:
        print("No usable candidates (all seen, removed, or wrong length).", file=sys.stderr)
        sys.exit(1)

    if not using_oauth() and len(candidates) > RSS_CANDIDATE_CAP:
        candidates = candidates[:RSS_CANDIDATE_CAP]
        print(f"RSS mode (no reddit credentials): judging top {RSS_CANDIDATE_CAP} "
              f"candidates, ~1 min per comment fetch -- this run takes a while.")

    print(f"Fetching top comments for {len(candidates)} candidates...")
    attach_comments(candidates)

    if comment_driven:
        candidates = promote_comments(candidates, args.min_len, args.max_len)
        if not candidates:
            print("No question thread had a comment fitting the narration length.", file=sys.stderr)
            sys.exit(1)

    print("Scoring with Claude...")
    card = score_candidates(candidates)

    for s in sorted(card.scores, key=lambda s: -s.overall):
        marker = " <== WINNER" if s.index == card.best_index else ""
        print(f"  [{s.overall:3d}] #{s.index} ({s.category}) hook={s.hook} "
              f"emotion={s.emotional_intensity} narrate={s.narratability} "
              f"audience={s.audience_signal} -- {s.reason}{marker}")
    print(f"Verdict: {card.verdict}")

    best_score = next(s for s in card.scores if s.index == card.best_index)
    if best_score.overall < args.min_score:
        print(f"Best candidate only scored {best_score.overall} (< {args.min_score}). "
              f"Skipping this run.", file=sys.stderr)
        sys.exit(2)

    winner = candidates[card.best_index]
    seen_ids.add(winner["id"])
    seen_path.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")

    print("Running the ending doctor...")
    doctored = punch_up_ending(winner, best_score.needs_ending_fix)
    ending_rewritten = doctored != winner["body"]
    winner["body"] = doctored

    Path(args.out).write_text(f"{winner['title']}. {winner['body']}", encoding="utf-8")
    meta = {k: v for k, v in winner.items() if k not in ("body", "comments")}
    meta["curation"] = best_score.model_dump()
    meta["ending_rewritten"] = ending_rewritten
    Path(args.out).with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    print("Writing platform upload copy...")
    copy = generate_upload_copy(winner)
    upload = copy.model_dump()
    # Both platforms get their AI toggle set at upload time; recorded here so
    # the uploaders (and a human posting manually) don't forget.
    upload["ai_disclosure"] = {"tiktok_aigc_label": True, "youtube_altered_content": True}
    upload["source_permalink"] = winner["permalink"]
    Path(args.out).with_suffix(".upload.json").write_text(
        json.dumps(upload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"TikTok caption: {copy.tiktok_caption}")

    print(f"\nPicked: '{winner['title']}' (virality {best_score.overall}/100, {best_score.category})")
    print(f"Source: {winner['permalink']}")


if __name__ == "__main__":
    main()
