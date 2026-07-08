"""
build_video.py

Combines:
  - a background video you supply (e.g. Minecraft parkour footage)
  - a narration audio track (from narrate.py)
  - a burn-in caption file (.ass, from narrate.py)

into a finished 1080x1920 (9:16) vertical mp4, ready to upload. Only needs
ffmpeg -- no network access required, so this step works fine even on a
Raspberry Pi with no GPU.

The background clip is looped/trimmed to match the narration length, then
center-cropped to 9:16 so it fills a phone screen without letterboxing.
"""
import argparse
import random
import shlex
import subprocess
import sys
from pathlib import Path


ASSETS = Path(__file__).parent / "assets"


def ensure_whoosh() -> Path:
    """Synthesize a whoosh SFX once (filtered pink-noise swell) and cache it.
    Played as the intro card fades out."""
    ASSETS.mkdir(exist_ok=True)
    wav = ASSETS / "whoosh.wav"
    if not wav.exists():
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=0.7:amplitude=0.9:seed=7",
            "-af", ("highpass=f=300,lowpass=f=2800,"
                    "afade=t=in:st=0:d=0.22,afade=t=out:st=0.32:d=0.38,volume=2.0"),
            str(wav),
        ], check=True)
    return wav


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build(background: Path, narration: Path, captions: Path, out_path: Path, width: int, height: int,
          card: Path | None = None, card_until: float = 0.0):
    audio_duration = probe_duration(narration)
    bg_duration = probe_duration(background)

    # Random start offset into the background clip so repeated runs don't all
    # open on frame 0 of the same parkour footage.
    max_start = max(bg_duration - audio_duration, 0) if bg_duration > audio_duration else 0
    start_offset = random.uniform(0, max_start) if max_start > 0 else 0

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-ss", f"{start_offset:.2f}",
        "-i", str(background),
        "-i", str(narration),
    ]

    # Scale so BOTH dimensions are >= target (covers the frame regardless of
    # whether the source is landscape, square, or already portrait), then
    # center-crop down to the exact target size, then burn in captions.
    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )
    # ffmpeg's filtergraph parser treats backslashes as escapes, so Windows
    # paths must be converted to forward slashes (and drive-colons escaped).
    subs_path = captions.as_posix().replace(":", "\\:")
    subs = f"subtitles='{subs_path}'"

    if card is not None and card_until > 0:
        # Intro card: overlay the post graphic, upper-middle of the frame,
        # until the narrator finishes the title, with a 0.3s fade-out and a
        # whoosh SFX mixed in as it leaves.
        whoosh = ensure_whoosh()
        fade_start = max(card_until - 0.3, 0)
        whoosh_ms = int(max(fade_start - 0.1, 0) * 1000)
        filter_complex = (
            f"[0:v]{base}[bg];"
            f"[2:v]format=rgba,fade=t=out:st={fade_start:.2f}:d=0.3:alpha=1[card];"
            f"[bg][card]overlay=(W-w)/2:(H-h)/2.8:enable='lt(t,{card_until:.2f})'[withcard];"
            f"[withcard]{subs}[vout];"
            f"[3:a]adelay={whoosh_ms}|{whoosh_ms},volume=0.8[wh];"
            f"[1:a][wh]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        cmd += ["-i", str(card), "-i", str(whoosh),
                "-filter_complex", filter_complex, "-map", "[vout]", "-map", "[aout]"]
    else:
        cmd += ["-vf", f"{base},{subs}", "-map", "0:v:0", "-map", "1:a:0"]

    cmd += [
        "-t", f"{audio_duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description="Assemble the final vertical video.")
    ap.add_argument("--background", required=True, help="your Minecraft parkour (or other) background footage")
    ap.add_argument("--narration", default="narration.mp3")
    ap.add_argument("--captions", default="captions.ass")
    ap.add_argument("--out", default="final.mp4")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--card", default=None, help="intro post-card PNG (from post_card.py)")
    ap.add_argument("--card-until", type=float, default=0.0,
                    help="seconds the card stays on screen (use title_end from narrate.py --out-timing)")
    args = ap.parse_args()

    background, narration, captions = Path(args.background), Path(args.narration), Path(args.captions)
    card = Path(args.card) if args.card else None
    for p in (background, narration, captions, *( [card] if card else [] )):
        if not p.exists():
            print(f"Missing input file: {p}", file=sys.stderr)
            sys.exit(1)

    build(background, narration, captions, Path(args.out), args.width, args.height,
          card=card, card_until=args.card_until)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
