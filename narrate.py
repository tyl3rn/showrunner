"""
narrate.py

Turns a text file into:
  - narration.mp3  (free TTS voiceover via edge-tts / Microsoft Edge voices)
  - captions.ass   (word-timed captions styled for vertical short-form video)

Needs outbound network access to Microsoft's edge-tts endpoint. Run
`edge-tts --list-voices` to see all available voices (hundreds of
languages/accents). en-US-GuyNeural and en-US-ChristopherNeural read in a
calm, clear register that works well for reddit-story narration.
"""
import argparse
import asyncio
import json
import random
from pathlib import Path

import edge_tts

# ASS styles tuned for 1080x1920 (9:16) TikTok-style captions: big bold text
# dead-center of the screen (Alignment=5), 1-2 words at a time, fast {\fad}
# fade-in. Each VIDEO uses one color -- all-white or all-yellow -- picked at
# random per run so the channel alternates looks between videos.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: White,Arial Black,88,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1
Style: Yellow,Arial Black,88,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

CAPTION_COLORS = ["White", "Yellow"]

# US-English male neural voices; one is picked at random per video so the
# channel doesn't sound like a single robot.
MALE_VOICES = [
    "en-US-GuyNeural", "en-US-ChristopherNeural", "en-US-EricNeural",
    "en-US-AndrewNeural", "en-US-BrianNeural", "en-US-RogerNeural",
    "en-US-SteffanNeural",
]


def ts(offset_100ns: float) -> str:
    """Convert edge-tts offset (100-nanosecond ticks) to an ASS h:mm:ss.cc timestamp."""
    seconds = offset_100ns / 10_000_000
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


async def synthesize(text: str, voice: str, rate: str, out_mp3: Path, out_ass: Path,
                     words_per_caption: int, title_words: int = 0, out_timing: Path | None = None):
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words = []
    with open(out_mp3, "wb") as audio_f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(chunk)

    if not words:
        raise RuntimeError(
            "No word-boundary events came back -- check the voice name and that "
            "this machine can actually reach Microsoft's TTS endpoint."
        )

    # The narrator reads the title while the intro card is on screen; captions
    # start with the story body. title_end tells the builder when to whoosh
    # the card away.
    title_words = min(title_words, len(words))
    title_end = 0.0
    if title_words:
        last = words[title_words - 1]
        title_end = (last["offset"] + last["duration"]) / 10_000_000
    if out_timing is not None:
        out_timing.write_text(json.dumps({"title_end": round(title_end, 2)}), encoding="utf-8")

    # One caption color for the whole video; alternates randomly per run.
    color = random.choice(CAPTION_COLORS)
    print(f"Caption color: {color}")

    caption_words = words[title_words:]
    groups = []
    i = 0
    while i < len(caption_words):
        # words_per_caption <= 0 means "random 1-2 words" (punchy TikTok pacing)
        n = words_per_caption if words_per_caption > 0 else random.choice([1, 2])
        group = caption_words[i : i + n]
        i += n
        groups.append({
            "start": group[0]["offset"],
            "end": group[-1]["offset"] + group[-1]["duration"],
            "text": " ".join(w["text"] for w in group).upper(),
        })

    # Keep text on screen continuously: each caption holds until the next one
    # starts (capped at +2s so a word doesn't linger through a long pause).
    # Without this, every breath in the narration leaves the screen blank.
    MAX_HOLD = 2 * 10_000_000  # ASS offsets are 100ns ticks
    for g, nxt in zip(groups, groups[1:]):
        g["end"] = min(nxt["start"], g["end"] + MAX_HOLD)
    if groups:
        groups[-1]["end"] += 5_000_000  # let the last caption breathe 0.5s

    lines = [ASS_HEADER]
    for g in groups:
        lines.append(
            f"Dialogue: 0,{ts(g['start'])},{ts(g['end'])},{color},,0,0,0,,{{\\fad(50,30)}}{g['text']}\n"
        )
    # MUST be utf-8: libass rejects (and silently drops) non-UTF-8 dialogue
    # lines, and Windows' default write encoding is cp1252.
    out_ass.write_text("".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Narrate a story into TTS audio + styled burn-in captions.")
    ap.add_argument("text_file", help="plain text file, e.g. story.txt from reddit_fetch.py")
    ap.add_argument("--voice", default="random-male",
                    help="'random-male' shuffles between US male voices; or name one explicitly (`edge-tts --list-voices`)")
    ap.add_argument("--rate", default="+33%", help="speech rate adjustment; +33%% = 1.33x speed")
    ap.add_argument("--words-per-caption", type=int, default=1,
                    help="words shown at once (0 = random 1-2)")
    ap.add_argument("--out-audio", default="narration.mp3")
    ap.add_argument("--out-captions", default="captions.ass")
    ap.add_argument("--title", default="",
                    help="post title; narrated over the intro card, captions start with the body")
    ap.add_argument("--out-timing", default=None, help="write {title_end: seconds} JSON here")
    args = ap.parse_args()

    voice = random.choice(MALE_VOICES) if args.voice == "random-male" else args.voice
    print(f"Voice: {voice} at {args.rate}")

    raw = Path(args.text_file).read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:  # tolerate files written before the utf-8 fix
        decoded = raw.decode("cp1252")
    text = " ".join(decoded.split())  # normalize whitespace/newlines

    asyncio.run(
        synthesize(
            text, voice, args.rate, Path(args.out_audio), Path(args.out_captions),
            args.words_per_caption, title_words=len(args.title.split()),
            out_timing=Path(args.out_timing) if args.out_timing else None,
        )
    )
    print(f"Wrote {args.out_audio} and {args.out_captions}")


if __name__ == "__main__":
    main()
