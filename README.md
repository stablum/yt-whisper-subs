# yt_whisper_subs.py

`yt_whisper_subs.py` is a Windows-oriented subtitle pipeline for YouTube videos
and local video files. It downloads a lossy compressed video when given a URL,
extracts a small lossy audio track, runs local OpenAI Whisper to create primary
language subtitles, optionally translates Dutch subtitles to English with the
OpenAI Responses API, and optionally launches `mpv` with dual subtitles.

This README is intentionally extensive. It is meant both as user documentation
and as a design handoff for future Codex sessions that need to modify the
script without rediscovering all of the local decisions.

Placement note: this README was written to be moved into a dedicated project
directory. If it is still sitting next to unrelated scripts, do not assume that
location is the final project layout.

## Current Intent

The script is optimized for this workflow:

1. Run it on a Dutch YouTube video.
2. Keep a local lossy video file under `~/Videos/yt-whisper-subs/videos`.
3. Generate Dutch subtitles locally with Whisper.
4. Compact the Dutch subtitle cues so fragmented speech becomes easier to read.
5. Send the full compacted Dutch SRT to the OpenAI API in one request.
6. Receive natural English translations with the exact same cue count and time
   markings as the compacted Dutch SRT.
7. Store subtitle archives under `~/Videos/yt-whisper-subs/subtitles`.
8. Store the `.srt` sidecars beside the video so `mpv` can discover them.
9. Re-run cheaply: if the video and requested subtitles already exist, skip
   download, audio extraction, CUDA checks, Whisper, and OpenAI, then just open
   `mpv`.

The script can also be used on a local video file, can skip playback, can avoid
English translation, and can fall back to Whisper's built-in audio translation.

## Important Defaults

These defaults are hard-coded near the top of the script:

| Area | Default |
| --- | --- |
| Output root | `~/Videos/yt-whisper-subs` |
| Managed virtual environment | `.venv` beside the script |
| Whisper language | `nl` |
| Primary Whisper model | `turbo` |
| English translation provider | `openai` |
| OpenAI translation model | `gpt-5.5` |
| OpenAI reasoning effort | `xhigh` |
| OpenAI timeout | `900` seconds |
| OpenAI env file | `.env` beside the script |
| Device | `cuda` |
| Python version for uv venv | `3.12` |
| Torch CUDA wheel index | `https://download.pytorch.org/whl/cu128` |
| Downloaded video container | `mkv` |
| yt-dlp format selector | `bv*+ba/b` |
| yt-dlp progress interval | `1` second |
| Extracted audio format | `opus` |
| Keep audio after run | yes |
| Play after generation | yes |
| Dutch-to-English subtitles | yes, when `--language` is Dutch and task is `transcribe` |
| Dual subtitle mode | yes |
| Primary subtitle color | `#FFE066` |
| Secondary subtitle color | `#66D9EF` |
| Primary subtitle position | `100` |
| Secondary subtitle position | `8` |
| Dual subtitle font size | `80` |
| Primary font scale | `0.6` |
| Subtitle compaction mode | `english` |
| Compaction gap | `0.9` seconds |
| Compaction max merged duration | `9.0` seconds |
| Compaction max merged chars | `180` |
| Compaction max characters per second | `25.0` |
| Wrapped subtitle line width | `50` |

One subtle default matters: when OpenAI English translation is enabled, the
primary Dutch SRT is compacted before translation even though `--compact-subs`
defaults to `english`. This exists so the OpenAI-translated English file has the
same timestamps and cue count as the compacted Dutch file that the user actually
wants to read.

If `--no-compact-subs` is used, this override is disabled. In that case OpenAI
receives the un-compacted primary SRT.

## Quick Start

From the directory containing the script:

```powershell
python .\yt_whisper_subs.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

or:

```powershell
uv run .\yt_whisper_subs.py --url "https://www.youtube.com/watch?v=VIDEO_ID"
```

If the `.venv` beside the script is missing, the script uses `uv` to create it
and install the Python dependencies it needs for Whisper and yt-dlp.

For OpenAI translation, create a `.env` file next to the script:

```dotenv
OPENAI_API_KEY=sk-...
```

Do not commit `.env`.

Run without opening `mpv`:

```powershell
python .\yt_whisper_subs.py --no-play "https://www.youtube.com/watch?v=VIDEO_ID"
```

Force a fresh download and fresh subtitle generation:

```powershell
python .\yt_whisper_subs.py --force "https://www.youtube.com/watch?v=VIDEO_ID"
```

Use local Whisper audio translation instead of OpenAI SRT translation:

```powershell
python .\yt_whisper_subs.py --english-translation-provider whisper "https://www.youtube.com/watch?v=VIDEO_ID"
```

Use a local video file:

```powershell
python .\yt_whisper_subs.py --video-file "C:\path\to\video.mkv"
```

Pass browser cookies to yt-dlp:

```powershell
python .\yt_whisper_subs.py --cookies-from-browser firefox "https://www.youtube.com/watch?v=VIDEO_ID"
```

## What The Script Produces

For a YouTube URL, the default output root is:

```text
C:\Users\<you>\Videos\yt-whisper-subs
```

Inside it:

```text
yt-whisper-subs\
  videos\
    Video title [youtube_id].mkv
    Video title [youtube_id].srt
    Video title [youtube_id].en.srt
  audio\
    Video title [youtube_id].opus
  subtitles\
    Video title [youtube_id].srt
    Video title [youtube_id].en.srt
    Video title [youtube_id].uncompact.srt
    Video title [youtube_id].en.uncompact.srt
```

The exact `.uncompact.*` files appear only when compaction changed an existing
subtitle file and a backup did not already exist.

The script writes subtitles in two places:

1. Sidecar subtitles beside the video, for `mpv` auto-detection:
   `videos\Video title [id].srt` and `videos\Video title [id].en.srt`.
2. Archive subtitles under `subtitles\`, so subtitle yields survive even if
   sidecars are moved or missing.

This duplication is intentional. Sidecars are for playback ergonomics. The
archive directory is for durable yield tracking.

The video file is intentionally not lossless. The default yt-dlp format selector
keeps YouTube's already-compressed audio/video streams and merges them into an
`mkv` container. Audio extracted for Whisper is also lossy, defaulting to Opus at
48 kbps, mono, 16 kHz.

## The End-To-End Pipeline

The main flow is:

```text
parse args
resolve source
  if URL:
    find exact cached video by YouTube ID unless --force
    otherwise download with yt-dlp
  if local file:
    resolve local path
derive yield paths from video stem
hydrate missing sidecar/archive subtitle pairs
compact existing subtitles when needed
print yield paths
if all requested yields exist and not --force:
  skip expensive work
  optionally delete audio
  optionally play in mpv
otherwise:
  decide whether Whisper is needed
  if Whisper is needed:
    ensure .venv
    check ffmpeg
    check CUDA if --device cuda
  generate primary subtitles if needed
  generate English subtitles if requested and needed
  optionally delete audio
  optionally play in mpv
```

The most important engineering property is parsimony: the script should not
consume bandwidth, GPU time, CPU time, or OpenAI API calls when the requested
yields are already present.

## Yield Reuse And `--force`

On normal runs, the script reuses existing yields.

For URL input, the video cache lookup is intentionally exact. The script extracts
the YouTube video ID from supported URL shapes and looks for a final media file
whose stem ends with `[video_id]`. It does not pick "the newest downloaded video"
as a fallback. That earlier behavior was risky because a new URL could
accidentally play a previously downloaded video.

Supported YouTube URL shapes include:

- `https://www.youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID`
- `/shorts/VIDEO_ID`
- `/live/VIDEO_ID`
- `/embed/VIDEO_ID`
- `/v/VIDEO_ID`

If everything requested already exists, the script prints a skip message and
does not run yt-dlp, ffmpeg, CUDA probing, Whisper, or OpenAI. It still launches
`mpv` unless `--no-play` is set.

`--force` means:

- Re-download URL videos instead of reusing a cached one.
- Pass `--force-overwrites` to yt-dlp.
- Regenerate subtitles.
- Do not fall back to an existing final video if yt-dlp fails during a forced
  redownload.

This is deliberately strong. It is useful when a previous yield is corrupt or
when the user wants to replace old English subtitles with the current OpenAI
translation path. It is also expensive.

If only one yield is unwanted, manually deleting that yield can be cheaper than
using `--force`. For example, deleting only `*.en.srt` lets the script regenerate
English translation from an existing primary SRT without re-downloading video or
rerunning Whisper.

## Dependency Management

The script expects to run on Windows. It uses:

- `uv`
- Python 3.12 in a script-local `.venv`
- `yt-dlp`
- `openai-whisper`
- `torch`
- `ffmpeg`
- `mpv`

Python dependencies are installed into `.venv` beside the script. This choice
was made because the script is intended to be portable as a single project
folder and should not depend on whichever Python packages happen to be installed
globally.

The script creates or updates `.venv` when:

- `--install-python-deps` is passed, or
- the expected `.venv\Scripts\python.exe` is missing.

It installs:

```text
wheel
setuptools
yt-dlp
openai-whisper
torch
```

When `--device cuda` is used, Torch is installed from the configured CUDA index:

```text
https://download.pytorch.org/whl/cu128
```

The script does not install the OpenAI Python SDK. The OpenAI Responses API call
uses the Python standard library (`urllib`). That was a deliberate dependency
decision: the OpenAI translation path should not require adding and maintaining
another Python package in the Whisper environment.

`ffmpeg` and `mpv` are external executables. With `--install-tools`, the script
attempts to install/update `uv`, `ffmpeg`, and `mpv` via Scoop. If Scoop is not
available, install those tools manually.

## Source Handling

The script accepts exactly one source:

- positional `source`
- `--url`
- `--video-file`

If a positional source starts with `http://` or `https://`, it is treated as a
URL. Otherwise it is treated as a local video file.

Local video files are resolved with `Path(...).expanduser().resolve()` and must
already exist.

## Download Design

The download command is built around yt-dlp:

```text
python -m yt_dlp
  --no-playlist
  --windows-filenames
  --no-part
  --progress
  --progress-delta <seconds>
  -f <format selector>
  --merge-output-format <container>
  --print after_move:filepath
  -o "%(title).180B [%(id)s].%(ext)s"
```

Design notes:

- `--no-playlist` avoids accidentally downloading an entire playlist.
- `--windows-filenames` prevents filenames that are awkward on Windows.
- `--no-part` avoids `.part` rename problems seen on Windows when another
  process or terminal interaction interferes with a partial file.
- `--progress-delta` defaults to `1`, so progress output is visible but not too
  noisy.
- `--print after_move:filepath` gives the script the final filename.
- The output filename includes the YouTube ID in square brackets so exact cache
  lookup is possible.
- The title is capped with `%(title).180B` to reduce path-length and filesystem
  pain.

The default format selector is:

```text
bv*+ba/b
```

That means "best video-only plus best audio-only, or best combined format as a
fallback." These are compressed streams from the source platform; the script is
not making a lossless video transcode.

The default merge container is:

```text
mkv
```

`mkv` is a forgiving container for mixed codecs, which is useful for YouTube
downloads.

## Audio Extraction

Whisper does not need the original video stream. The script extracts a compact
audio file:

```text
ffmpeg -hide_banner -y -i <video> -vn -ac 1 -ar 16000 <codec args> <audio>
```

Supported kept audio formats:

| Format | Codec args |
| --- | --- |
| `opus` | `-c:a libopus -b:a 48k -vbr on` |
| `m4a` | `-c:a aac -b:a 64k` |
| `mp3` | `-c:a libmp3lame -b:a 64k` |

The default is Opus because it is small and suitable for speech. The audio is
kept by default because it can be useful for reruns and inspection. Use
`--delete-audio` to remove it after the run.

## Local Whisper Transcription

Primary subtitles are generated with the local Whisper command inside `.venv`:

```text
whisper.exe <audio>
  --model <model>
  --task <transcribe|translate>
  --output_format srt
  --device <cuda|cpu>
  --fp16 True|False
  --language <language>
  --output_dir <temporary subtitle directory>
```

The default task is `transcribe`, default language is `nl`, and default model is
`turbo`.

If the language is not `auto`, the script passes it explicitly to Whisper. This
is faster and more deterministic for the intended Dutch workflow.

Before running Whisper on CUDA, the script checks PyTorch CUDA visibility:

```python
torch.cuda.is_available()
torch.cuda.device_count()
torch.cuda.get_device_name(0)
```

If CUDA is requested but not visible, the script exits with a clear error rather
than silently running a huge job on CPU.

The script also checks and removes corrupt or zero-byte Whisper model cache files
when it can infer the expected SHA-256 from Whisper's model URL. This protects
against interrupted model downloads.

## English Translation Modes

English subtitles are generated only when all of these are true:

- the internal `english_for_dutch` flag is true, which is the default and can be
  disabled with `--no-english-for-dutch`
- `--task transcribe`, which is the default
- `--language` is recognized as Dutch (`nl`, `dutch`, or `nederlands`)

Important: `--language auto` will not trigger the Dutch-to-English path, even if
Whisper would have detected Dutch. This is intentional in the current code
because the decision is made before Whisper runs.

There are two English translation providers.

### Default: OpenAI SRT Translation

The default is:

```text
--english-translation-provider openai
```

This mode does not translate from audio. Instead, it translates from the primary
SRT after primary compaction has occurred.

The contract is:

1. The Dutch SRT is parsed into cues.
2. The complete compacted Dutch SRT is placed into one prompt.
3. The prompt asks for natural idiomatic English.
4. The prompt forbids merging, splitting, adding, or omitting cues.
5. The API is asked for strict JSON:

   ```json
   {
     "translations": [
       {
         "index": 1,
         "text": "..."
       }
     ]
   }
   ```

6. The script validates that:
   - the JSON parses,
   - `translations` is a list,
   - the list length equals the source cue count,
   - each index is exactly the expected 1-based cue number,
   - each text value is a non-empty string.
7. The script renders a new English SRT by pairing each translated text with the
   original source cue start and end timestamps.

This gives the English file the same cue count and cue timings as the compacted
Dutch file. That property is the main reason this mode exists.

The OpenAI request uses:

```text
POST https://api.openai.com/v1/responses
```

with:

```json
{
  "model": "gpt-5.5",
  "input": "...complete prompt and SRT...",
  "reasoning": {
    "effort": "xhigh"
  },
  "text": {
    "format": {
      "type": "json_schema",
      "name": "srt_translation",
      "strict": true,
      "schema": "..."
    }
  },
  "store": false
}
```

The exact default model and effort are configurable:

```powershell
python .\yt_whisper_subs.py `
  --openai-translation-model gpt-5.5 `
  --openai-reasoning-effort xhigh `
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

The API key is loaded from either:

- `OPENAI_API_KEY` already in the environment, or
- `.env` beside the script, or
- a custom path passed with `--openai-env-file`.

The `.env` parser is intentionally simple. It supports:

```dotenv
OPENAI_API_KEY=...
export OPENAI_API_KEY=...
OPENAI_API_KEY="..."
OPENAI_API_KEY='...'
```

It skips blank lines and comments.

Privacy note: this mode sends the entire compacted subtitle text to OpenAI in
one request. That is deliberate for translation quality and full-context
coherence, but it is still a cloud API call containing the transcript.

Cost note: one request over a long SRT can be large. The script currently does
not chunk long videos, because chunking would reduce the whole-document context
that motivated this mode. Very long videos may exceed model context limits or
be expensive.

### Fallback: Whisper Audio Translation

The older path is still available:

```text
--english-translation-provider whisper
```

This runs Whisper again on the audio with:

```text
--task translate
--language <source language>
--model <english model>
```

The default English Whisper model is:

- `medium` when the primary model is `turbo`
- otherwise the same as `--model`

This is because Whisper `turbo` is optimized for transcription and is not the
preferred path for translation in this script. OpenAI SRT translation is now the
default because it gives better English prose and uses full subtitle context.

Whisper-translated English subtitles can be compacted afterward. OpenAI
translated English subtitles are not compacted afterward, because compacting
them would break the exact cue-count/timestamp relationship with the compacted
Dutch source.

## Subtitle Compaction

Whisper often emits subtitle cues that are too fragmented. The script has a
downstream compaction step that merges adjacent cues when doing so appears safe.

The compaction parser is deliberately simple and SRT-specific:

- normalize line endings,
- split on blank lines,
- ignore numeric cue indexes,
- parse `HH:MM:SS,mmm --> HH:MM:SS,mmm`,
- normalize cue text whitespace.

Two adjacent cues may merge when:

- the gap between them is at most `--compact-gap`,
- the first cue does not end in strong punctuation, unless the period is judged
  to be a likely false Whisper period,
- the merged duration is at most `--compact-max-duration`,
- the merged text length is at most `--compact-max-chars`,
- the merged characters-per-second rate is at most `--compact-max-cps`.

The soft-period logic exists because Whisper sometimes inserts periods inside a
sentence. It checks common English function words at the end and beginning of
cues and can remove a false terminal period before merging.

Defaults:

```text
--compact-subs english
--compact-soft-periods english
--compact-gap 0.9
--compact-max-duration 9.0
--compact-max-chars 180
--compact-max-cps 25.0
--compact-line-width 50
```

In normal non-OpenAI mode, `english` means "compact only English subtitles."

In default OpenAI mode, the script sets an internal flag so the primary Dutch
SRT is also compacted first. This lets the translated English file match the
compacted Dutch SRT exactly.

Compaction writes backups. For a file:

```text
Video title [id].srt
```

the backup is:

```text
Video title [id].uncompact.srt
```

Backups are created only if the compacted output differs and no backup already
exists. If a final compacted subtitle is missing but the `.uncompact.srt` backup
exists, the script can rebuild the final file from the backup according to the
current compaction settings.

This backup design was chosen because compaction is heuristic. It should be
reversible enough that future tuning does not require rerunning Whisper.

## mpv Playback

By default the script opens `mpv` after producing or verifying yields.

Playback command shape:

```text
mpv --sub-auto=no --sub-file=<primary> --sub-file=<secondary> <video>
```

If dual subtitles are enabled and at least two subtitle files exist, the script
does more:

- The primary subtitle remains the original `.srt` file.
- The secondary subtitle is converted to a temporary `.ass` file.
- `mpv` is launched with `--sid=1` and `--secondary-sid=2`.
- `--sub-color`, `--sub-font-size`, and `--sub-pos` style the primary subtitle.
- The generated ASS file styles the secondary subtitle with its own color,
  position, and font size.
- `--secondary-sub-ass-override=no` tells mpv not to override the secondary ASS
  styling.

The default arrangement is:

- primary subtitles at position `100` with color `#FFE066`,
- secondary subtitles at position `8` with color `#66D9EF`.

Position values are mpv-style percentages. In this script:

- lower values place ASS subtitles near the top,
- higher values place subtitles near the bottom,
- middle values use center alignment.

The `.ass` file is temporary and exists only for playback. It is not a persistent
yield. Persistent subtitle yields remain `.srt`.

The reason for using ASS only for the secondary track is practical: mpv can show
two subtitle tracks at once, but styling the secondary track differently is much
easier when the secondary track carries its own ASS style. Keeping the primary
track as native SRT preserves more of mpv's normal primary-subtitle behavior.

Disable dual display and load subtitles as selectable tracks:

```powershell
python .\yt_whisper_subs.py --no-dual-subs "https://www.youtube.com/watch?v=VIDEO_ID"
```

Customize colors:

```powershell
python .\yt_whisper_subs.py `
  --dual-sub-primary-color "#FFE066" `
  --dual-sub-secondary-color "#66D9EF" `
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

Customize sizes:

```powershell
python .\yt_whisper_subs.py `
  --dual-sub-font-size 90 `
  --dual-sub-primary-font-size 54 `
  --dual-sub-secondary-font-size 90 `
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

## Command Line Reference

### Source Arguments

| Option | Meaning |
| --- | --- |
| `source` | Positional URL or local video path. |
| `--url URL` | Explicit YouTube/video URL. Mutually exclusive with `--video-file`. |
| `--video-file PATH` | Explicit local video file. Mutually exclusive with `--url`. |

Exactly one source must be provided.

### Output And Source Language

| Option | Default | Meaning |
| --- | --- | --- |
| `--out-dir DIR` | `~/Videos/yt-whisper-subs` | Root for `videos`, `audio`, and `subtitles`. |
| `--language LANGUAGE` | `nl` | Whisper language code, or `auto`. |
| `--task transcribe|translate` | `transcribe` | Primary Whisper task. |

### Whisper And CUDA

| Option | Default | Meaning |
| --- | --- | --- |
| `--model MODEL` | `turbo` | Primary Whisper model. |
| `--device cuda|cpu` | `cuda` | Whisper/PyTorch device. |
| `--torch-index-url URL` | CUDA 12.8 PyTorch index | Torch install index when using CUDA. |
| `--python-version VERSION` | `3.12` | Python version for the uv-managed `.venv`. |
| `--install-python-deps` | off | Recreate/update Python dependencies even if `.venv` exists. |

Whisper model choices:

```text
tiny, base, small, medium, large, large-v2, large-v3, turbo
```

### English Translation

| Option | Default | Meaning |
| --- | --- | --- |
| `--no-english-for-dutch` | off | Disable automatic English subtitles for Dutch input. |
| `--english-translation-provider openai|whisper` | `openai` | Select OpenAI SRT translation or Whisper audio translation. |
| `--english-model MODEL` | conditional | Whisper model for the `whisper` provider only. |
| `--openai-translation-model MODEL` | `gpt-5.5` | Model for OpenAI SRT translation. |
| `--openai-reasoning-effort EFFORT` | `xhigh` | Reasoning effort for OpenAI SRT translation. |
| `--openai-timeout SECONDS` | `900` | API request timeout. |
| `--openai-env-file PATH` | `.env` beside script | Env file to load for `OPENAI_API_KEY`. |

OpenAI reasoning effort choices:

```text
none, minimal, low, medium, high, xhigh
```

### Download And Media

| Option | Default | Meaning |
| --- | --- | --- |
| `--video-format FORMAT` | `bv*+ba/b` | yt-dlp format selector. |
| `--merge-output-format mkv|mp4|webm` | `mkv` | Container for downloaded streams. |
| `--download-progress-delta SECONDS` | `1` | Minimum interval between yt-dlp progress updates. |
| `--audio-format opus|m4a|mp3` | `opus` | Kept lossy audio format for Whisper. |
| `--keep-audio` | on | Keep extracted audio. |
| `--delete-audio` | off | Delete extracted audio after the run. |
| `--cookies-from-browser BROWSER` | unset | Forward browser cookies to yt-dlp. |

### Playback

| Option | Default | Meaning |
| --- | --- | --- |
| `--no-play` | off | Do not open mpv. |
| `--no-dual-subs` | off | Load multiple subtitle tracks but do not display both at once. |
| `--dual-sub-primary-color COLOR` | `#FFE066` | Primary subtitle color. |
| `--dual-sub-secondary-color COLOR` | `#66D9EF` | Secondary subtitle color. |
| `--dual-sub-primary-pos FLOAT` | `100` | Primary subtitle position. |
| `--dual-sub-secondary-pos FLOAT` | `8` | Secondary subtitle position. |
| `--dual-sub-font-size FLOAT` | `80` | Base dual subtitle font size. |
| `--dual-sub-primary-font-size FLOAT` | derived | Override primary font size. |
| `--dual-sub-secondary-font-size FLOAT` | derived | Override secondary font size. |

Colors must be `#RRGGBB` or `#RRGGBBAA`.

### Compaction

| Option | Default | Meaning |
| --- | --- | --- |
| `--compact-subs english|all|none` | `english` | Which subtitles to compact. |
| `--no-compact-subs` | off | Same as `--compact-subs none`. |
| `--compact-soft-periods english|all|none` | `english` | Allow likely false periods to be mergeable. |
| `--no-compact-soft-periods` | off | Same as `--compact-soft-periods none`. |
| `--compact-gap FLOAT` | `0.9` | Maximum cue gap that may merge. |
| `--compact-max-duration FLOAT` | `9.0` | Maximum merged cue duration. |
| `--compact-max-chars INT` | `180` | Maximum merged cue character length. |
| `--compact-max-cps FLOAT` | `25.0` | Maximum merged reading speed. |
| `--compact-line-width INT` | `50` | Wrap width when rendering compacted SRT. |

### Execution Control

| Option | Default | Meaning |
| --- | --- | --- |
| `--force` | off | Re-download URL videos and regenerate subtitles. |
| `--install-tools` | off | Install/update `uv`, `ffmpeg`, and `mpv` via Scoop. |

## Software Engineering Decisions

### A Single Script Instead Of A Package

The script is intentionally one file. The workflow is personal, operational, and
Windows-specific. Keeping everything in one file makes it easy to move into a
dedicated project directory later and easy for Codex to inspect in one pass.

The downside is file length. If this becomes a shared project, good extraction
boundaries would be:

- `cli.py`
- `downloads.py`
- `whisper_local.py`
- `srt.py`
- `compaction.py`
- `openai_translate.py`
- `mpv.py`

Do not split it casually. The current single-file shape makes local iteration
fast.

### Script-Local `.venv`

The script manages `.venv` beside itself with `uv`. This avoids global Python
state and keeps the CUDA/Whisper stack isolated.

This was chosen because PyTorch, Whisper, and Python minor versions can be
sensitive. A global Python update should not silently break the workflow.

### Standard Library OpenAI Client

The OpenAI API path uses `urllib` instead of the OpenAI SDK. The reason is
dependency parsimony: the existing venv exists primarily for Whisper and yt-dlp.
Adding the SDK would be convenient but not necessary for one JSON HTTP request.

If future code adds retries, streaming, file uploads, or richer API usage, the
SDK may become worthwhile.

### Exact YouTube ID Cache Matching

The cache lookup uses the YouTube video ID embedded in the filename. It does not
guess by newest file.

This prevents a serious UX failure: running the script on a new URL and seeing a
previous video launch because it happened to be the newest local media file.

### Lossy-Only Media Storage

The script avoids lossless video and audio yields. YouTube streams are already
lossy, and Whisper only needs compact speech audio. This keeps disk use under
control.

### `--no-part` For yt-dlp

Windows has shown rename failures with `.part` files when a process still has a
handle open. `--no-part` trades some resume behavior for fewer confusing
`WinError 32` failures during normal use.

### Full-SRT OpenAI Translation

The OpenAI path sends the whole compacted SRT in one request. This is not the
cheapest possible architecture, but it is the quality-oriented one:

- The model can see the entire discourse.
- It can translate recurring terms consistently.
- It can avoid locally plausible but globally wrong choices.
- It avoids cross-chunk style drift.

The script then validates the structured output and applies translations to the
original compacted cue timings itself. The model is not trusted to output final
SRT timestamps.

### Compact Before Translating

Compacting before translation was chosen because the final Dutch and English
files should describe the same subtitle regions. If English were translated from
uncompacted Dutch and then compacted independently, the two languages could
drift in cue count and timing.

Default OpenAI mode therefore creates this invariant:

```text
primary compacted SRT cues == English translated SRT cues
```

where equality means same number of cues and same start/end timestamps.

### Back Up Before Heuristic Compaction

Compaction is useful but heuristic. The `.uncompact.srt` backup preserves the
pre-compaction text so tuning parameters later does not require rerunning
Whisper.

### Separate Sidecar And Archive Subtitles

Sidecars are for playback. Archives are for persistence. This duplication also
lets the script repair one from the other:

- missing sidecar can be seeded from archive,
- missing archive can be synced from sidecar.

### ASS Only For Secondary Subtitles

The secondary subtitle track is converted to temporary ASS to allow separate
color, font, and position. The primary remains SRT so mpv's primary subtitle
controls remain closer to normal.

### Fail Early On CUDA Mismatch

If `--device cuda` is requested and PyTorch cannot see CUDA, the script fails
before Whisper starts. This avoids surprising multi-hour CPU runs.

## Internal Function Map

High-level groups:

| Function(s) | Responsibility |
| --- | --- |
| `parse_args` | CLI definition and source disambiguation. |
| `venv_paths`, `ensure_python_deps` | Locate and maintain `.venv`. |
| `run` | Print and execute subprocesses; optionally stream stdout. |
| `youtube_video_id`, `latest_downloaded_video` | Exact video cache lookup. |
| `download_video` | yt-dlp invocation and final path resolution. |
| `extract_audio`, `audio_codec_args` | ffmpeg audio yield creation. |
| `check_cuda`, `run_whisper` | Whisper execution and CUDA validation. |
| `parse_srt`, `render_srt` | SRT data model and serialization. |
| `compact_cues`, `may_merge_cues` | Cue compaction heuristics. |
| `save_uncompacted_backup`, `restore_subtitle_from_uncompacted_backup` | Reversible compaction support. |
| `translate_srt_with_openai` | Full-SRT OpenAI translation and rendering. |
| `openai_responses_api_request` | Raw HTTP call to Responses API. |
| `parse_openai_translations` | Strict translation response validation. |
| `hydrate_subtitle_pair`, `sync_subtitle_archive` | Sidecar/archive repair and syncing. |
| `write_ass_subtitle`, `play_video` | mpv dual-subtitle playback. |
| `main` | Pipeline orchestration and skip logic. |

The central data model is:

```python
@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str
```

This keeps SRT logic timestamp-based internally and avoids string manipulation
until final rendering.

## Future Codex Maintenance Notes

Start by preserving these invariants:

1. A second run on the same URL with all yields present must skip yt-dlp,
   ffmpeg, CUDA, Whisper, and OpenAI.
2. URL cache hits must be by exact YouTube video ID, not newest file.
3. Default OpenAI English translation must use the compacted primary SRT.
4. Default OpenAI English output must have the same cue count and timestamps as
   the compacted primary SRT.
5. Do not independently compact OpenAI English output after translation.
6. Do not print or commit `.env` or API keys.
7. Keep video and subtitle yields under `~/Videos/yt-whisper-subs` by default.
8. Keep video storage lossy.
9. Keep local Whisper/PyTorch dependencies in `.venv` beside the script.
10. Preserve sidecar/archive repair behavior.

When changing the script, useful verification commands are:

```powershell
python -m py_compile .\yt_whisper_subs.py
python .\yt_whisper_subs.py --help
```

Mock the OpenAI translation path without making an API call:

```powershell
python -c "import argparse, tempfile, json; from pathlib import Path; import yt_whisper_subs as y; d=Path(tempfile.mkdtemp()); src=d/'nl.srt'; dst=d/'en.srt'; src.write_text('1\n00:00:00,000 --> 00:00:01,200\nGoedemiddag allemaal.\n\n2\n00:00:01,200 --> 00:00:02,800\nWelkom bij de persconferentie.\n', encoding='utf-8'); y.openai_responses_api_request=lambda args,payload: {'output_text': json.dumps({'translations':[{'index':1,'text':'Good afternoon, everyone.'},{'index':2,'text':'Welcome to the press conference.'}]})}; args=argparse.Namespace(openai_translation_model='mock', openai_reasoning_effort='xhigh', compact_line_width=50); y.translate_srt_with_openai(src,dst,args); print(dst.read_text(encoding='utf-8'))"
```

Expected property: the English output should retain the exact two timestamp
ranges from the input.

Check compaction routing for default OpenAI mode:

```powershell
python -c "import argparse, yt_whisper_subs as y; args=argparse.Namespace(compact_subs='english', english_translation_provider='openai', compact_primary_for_openai_translation=True); print(y.should_compact_subtitles(args, is_english=False)); print(y.should_compact_subtitles(args, is_english=True))"
```

Expected output:

```text
True
False
```

For a real API smoke test, use a tiny SRT and the `.env` file. Keep it tiny to
avoid unnecessary cost.

## Troubleshooting

### `OPENAI_API_KEY is not set`

Create `.env` beside the script or set the environment variable:

```dotenv
OPENAI_API_KEY=sk-...
```

If using a custom env file:

```powershell
python .\yt_whisper_subs.py --openai-env-file C:\path\to\.env <source>
```

### OpenAI returns a model error

The default model is a script constant. If model availability changes for the
account or API, pass another model:

```powershell
python .\yt_whisper_subs.py --openai-translation-model MODEL <source>
```

or update `DEFAULT_OPENAI_TRANSLATION_MODEL`.

### CUDA is not visible

The script prints PyTorch CUDA visibility before Whisper. If CUDA is false:

- update NVIDIA drivers,
- confirm the correct Torch CUDA wheel installed,
- rerun with `--install-python-deps`,
- or use `--device cpu` for a slower run.

### yt-dlp warns about JavaScript runtimes

yt-dlp may warn that no supported JavaScript runtime is available. The script
does not manage that dependency. Some YouTube formats may be missing until a
runtime supported by yt-dlp is installed.

### yt-dlp download output is noisy

The script defaults to:

```text
--download-progress-delta 1
```

Increase it:

```powershell
python .\yt_whisper_subs.py --download-progress-delta 5 <source>
```

### A repeated run plays the wrong video

This should not happen for supported YouTube URL shapes because cache lookup is
by exact video ID. If it happens, inspect filenames under:

```text
~/Videos/yt-whisper-subs/videos
```

The final video filename should end with:

```text
[VIDEO_ID].mkv
```

Unsupported URL shapes may not yield a video ID, in which case no cache hit is
used.

### I want only to regenerate English subtitles

Avoid `--force` if you do not want to redownload the URL video. Delete the
English sidecar and archive:

```text
videos\Video title [id].en.srt
subtitles\Video title [id].en.srt
```

Then rerun normally. The script should reuse the video and primary subtitles,
then regenerate English with OpenAI.

### `mpv` does not auto-detect subtitles

The script writes sidecar subtitles beside the video specifically for
auto-detection:

```text
Video title [id].srt
Video title [id].en.srt
```

However, when the script launches `mpv`, it passes subtitle files explicitly and
uses `--sub-auto=no`. That prevents mpv from adding extra auto-detected tracks
on top of the ones the script selected.

### What are `.ass` files?

ASS is Advanced SubStation Alpha, a subtitle format with styling. The script
creates a temporary ASS file for the secondary subtitle track so it can have a
different color, position, and font size. These temporary files are not the
authoritative subtitle yields.

## Known Limitations

- The OpenAI translation path is one request by design. Very long videos may
  exceed context limits or become expensive.
- `--language auto` does not trigger automatic English-for-Dutch translation.
- Existing `.en.srt` files are treated as ready regardless of whether they were
  produced by Whisper or OpenAI. Delete them or use `--force` to regenerate.
- There is no metadata sidecar recording the exact model, prompt, or compaction
  settings used for each yield.
- SRT parsing is intentionally pragmatic, not a full subtitle spec
  implementation.
- Compaction heuristics are tuned for readability, not linguistic perfection.
- The script is Windows-first. Some paths and executable names assume Windows.
- `--force` is broad: for URL inputs it redownloads the video too.

## Possible Future Improvements

These are intentionally not implemented yet:

- Add a metadata JSON file per video recording source URL, video ID, models,
  compaction settings, OpenAI model, and generation timestamps.
- Add a `--force-english` or `--regenerate-english` option that avoids video
  redownload and Whisper reruns.
- Add a `--translate-existing-srt` mode for translating an SRT without touching
  video/audio.
- Add context-safe chunking for very long SRT files while preserving global
  terminology, perhaps with a glossary pass.
- Add retries with exponential backoff for transient OpenAI failures.
- Add an optional OpenAI SDK implementation if API usage grows.
- Add tests around SRT parsing, compaction, archive hydration, and exact cue
  preservation.
- Add provider metadata into generated subtitle comments or adjacent JSON.
- Add better language detection handoff so `--language auto` can still trigger
  English translation when Whisper detects Dutch.

## Minimal Mental Model

If you remember only one thing, remember this:

```text
Downloaded video is the durable media yield.
Primary compacted SRT is the timing authority.
OpenAI translates text only.
The script renders English onto the primary compacted SRT timings.
Sidecars are for mpv; archives are for durable reuse.
Second runs should be cheap.
```
