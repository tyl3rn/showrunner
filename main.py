"""
main.py

One-command pipeline: pick a Reddit story -> narrate it -> assemble the
final vertical video over your background footage.

    python3 main.py AskReddit --background parkour.mp4

Runs curate.py (Claude-scored story selection), narrate.py, and
build_video.py in sequence. Needs:
  - network access to reddit.com and Microsoft's edge-tts endpoint
  - ANTHROPIC_API_KEY in the environment (for the curation agent)
  - a background video file you provide (Minecraft parkour or otherwise)
  - ffmpeg installed

curate.py exits 2 when no candidate clears the virality bar -- main.py
treats that as "skip this run" rather than an error, so a scheduled loop
just quietly waits for the next slot instead of posting a dud.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description="Full reddit-story-video pipeline.")
    ap.add_argument("subreddit", nargs="?", default="auto",
                    help="a subreddit, or 'auto' (default) to rotate the curated pool")
    ap.add_argument("--background", required=True, help="path to your Minecraft parkour (or other) footage")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "controversial"])
    ap.add_argument("--time-filter", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--voice", default="random-male")
    ap.add_argument("--rate", default="+50%")
    ap.add_argument("--words-per-caption", type=int, default=1, help="words shown at once (0 = random 1-2)")
    ap.add_argument("--workdir", default="run_output", help="where intermediate files land")
    ap.add_argument("--out-dir", default=None, help="where the finished video lands (default: workdir)")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir) if args.out_dir else workdir
    out_dir.mkdir(parents=True, exist_ok=True)

    story_txt = workdir / "story.txt"
    narration_mp3 = workdir / "narration.mp3"
    captions_ass = workdir / "captions.ass"

    curate_cmd = [
        sys.executable, str(HERE / "curate.py"), args.subreddit,
        "--listing", args.listing, "--time-filter", args.time_filter,
        "--seen-file", str(workdir / "seen_story_ids.json"),
        "--out", str(story_txt),
    ]
    print("+", " ".join(curate_cmd))
    result = subprocess.run(curate_cmd)
    if result.returncode == 2:
        print("No story cleared the virality bar this run -- skipping.")
        sys.exit(0)
    if result.returncode != 0:
        sys.exit(result.returncode)

    meta_raw = story_txt.with_suffix(".meta.json").read_bytes()
    try:
        meta = json.loads(meta_raw.decode("utf-8"))
    except UnicodeDecodeError:  # tolerate files written before the utf-8 fix
        meta = json.loads(meta_raw.decode("cp1252"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final_mp4 = out_dir / f"{meta['subreddit']}-{stamp}.mp4"
    card_png = workdir / "card.png"
    timing_json = workdir / "timing.json"

    run([
        sys.executable, str(HERE / "post_card.py"),
        "--title", meta["title"], "--subreddit", meta["subreddit"],
        "--out", str(card_png),
    ])

    run([
        sys.executable, str(HERE / "narrate.py"), str(story_txt),
        "--voice", args.voice, "--rate", args.rate,
        "--words-per-caption", str(args.words_per_caption),
        "--title", meta["title"], "--out-timing", str(timing_json),
        "--out-audio", str(narration_mp3), "--out-captions", str(captions_ass),
    ])

    # Card stays up while the narrator reads the title, minimum 3s so the
    # hook visual has time to register.
    card_until = max(3.0, json.loads(timing_json.read_text(encoding="utf-8"))["title_end"])

    run([
        sys.executable, str(HERE / "build_video.py"),
        "--background", args.background,
        "--narration", str(narration_mp3), "--captions", str(captions_ass),
        "--card", str(card_png), "--card-until", f"{card_until:.2f}",
        "--out", str(final_mp4),
    ])

    # Park the platform captions next to the video, matching its name, so an
    # uploader (or a human) grabs video + text as a pair.
    upload_src = story_txt.with_suffix(".upload.json")
    if upload_src.exists():
        shutil.copyfile(upload_src, final_mp4.with_suffix(".upload.json"))

    print(f"\nDone -> {final_mp4}")


if __name__ == "__main__":
    main()
