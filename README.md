# yt-whisper-subs

`yt_whisper_subs.py` is the launcher for a Windows-oriented subtitle pipeline
for YouTube videos and local video files. The implementation lives in the
`yt_whisper_subs/` package. It downloads a lossy compressed video when given a
URL, extracts a small lossy audio track, runs local OpenAI Whisper to create
primary language subtitles, optionally translates Dutch subtitles to English
with the OpenAI Responses API, can create bilingual topic chapters from the
finished timestamped transcript, and optionally launches `mpv` with dual
subtitles and chapter navigation.

`yt_whisper_library.pyw` is the native Windows desktop companion. It presents
downloaded and remote channel videos in a searchable library, persists YouTube
metadata, tracks channel uploads, optionally runs the existing subtitle
pipeline for new videos, and opens downloaded entries through the same `mpv`
dual-subtitle playback implementation. It is a PySide6 desktop application,
not a web UI and not a local web server.

This README is intentionally extensive. It is meant both as user documentation
and as a design handoff for future Codex sessions that need to modify the
script without rediscovering all of the local decisions.

## Current Intent

The script is optimized for this workflow:

1. Run it on a Dutch YouTube video.
2. Keep a local lossy video file under `~/Videos/yt-whisper-subs/videos`,
   named from the YouTube video ID.
3. Generate Dutch subtitles locally with Whisper.
4. Compact the Dutch subtitle cues so fragmented speech becomes easier to read,
   then extend cue end times slightly into following silence.
5. Send the compacted and gap-extended Dutch cue texts to the OpenAI API as
   bounded indexed JSON chunks whose cue order is dispersed inside each batch.
6. Receive natural English translations with the exact same cue count and time
   markings as the compacted and gap-extended Dutch SRT.
7. Store subtitle archives under `~/Videos/yt-whisper-subs/subtitles`.
8. Store the `.srt` sidecars beside the video so `mpv` can discover them.
9. When requested, generate durable primary-language/English topic chapters
   whose boundaries are mapped locally to real subtitle timestamps.
10. Write a timestamped run log under `~/Videos/yt-whisper-subs/logs`.
11. Re-run cheaply: if the video and requested subtitles/chapters already
    exist, skip download, audio extraction, CUDA checks, Whisper, and OpenAI,
    then just open `mpv`.

The script can also be used on a local video file, can skip playback, can avoid
English translation, and can fall back to Whisper's built-in audio translation.

The companion library is optimized for a second workflow:

1. Scan every existing ID-named or old title-with-ID video into a durable SQLite
   catalog.
2. Backfill old download titles, source channels, upload dates, duration, view
   count, description, and thumbnails from YouTube when metadata is missing.
3. Subscribe to YouTube channel handles or URLs and display a bounded recent
   history of their long-form videos and streams plus every local download.
4. Pin the most relevant subscriptions into a dedicated quick-access shelf.
5. Check channels every four hours while the app is open or in the system tray.
6. Optionally run the unchanged one-video subtitle pipeline for videos first
   discovered after a channel's initial baseline check.
7. Double-click downloaded videos to open the same English-first dual-subtitle
   `mpv` view used by the CLI.
8. Track furthest watched position and confirmed completion for library-launched
   playback without changing the user's mpv configuration.
9. Generate bilingual chapters for new downloads, show them in the selected-video
   inspector, and double-click any chapter to open or seek mpv at that moment.
10. Cancel the active download/transcription/translation pipeline without stopping
   playback or discarding channel work already waiting in the serial queue.
11. Pause and resume the active pipeline process tree without discarding its
    in-memory model state.
12. Recover a pipeline left by a system crash from durable stage checkpoints and
    reuse every complete or partial yield when the user clicks Resume.
13. Detect missing or corrupt subtitle yields, show them as Issues, and offer a
    one-click Repair action instead of claiming that the pipeline is complete.

## Important Defaults

These defaults are hard-coded near the top of the script:

| Area | Default |
| --- | --- |
| Output root | `~/Videos/yt-whisper-subs` |
| Managed virtual environment | `.venv` beside the script |
| Whisper language | `nl` |
| Primary Whisper model | `turbo` |
| English translation provider | `openai` |
| OpenAI translation model | `gpt-5-mini` |
| OpenAI reasoning effort | `low` |
| OpenAI timeout | `900` seconds |
| OpenAI transient retries | `3` |
| OpenAI translation chunk size | `120` cues |
| OpenAI translation context | `3` neighboring cues |
| AI chapter density | at least `1` chapter per `4` minutes; 15 for 60 minutes |
| CLI chapter generation | opt-in with `--chapters`; library downloads enable it |
| Subprocess silence heartbeat | `60` seconds |
| OpenAI env file | `.env` beside the script |
| Run logs | timestamped under `~/Videos/yt-whisper-subs/logs` |
| Device | `cuda` |
| Python version for uv venv | `3.14` |
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
| Primary font scale | `0.45` |
| Library channel check interval | `4` hours |
| Recent channel history | `50` entries per Videos/Streams section; Shorts ignored |
| Adaptive channel scan ceiling | `500` entries per section when bridging an offline gap |
| Earliest publication date | optional; disabled until configured |
| Library metadata hydration pace | at most `1` video lookup per minute |
| Failed metadata lookup cooldown | `24` hours |
| Metadata queue pause after failure | `15` minutes |
| Library close behavior | keep running in the system tray |
| Start with Windows | disabled; optional per-user quiet tray launch |
| Library activity trace | hidden by default; last `5,000` lines retained per session |
| Library pipeline display | segmented per-video phase bar with live stage wording |
| Library watched-progress sample | every `5` seconds during mpv playback |
| Library 100% watched rule | confirmed mpv end-of-file event only |
| Library smart view | remembered across launches; search and channel remain independent |
| Channel quick access | persistent starred Pinned shelf; right-click or `Alt+P` |
| Library column layout | resizable, reorderable, and remembered across launches |
| Library execution queue | one heavy-work lane; playback launches independently |
| Library cancellation | visible Cancel button during active work; `Ctrl+Shift+X` |
| Library pause/resume | process-tree suspension; `Ctrl+Shift+P` |
| Library crash recovery | durable stage/overall-progress checkpoint; one-click Resume |
| Library yield removal | confirmed exact per-video manifest; individual non-recursive unlinks |
| Channel auto-download baseline | future discoveries only; never the initial backlog |
| Subtitle compaction mode | `english` |
| Compaction gap | `0.9` seconds |
| Compaction max merged duration | `9.0` seconds |
| Compaction max merged chars | `180` |
| Compaction max characters per second | `25.0` |
| Wrapped subtitle line width | `50` |
| Subtitle gap extension | `5` seconds |

One subtle default matters: when OpenAI English translation is enabled, the
primary Dutch SRT is compacted before translation even though `--compact-subs`
defaults to `english`. It is also gap-extended before translation. This exists
so the OpenAI-translated English file has the same timestamps and cue count as
the Dutch file that the user actually wants to read.

If `--no-compact-subs` is used, the compaction override is disabled. In that
case OpenAI receives cue text from the un-compacted primary SRT, still
gap-extended unless `--subtitle-gap-extension 0` is used.

## Quick Start

Open the native desktop library by double-clicking `yt_whisper_library.pyw`, or
run it from PowerShell:

```powershell
python .\yt_whisper_library.pyw
```

The launcher creates or reuses `.venv`, installs `yt-dlp[default]` and
`PySide6-Essentials` and `psutil` when needed, and relaunches with `pythonw.exe`. The first
start scans existing downloads immediately. An overdue channel check and the
gently paced metadata queue then run in the background without freezing the
GUI. On Windows, background child operations run without opening or flashing a
terminal window. mpv remains a normal visible application with a taskbar icon
and Alt+Tab entry. Phase progress remains visible in the table and status bar,
while exact tool output and errors remain available in the activity trace and
run logs.

When this launcher is started from PowerShell, WezTerm, or another terminal, the
outer process remains attached until the managed GUI exits. Pressing **Ctrl+C**
now terminates that managed `pythonw.exe` process and immediately restores the
prompt with the conventional interrupted exit code `130`. Closing the window
can still hide it in the tray by design; use **Library → Quit** for a normal
zero-code exit.

Choose **Library → Settings**, enable **Start quietly in the system tray when I
sign in**, and click Save to start the library with the current Windows user.
This writes one exact value under the current user's standard Windows Run key,
needs no administrator rights, preserves the selected output root, and uses
`pythonw.exe`, so login does not flash a terminal. Disabling the checkbox removes
only that value. Manual launches still open the main window normally.

Use the same non-default output root as the CLI:

```powershell
python .\yt_whisper_library.pyw --out-dir "D:\Videos\yt-whisper-subs"
```

The existing one-video CLI remains:

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

Regenerate only the English subtitle yield from the existing Dutch SRT:

```powershell
python .\yt_whisper_subs.py --force-english "https://www.youtube.com/watch?v=VIDEO_ID"
```

Generate chapters while otherwise reusing existing yields:

```powershell
python .\yt_whisper_subs.py --chapters "https://www.youtube.com/watch?v=VIDEO_ID"
```

Regenerate only an existing chapter plan:

```powershell
python .\yt_whisper_subs.py --force-chapters "https://www.youtube.com/watch?v=VIDEO_ID"
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
    youtube_id.mkv
    youtube_id.srt
    youtube_id.en.srt
  audio\
    youtube_id.opus
  logs\
    youtube_id-YYYYMMDD-HHMMSS.log
  metadata\
    youtube_id.info.json
  subtitles\
    youtube_id.srt
    youtube_id.en.srt
    youtube_id.uncompact.srt
    youtube_id.en.uncompact.srt
  chapters\
    youtube_id.chapters.json
    youtube_id.chapters.ffmetadata
  library\
    catalog.sqlite3
```

The exact `.uncompact.*` files appear only when compaction changed an existing
subtitle file and a backup did not already exist.

For YouTube URLs, filenames are based on the video ID instead of the title. This
avoids title punctuation, Unicode, path length, and title-change problems. If an
older title-based yield such as `Video title [youtube_id].mkv` exists, the
script migrates it to `youtube_id.mkv` when the ID-named target does not already
exist. Local video files still use the local file stem.

The script writes subtitles in two places:

1. Sidecar subtitles beside the video, for `mpv` auto-detection:
   `videos\youtube_id.srt` and `videos\youtube_id.en.srt`.
2. Archive subtitles under `subtitles\`, so subtitle yields survive even if
   sidecars are moved or missing.

This duplication is intentional. Sidecars are for playback ergonomics. The
archive directory is for durable yield tracking.

Chapter JSON is the authoritative bilingual plan, including exact millisecond
boundaries, generation time, language, model, and video duration. The adjacent
FFmetadata file is a derived mpv input. It can be reconstructed from JSON and
does not modify or remux the downloaded video.

Every new YouTube download also writes the extractor's cleaned `.info.json`
under `metadata\`, embeds normal media metadata into the downloaded container,
and embeds the info JSON as an MKV attachment when the container supports it.
The sidecar is the catalog's durable source for title, channel, upload time,
duration, views, description, thumbnail URL, source URL, and other yt-dlp
fields. The library database is a query cache and subscription store; deleting
it does not delete any videos or metadata sidecars, and the next library start
can rebuild local download state.

Privacy note: yt-dlp warns that info JSON can contain personal information and
temporary extractor URLs. Keep the `metadata\` directory private; the project
does not upload these sidecars or put them in the repository.

The video file is intentionally not lossless. The default yt-dlp format selector
keeps YouTube's already-compressed audio/video streams and merges them into an
`mkv` container. Audio extracted for Whisper is also lossy, defaulting to Opus at
48 kbps, mono, 16 kHz.

## The End-To-End Pipeline

The main flow is:

```text
parse args
start timestamped run log
resolve source
  if URL:
    find exact cached video by YouTube ID unless --force
    otherwise download with yt-dlp
    canonicalize URL yields to video-id filenames
  if local file:
    resolve local path
derive yield paths from video ID for URLs or video stem for local files
hydrate missing sidecar/archive subtitle pairs
compact existing subtitles when needed
extend subtitle cues into following silence when needed
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
  generate bilingual chapters if requested and needed
  optionally delete audio
  optionally play in mpv
```

The most important engineering property is parsimony: the script should not
consume bandwidth, GPU time, CPU time, or OpenAI API calls when the requested
yields are already present.

## Yield Reuse And `--force`

On normal runs, the script reuses existing yields.

For URL input, the video cache lookup is intentionally exact. The script extracts
the YouTube video ID from supported URL shapes and looks first for a final media
file whose stem is exactly `video_id`. It also recognizes the older
`Video title [video_id]` cache form so existing downloads can be migrated. It
does not pick "the newest downloaded video" as a fallback. That earlier behavior
was risky because a new URL could
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

`--force-english` is the supported shortcut for that common case. It keeps the
cached video, audio, and primary Dutch subtitles, then regenerates only the
Dutch-to-English subtitle yield when English generation is enabled for the run.

`--force-chapters` similarly implies `--chapters` and regenerates only the
chapter plan from reusable subtitles. It does not redownload the video, and it
does not rerun Whisper when primary subtitles already exist. Direct CLI chapter
generation remains opt-in so established one-video CLI interaction and API cost
do not change unexpectedly; native-library downloads explicitly enable it.

## Dependency Management

The script expects to run on Windows. It uses:

- `uv`
- Python 3.14 in a script-local `.venv`
- `yt-dlp`
- `yt-dlp-ejs` through yt-dlp's `default` package extra
- `openai-whisper`
- `torch`
- `ffmpeg`
- `mpv`
- `PySide6-Essentials` for the native library GUI
- `psutil` for pausing and resuming the complete owned pipeline process tree

Python dependencies are installed into `.venv` beside the script. This choice
was made because the script is intended to be portable as a single project
folder and should not depend on whichever Python packages happen to be installed
globally.

The `.pyw` library launcher bootstraps only `yt-dlp[default]`,
`PySide6-Essentials`, and `psutil`, keeping a first GUI start much smaller than a Whisper/CUDA
installation. If the user downloads a remote catalog entry, the library invokes
`yt_whisper_subs.py --no-play`; that existing pipeline then installs or validates
Whisper and Torch exactly as a direct CLI run would.

The repository also includes a tracked `.python-version` file set to `3.14` so
local Python tooling and the script-managed environment share the same project
default.

The script creates or updates `.venv` when:

- `--install-python-deps` is passed, or
- the expected `.venv\Scripts\python.exe` is missing, or
- the existing `.venv` uses a different Python major/minor version than
  `--python-version`.

It installs:

```text
wheel
setuptools
yt-dlp[default]
openai-whisper
torch
```

When `--device cuda` is used, Torch is installed from the configured CUDA index:

```text
https://download.pytorch.org/whl/cu128
```

The Torch install is forced to reinstall the `torch` package in CUDA mode. This
matters because an existing CPU-only Torch wheel can otherwise satisfy the plain
`torch` requirement and survive a normal upgrade, leaving PyTorch unable to see
the NVIDIA GPU.

The script does not install the OpenAI Python SDK. The OpenAI Responses API call
uses the Python standard library (`urllib`). That was a deliberate dependency
decision: the OpenAI translation path should not require adding and maintaining
another Python package in the Whisper environment.

`ffmpeg` and `mpv` are external executables. With `--install-tools`, the script
attempts to install/update `uv`, `ffmpeg`, and `mpv` via Scoop. If Scoop is not
available, install those tools manually.

Because YouTube extraction changes frequently, the managed runtime checks for a
new yt-dlp release at most once every seven days. Update failure is non-fatal
when an older installation is already usable. The `default` package extra
includes yt-dlp's EJS solver component; the script automatically enables Deno
or Node from `PATH` (in that order) for YouTube's JavaScript challenges.

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
  --js-runtimes <installed deno-or-node>
  --no-playlist
  --windows-filenames
  --part
  --continue
  --progress
  --progress-delta <seconds>
  -f <format selector>
  --merge-output-format <container>
  --write-info-json
  --embed-metadata
  --embed-info-json
  --print after_move:filepath
  -o "%(id)s.%(ext)s"
  -o "infojson:<metadata directory>/%(id)s.%(ext)s"
```

Design notes:

- `--no-playlist` avoids accidentally downloading an entire playlist.
- `--windows-filenames` prevents filenames that are awkward on Windows.
- `--part` and `--continue` keep yt-dlp's normal resume behavior explicit, so
  interrupted downloads can continue instead of fetching the same bytes again.
- `--progress-delta` defaults to `1`, so progress output is visible but not too
  noisy.
- `--write-info-json` preserves the full cleaned extractor result outside the
  media container, where the catalog can ingest it cheaply.
- `--embed-metadata` adds standard title, uploader, date, description, and
  related tags supported by the selected container.
- `--embed-info-json` also attaches the complete JSON to MKV when supported.
- The typed `infojson:` output template keeps metadata under `metadata\`
  instead of cluttering `videos\`. A typed output template is used instead of
  `--paths`, because yt-dlp ignores `--paths` when the main media template is an
  absolute path.
- `--print after_move:filepath` gives the script the final filename.
- The output filename is the extractor/video ID only, which avoids title-derived
  filesystem problems and makes exact cache lookup direct.

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

## Native YouTube Library

The desktop library is a native PySide6/Qt application with a dark Windows UI.
It contains:

- counted smart views for All, On device, Available, Unwatched, Continue,
  Watched, and Issues;
- a counted, starred **Pinned** shelf above the regular channel list;
- instant title, channel, and YouTube-ID search that composes with smart views;
- sortable, resizable, and reorderable pipeline, watched, title, channel,
  published, downloaded, duration, size, and view-count columns;
- a selected-video inspector for description, local path, errors, and a
  scrollable bilingual chapter list with exact jump points;
- manual Check, Download/Repair/Resume, Pause, Cancel, Play, Open on YouTube,
  and exact-yield removal actions;
- configurable browser cookies, check interval, and current-user Windows login start;
- an optional timestamped activity trace for live pipeline and subprocess output;
- a system tray so periodic checks continue when the main window is closed.

The native title bar includes the current application version. That value comes
directly from `yt_whisper_subs.__version__`, the single source used by the GUI;
it is not duplicated in the window code.

### Pinned Channels

Channels that matter most can be moved to the starred **Pinned** shelf at the
top of the sidebar. Select a channel and use **Channel → Pin selected channel**,
press **Alt+P**, or right-click the row and choose **Pin to quick access**.
Pinned channels are shown only in that shelf, so they are not duplicated in the
long regular list. The most recently pinned channel appears first; unpinned
channels remain alphabetical. Pin state is durable SQLite subscription data and
survives restarts. The same context menu also exposes automatic-download and
stop-tracking controls.

### Responsiveness And Derived-State Safety

Channel selection no longer queries SQLite or resets and resorts thousands of
table rows. The complete current catalog lives in one Qt source model and the
selected channel is applied by its proxy as an in-memory scope. Stable YouTube
IDs have an O(1) row index for pipeline/playback updates and selection restore;
facet counts classify each matching row once instead of evaluating every view
strategy repeatedly. Cell painting formats only the requested field and never
opens or parses a file. Pipeline progress updates do not recalculate watched
facets, while playback updates do so only when their classification can change.

SQLite, media, SRT, and chapter files remain the sources of truth. The only
cross-refresh cache contains derived SRT validity and parsed chapter data. Every
entry carries the authoritative file path, size, and nanosecond modification
time, so changing, replacing, deleting, or creating a sidecar invalidates that
entry automatically. Catalog mutations still trigger a fresh SQLite snapshot.
WAL mode is configured once when the database opens instead of being renegotiated
for every short read connection.

Background subprocesses launched from the desktop application—including
dependency setup, channel discovery, downloads, ffmpeg, and Whisper—use
Windows' hidden, no-console process mode. mpv uses a distinct visible-application
policy: its console is suppressed, but its video window, taskbar icon, and
Alt+Tab entry remain visible. This keeps computational work visually quiet
without hiding the application the user explicitly asked to open.

Choose **View → Activity trace** or press **Ctrl+Shift+L** to open the dockable
trace panel. It timestamps download progress, command lines, ffmpeg and Whisper
output, OpenAI translation/chapter stages and token usage, channel checks, task
completion, and failures. The newest 5,000 lines are retained in memory even
while the panel is hidden; **Copy all** and **Clear** are available in the
panel. Visibility is remembered across launches. Durable per-video pipeline
logs continue to be written under `logs\`.

### Smart Views And Search

The compact **Show** shelf above the table answers common library questions
without opening dialogs or combining contradictory dropdowns:

- **All** shows the complete current library or selected channel.
- **On device** shows every downloaded, playable video.
- **Available** shows tracked videos that have not been downloaded.
- **Unwatched** shows downloaded videos with no observed playback position.
- **Continue** shows videos that were started but have not reached confirmed
  end-of-file.
- **Watched** shows videos with confirmed mpv completion.
- **Issues** isolates videos whose latest download or subtitle processing
  attempt failed, or whose required Dutch/English SRT files are missing,
  unreadable, empty, or contain no valid cues.

The number on every chip is calculated from the current channel and search
scope, but independently of the selected chip. This makes the shelf a small
faceted overview as well as navigation: while searching one channel, it still
shows how many matching results are downloaded, unfinished, or in need of
attention. Empty inactive views become unavailable rather than leading to a
surprising blank table.

These contextual counts replace the older four-card Videos, Downloaded,
Available, and Channels summary row, reclaiming vertical space for the catalog.
The sidebar shows separate counts for the **Pinned** and regular channel shelves.

Only one smart view can be active, so availability and viewing-state filters
cannot contradict each other. Channel selection remains in the sidebar and
free-text search remains in the top bar; all three scopes compose. **Clear** or
**Ctrl+Shift+F** resets search and the smart view without leaving the selected
channel. **Ctrl+F** focuses search. The chosen smart view is remembered across
application launches.

Every table header divider can be dragged to resize its column, and every
header can be dragged left or right to change the visual order. Qt's native
header state is saved in the library database shortly after each change and
again when the app exits, then restored on the next launch. Choose **View →
Reset column layout** to return to the shipped order and readable default
widths. Sorting remains available by clicking any header.

### Pipeline Progress And Diagnostics

The first table column is both a status display and an eMule/BitTorrent-style
segmented progress bar. Its colored sections represent preparation, video
download, audio extraction, speech-to-text, English translation, AI chapter
planning, and final file work. The label above the bar changes through
**Queued**, **Preparing**,
**Downloading**, **Extracting audio**, **Speech-to-text**, **Translating**,
**Creating chapters**, **Finalizing**, and **Ready to play**. Available, live, upcoming, and failed
rows use the same column, so status is not split across unrelated UI elements.

Percentages are sourced where the underlying tool exposes meaningful progress:
yt-dlp supplies download percentage, Whisper's frame progress supplies
speech-to-text or audio-translation percentage, and OpenAI subtitle translation
reports completed chunks. Short stages without a trustworthy percentage show
their active segment and label without inventing one. The right-hand percentage
is a weighted indication of the whole pipeline, not an ETA. Skipped or reused
stages advance immediately. A failed run preserves the reached position in red
instead of falling back to zero; hovering the cell shows its diagnostic label.

The status bar mirrors the current fine-grained stage without being overwritten
by noisy subprocess lines. Those lines still flow, timestamped, into the
optional activity trace. This gives the normal view a calm answer to “what is
it doing now?” and the trace a precise answer to “what exactly happened?”.

The GUI child process opts into these structured events with a private
environment variable. A direct `yt_whisper_subs.py` run does not enable that
protocol, so its established command-line interaction and output remain
unchanged.

### Pause, Resume, And Crash Recovery

While a video pipeline is active, the toolbar exposes **Pause** and **Cancel**.
Pause (or **Ctrl+Shift+P**) suspends the GUI-owned CLI process and its full child
tree, including yt-dlp, ffmpeg, Whisper, or the currently active Python request.
The amber bar retains the exact reached overall position and the button changes
to **Resume**. Playback remains independent and can still be started while the
pipeline is paused. Cancel remains available from the paused state.

Pipeline kind, stage, label, and overall fraction are checkpointed in SQLite at
phase changes and whole percentage points. A normal completion, reported error,
or explicit cancellation removes that recovery marker. If Windows, the machine,
or the GUI process terminates without reaching a normal outcome, the next launch
marks the row **Interrupted** and offers **Resume** without a confirmation dialog.
The resumed operation uses the normal idempotent pipeline: yt-dlp continues its
`.part` file, completed media/audio/subtitles are reused, OpenAI translation can
reuse its chunk checkpoint, and only unfinished work reruns. Whisper itself does
not expose a serializable in-memory CUDA checkpoint, so a crash during that one
stage restarts speech-to-text while preserving all earlier stages.

### Watched Progress And Completion

The **Watched** column is a compact progress bar for downloaded videos opened
from the desktop library. While mpv is running, the app samples its playback
position and duration every five seconds. SQLite retains the furthest observed
position, so rewinding or replaying a video never moves the bar backward.

Position-derived progress is capped at 99%. The app writes a separate
completion timestamp only when mpv emits `end-file` with reason `eof`; that
event alone renders the green **✓ 100%** state. Closing the window, stopping
playback, or an mpv error flushes the last position without marking the video
complete. Seeking directly to the end counts as completion because mpv reports
EOF; the feature models “reached the end,” not second-by-second viewing
coverage.

Tracking uses a unique local JSON IPC named pipe supplied on that single mpv
command line. It does not edit `mpv.conf`, pass `--no-config`, or interfere with
`save-position-on-quit`. The endpoint disappears with the player process.
Existing mpv watch-later files are not bulk-imported because their hashed
filenames do not reliably distinguish never-watched from completed videos.
Opening a resumable video from the library immediately teaches the catalog its
resumed position.

### Existing Downloads And Metadata Backfill

On startup the library scans `videos\` by exact YouTube ID. Both
`youtube_id.mkv` and old `Title [youtube_id].mkv` forms are recognized. A matching
`metadata\youtube_id.info.json` is ingested without network access. Older files
without sidecars appear immediately with their best offline title and download
time, then receive full remote metadata through a separate paced queue. The app
attempts at most one video lookup per minute, pauses the queue while a channel
check or subtitle-pipeline task is active, and waits 24 hours before retrying a
failed lookup. Any failure also pauses the entire queue for 15 minutes, which
keeps a broad YouTube refusal from cascading across the backlog. Untouched rows
always run before retries, so one unavailable video cannot block the backlog.
This avoids request bursts while steadily restoring missing publication times.
The status-bar footer shows the remaining queue; the activity trace records
each saved, deferred, and queue-paused lookup.

Filesystem creation time is used as the best available historical download
time. It is distinct from YouTube publication time and is shown in a separate
column.

### Channel Subscription Checks

Track a channel with an `@handle` or a `/channel/`, `/c/`, or `/user/` URL. The
service checks the channel's Videos and Streams tabs with yt-dlp's flat
playlist mode and deduplicates them by video ID. A normal check requests only
the newest 50 entries from each section. If that slice contains no already-known
ID, the request expands geometrically up to 500 entries so a long offline gap is
less likely to hide uploads. YouTube's Atom feed supplies exact publication
timestamps for the latest entries. Remaining rows without a flat timestamp are
progressively hydrated with full per-video metadata.

Shorts are deliberately excluded from subscriptions. A channel without a
Streams tab is treated as having an empty Streams section, not as a failed
refresh. Genuine extraction or network failures remain partial refreshes and
preserve old rows rather than risking destructive synchronization.

Tracking is visible immediately: the app first persists and selects a compact
handle-based placeholder in the sidebar, then queues resolution of the official
channel title and initial history. You can therefore enter several channels
while a download, Whisper transcription, translation, or earlier channel lookup
is running; every placeholder appears at once. The actual work uses one shared
single-worker queue and starts later in entry order, so expensive operations do
not compete for CPU, GPU, disk, or network resources. The activity trace marks
waiting entries as **Channel queued** and records when each really starts.
Metadata hydration is low priority, schedules only one item at a time, and does
not add its entire backlog to the queue.

Configure both controls under **Library → Settings**. **Recent history** is a
per-section count, so the default retains at most roughly 100 deduplicated
remote entries per channel. **Published since** is optional; choose a date such
as `2026-04-01` to discard older remote entries. Saving a cutoff immediately
removes rows already known to be older. Each later successful channel refresh
also removes remote-only rows outside its complete bounded snapshot, including
old rows whose date was never available. A partial refresh never prunes.
Downloaded videos, subtitle and chapter files, metadata, and playback history
are exempt from both retention controls.

The default interval is four hours and can be changed from **Library →
Settings**. The schedule is persisted in SQLite, so reopening the app performs
an overdue check. Checks run only while the application process is open; closing
the window keeps it in the system tray by default. Choose **Library → Quit** to
stop checks completely.

Channel checks only discover catalog entries; they never launch a burst of
per-video metadata requests. Metadata enrichment has its own one-item timer and
runs only while the application process is open.

Browser cookies can be configured in the same dialog using values such as
`firefox`, `chrome`, or `edge`. They are forwarded to both channel discovery and
the existing download pipeline.

### Safe Automatic Downloads

Automatic download is a per-channel policy. The first successful channel check
is always a baseline: it makes the existing back catalog browseable but never
downloads it. If automatic download is enabled, only video IDs absent from the
catalog and discovered by later checks are candidates. Active and upcoming live
streams are excluded.

An automatic or manual library download is not a second media implementation.
It executes the existing CLI with the video's canonical URL, the shared output
root, and `--no-play`. The normal video download, Whisper transcription, OpenAI
translation, bilingual chapter generation, subtitle compaction, archival,
metadata, logging, reuse, and error
behavior therefore remain single-sourced.

Double-clicking a remote-only row starts that pipeline immediately; there is no
extra confirmation dialog after the deliberate double-click. During active
heavy work the header exposes **Cancel**; **Video → Cancel current operation**
and **Ctrl+Shift+X** are equivalent. Cancellation terminates the isolated child
process tree, so yt-dlp, ffmpeg, Whisper, or an in-flight API-stage parent cannot
be orphaned. It is rendered as an amber **Cancelled** state rather than a red
operational failure. Work already queued behind it remains queued.

A downloaded media file is not sufficient proof of pipeline completion. Dutch
and English sidecar/archive pairs must contain parseable SRT cues, and stage
completion is emitted only after its durable outputs validate. Damaged or
incomplete rows expose **Repair**, which reuses the media and valid prior yields
while rerunning only what is missing.

**Video → Remove download and yields…** or **Shift+Delete** first shows a
confirmation with the exact file manifest. Removal enumerates only immediate
files in the managed `videos`, `audio`, `metadata`, `subtitles`, `chapters`, and
`logs` folders whose name has the selected 11-character ID plus its required
delimiter. It rejects paths outside those folders and unlinks one explicit path
at a time—never a recursive command or wildcard. A tracked catalog row remains
Available afterward, so it can be downloaded again; playback history remains
separate.

### Shared Playback

Double-clicking a downloaded row or choosing Play invokes
`playback.play_video` with the same English-first sidecar discovery, colors,
positions, primary font scale, secondary ASS conversion, and mpv options as the
CLI. Double-clicking a remote-only row starts its download directly. mpv runs in a
background worker so the Qt window remains responsive while playback is open;
mpv itself remains a normal visible and switchable Windows application.
Playback uses a dedicated worker lane, so a downloaded video opens immediately
while another video is downloading, running Whisper, translating, or generating
chapters. Those expensive operations remain serialized one at a time. Playback
progress updates its table row without replacing the active pipeline stage in
the status bar.
Library-launched playback additionally enables one ephemeral IPC endpoint so
the Watched bar updates live. Direct CLI playback keeps its established command
shape and terminal interaction unchanged.
When only a Dutch SRT exists, that sole track is explicitly enabled as `sid=1`
and receives the normal primary styling, overriding an mpv subtitles-off
preference for that launch. Invalid SRT files are not passed to mpv.

When a chapter plan exists, both GUI and CLI playback add its derived
`--chapters-file` only to that mpv launch. The GUI inspector shows the primary
title and English title together. If that same library video is already playing,
double-clicking a chapter sends an exact absolute seek over its existing
launch-scoped IPC connection. A click made while the named pipe is still opening
is queued safely. If no matching player is active, the normal background action
opens mpv with `--start` at the chapter's trusted local timestamp. A player for a
different video is never moved. None of this edits mpv config, and mpv's normal
chapter keys/menu can navigate the same plan.

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

Python subprocesses inherit `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` from the
wrapper. This keeps Whisper's live transcript output from failing on Windows
code pages when recognized speech contains characters outside the local console
encoding.

Long-running tools such as Whisper, yt-dlp, and ffmpeg are streamed through the
wrapper. If a child process stays alive but produces no output for 60 seconds,
the wrapper prints a heartbeat line:

```text
[subprocess still running; no output for 60s]
```

This line is diagnostic only. It does not kill the child process. It exists so a
Whisper model load, network stall, or unusually quiet transcription phase is
visible in both the terminal and the timestamped run log. Playback through `mpv`
does not use this heartbeat, because a quiet player process is normal while a
video is open.

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
2. The complete compacted and gap-extended Dutch SRT is parsed into cues and
   split into indexed cue-text JSON chunks when it is longer than the configured
   OpenAI chunk size.
3. The prompt asks for natural idiomatic English.
4. The prompt forbids merging, splitting, adding, or omitting cues.
5. Each source cue is sent as an object with an explicit `index` and `text`, and
   the prompt tells the model to translate only that cue's text for the matching
   output index. This is important for sentence fragments that span multiple
   cues: the model must not complete a fragment with neighboring cue text.
6. Cues inside each chunk are deliberately sent in a non-chronological
   even-then-odd order while keeping their 1-based indexes. This reduces the
   model's tendency to complete one subtitle fragment with the next cue and
   shift subsequent translations by one.
7. Each chunk includes a small amount of neighboring text as context, but the
   model is asked to translate only the current chunk.
8. Completed chunks are saved to a temporary `.en.partial.json` checkpoint next
   to the English sidecar, so an interrupted or failed run can resume without
   paying for already translated chunks again.
9. If a chunk response is valid JSON but incomplete, for example 91 usable
   translations for 92 source cues, the script keeps the usable indexed
   translations and sends a small repair request for only the missing cues.
10. The API is asked for strict JSON:

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

11. The script validates that:
   - the JSON parses,
   - `translations` is a list,
   - every expected 1-based cue index has exactly one usable translation,
   - each text value is a non-empty string.
12. The script renders a new English SRT by pairing each translated text with the
   original source cue start and end timestamps.

This gives the English file the same cue count and cue timings as the compacted
and gap-extended Dutch file. That property is the main reason this mode exists.

The OpenAI request uses:

```text
POST https://api.openai.com/v1/responses
```

with:

```json
{
  "model": "gpt-5-mini",
  "input": "...prompt and current indexed cue JSON chunk...",
  "reasoning": {
    "effort": "low"
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

Transient OpenAI request failures are retried before the run fails. This covers
dropped connections such as Windows `WinError 10054`, timeouts, and temporary
HTTP responses such as 429 or 5xx. Authentication, model, schema, and validation
errors still fail immediately or after the API returns a final non-retryable
response. Incomplete but parseable translation chunks are handled separately by
the missing-cue repair request described above.

Set `--openai-translation-chunk-cues 0` to use one full cue-list request. The
chunked default is more resilient for longer videos and avoids connection
resets seen with large JSON responses.

The exact default model and effort are configurable:

```powershell
python .\yt_whisper_subs.py `
  --openai-translation-model gpt-5-mini `
  --openai-reasoning-effort low `
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

Privacy note: this mode sends the compacted subtitle text to OpenAI. That is
deliberate for translation quality, but it is still a cloud API call containing
the transcript.

Cost note: long videos may require multiple OpenAI requests because the script
now chunks translation work by cue count. This avoids oversized responses but
can still be expensive for long transcripts. A malformed or incomplete chunk may
add a small repair request for only the missing cues.

Cost controls are intentionally conservative by default:

- `gpt-5-mini` is used instead of frontier models such as `gpt-5.5`.
- reasoning effort defaults to `low`, which is accepted by the default
  `gpt-5-mini` model and gives translation a little more room than `minimal`.
- only three neighboring cues are sent as context around each chunk.
- token usage is printed after each successful OpenAI response when the API
  returns usage metadata.

Raise the model, reasoning effort, or context window only when quality demands
it for a specific source.

### Bilingual AI Chapters

Chapter planning is intentionally a fresh Responses API request rather than a
continuation of a translation conversation. Translation is checkpointed in
independent cue chunks and has a strict cue-by-cue objective; chaptering is a
whole-video editorial task. Keeping the requests stateless (`store: false`, no
conversation or previous-response ID) prevents partial translation retries from
leaking into chapter structure and makes regeneration deterministic and
independently retryable.

The planner does not trust the model to invent timestamps. It:

1. Reads the actual container duration with hidden `ffprobe`, plus the final
   primary SRT and, when available, its exactly aligned English SRT. Transcript
   duration is the fallback for containers that do not expose a duration.
2. Coalesces cues into at most 240 chronological transcript windows, normally
   about 30 seconds each.
3. Requests strict JSON containing only a window index plus a concise title in
   the primary language and in English.
4. Requires the first chapter at the opening window, increasing unique indexes,
   whole-video coverage, and a minimum density of one chapter per four minutes.
   A 60-minute video therefore requires at least 15 chapters.
5. Maps every returned index back to the locally held SRT timestamp and writes
   the authoritative JSON plus a derived mpv FFmetadata sidecar.

If structured output fails local validation, one fresh correction request is
made with the specific validation error. No incomplete plan is persisted.
Chapter creation uses the same OpenAI model, reasoning effort, timeout, retry,
and `.env` settings as translation. It works from the primary transcript even
when no English SRT exists; the planner still produces both title languages.

Native-library downloads request chapters automatically. For an older local
download, select it and use **Generate** in the chapter pane or **Video →
Generate chapters**. The action reuses the local video and subtitles. Use
`--chapters` to opt a direct CLI run in, `--force-chapters` to regenerate only
the plan, and `--chapter-minutes` to adjust minimum density.

Privacy and cost note: chapter generation sends the timestamped transcript text
to OpenAI in one additional request, with at most one validation-repair request.
This is separate from any requests used to translate the subtitles.

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

## Subtitle Compaction And Gap Extension

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
SRT is also compacted first. After gap extension, this lets the translated
English file match the displayed Dutch SRT exactly.

After compaction, subtitle cue timing is smoothed so short no-speech gaps do not
drop all text immediately. By default each cue is extended by up to 5 seconds
into the following gap, capped at the next cue's start time so cues do not
overlap. The final cue is left unchanged because there is no following cue to
cap against. Use `--subtitle-gap-extension 0` to disable this.

For the default OpenAI translation path, this timing extension happens on the
primary SRT before translation. The English SRT is rendered onto those same cue
timings, so original and translated subtitles keep matching cue boundaries. On
reruns, an existing OpenAI-style English SRT with the same cue count is also
realigned to the primary SRT timings before the script decides all yields are
ready.

Compaction writes backups. For a file:

```text
youtube_id.srt
```

the backup is:

```text
youtube_id.uncompact.srt
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

The desktop library inserts a unique
`--input-ipc-server=yt-whisper-subs-<random-id>` option before the video. It is
launch-scoped and does not alter persistent mpv configuration. Direct CLI
playback omits this option.

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
| `--out-dir DIR` | `~/Videos/yt-whisper-subs` | Root for media, subtitles, chapters, metadata, logs, and library state. |
| `--language LANGUAGE` | `nl` | Whisper language code, or `auto`. |
| `--task transcribe|translate` | `transcribe` | Primary Whisper task. |

### Whisper And CUDA

| Option | Default | Meaning |
| --- | --- | --- |
| `--model MODEL` | `turbo` | Primary Whisper model. |
| `--device cuda|cpu` | `cuda` | Whisper/PyTorch device. |
| `--torch-index-url URL` | CUDA 12.8 PyTorch index | Torch install index when using CUDA. |
| `--python-version VERSION` | `3.14` | Python version for the uv-managed `.venv`. |
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
| `--openai-translation-model MODEL` | `gpt-5-mini` | Model shared by OpenAI SRT translation and chapter generation. |
| `--openai-reasoning-effort EFFORT` | `low` | Reasoning effort shared by OpenAI translation and chapters. |
| `--openai-timeout SECONDS` | `900` | API request timeout. |
| `--openai-max-retries INT` | `3` | Retries for transient OpenAI request failures. |
| `--openai-translation-chunk-cues INT` | `120` | Maximum cues per OpenAI translation request; `0` means one full cue-list request. |
| `--openai-translation-context-cues INT` | `3` | Neighboring cues sent as context around each chunk. |
| `--openai-env-file PATH` | `.env` beside script | Env file to load for `OPENAI_API_KEY`. |

OpenAI reasoning effort choices:

```text
none, minimal, low, medium, high, xhigh
```

The default `gpt-5-mini` model accepts `minimal`, `low`, `medium`, and `high`.
The script validates that combination before sending a request, because the API
rejects `none` and `xhigh` for `gpt-5-mini`.

### Chapters

| Option | Default | Meaning |
| --- | --- | --- |
| `--chapters` | off in direct CLI; on for library downloads | Generate durable primary-language/English chapters from finished subtitles. |
| `--force-chapters` | off | Imply `--chapters` and regenerate only the chapter plan while reusing media/subtitles. |
| `--chapter-minutes FLOAT` | `4` | Maximum average minutes per chapter; real topic transitions choose boundaries. |

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
| `--subtitle-gap-extension FLOAT` | `5` | Seconds to extend each cue into following silence, capped by the next cue start; `0` disables it. |

### Execution Control

| Option | Default | Meaning |
| --- | --- | --- |
| `--force` | off | Re-download URL videos and regenerate subtitles. |
| `--force-english` | off | Regenerate only the English subtitle yield from existing primary subtitles when possible. |
| `--force-chapters` | off | Regenerate only chapters from existing subtitles when possible. |
| `--install-tools` | off | Install/update `uv`, `ffmpeg`, and `mpv` via Scoop. |

### Logging

| Option | Default | Meaning |
| --- | --- | --- |
| `--log-file PATH` | `logs\source-YYYYMMDD-HHMMSS.log` | Write the run log to a specific path. |

Every normal run writes a comprehensive UTF-8 log file. For YouTube URLs, the
default log filename starts with the video ID. The log records the command line,
resolved arguments, output root, yield paths, script messages, and subprocess
output from tools such as yt-dlp, ffmpeg, Whisper, and mpv. It does not print
environment variables or the OpenAI API key.

## Software Engineering Decisions

### Modular Package With A Script Launcher

The repository keeps `yt_whisper_subs.py` as the command users run, but the
implementation is split into cohesive modules under `yt_whisper_subs/`. This
keeps the operational surface simple while avoiding a kitchen-drawer source file
that mixes CLI parsing, media handling, SRT transforms, OpenAI translation,
playback, logging, and orchestration.

The split is intentionally responsibility-oriented:

- `cli.py` owns argument parsing.
- `opts.py` owns option-derived policy decisions.
- `cfg.py` is the single source of truth for defaults.
- `proc.py` owns subprocess execution, stdio setup, tool checks, CUDA probing,
  and the script-local `.venv`.
- `runlog.py` owns timestamped run logging.
- `youtube.py` owns YouTube ID extraction, cache lookup, migration, and yt-dlp
  downloads.
- `media.py` owns local video paths and audio extraction.
- `whisper_local.py` owns Whisper execution and model-cache validation.
- `srt.py` owns the subtitle cue model, SRT parsing/rendering, compaction, and
  gap extension.
- `openai_client.py` owns dotenv loading, Responses API calls, retry behavior,
  and response text extraction.
- `openai_translate.py` owns the indexed-cue translation strategy through
  `OpenAISrtTranslator`, `TranslationChunk`, and `TranslationCheckpoint`.
- `subtitle_files.py` owns sidecar/archive repair, compaction backups, timing
  alignment, and pair finalization through `SubtitlePair`.
- `playback.py` owns ASS generation and mpv launch details.
- `pipeline.py` owns end-to-end orchestration, concrete yield paths, and cheap
  reuse through `PipelineRunner`, `YieldDirs`, and `RunYields`.
- `app.py` wires CLI parsing, logging, and pipeline execution.

### Cohesive Runtime Objects

Several modules use small, behavior-bearing value objects instead of passing
long path and state argument lists through orchestration code:

- `subtitle_files.SubtitlePair` keeps the sidecar/archive subtitle lifecycle in
  one place: hydration, archive syncing, compaction backups, gap extension, and
  timing alignment.
- `pipeline.YieldDirs` and `pipeline.RunYields` describe the concrete files for
  one run, while `pipeline.PipelineRunner` owns the workflow state such as
  whether the managed Python dependencies have already been checked.
- `openai_translate.OpenAISrtTranslator` owns chunked translation, repair
  requests, and checkpoint reuse. `TranslationCheckpoint` owns the partial JSON
  persistence contract, and `TranslationChunk` owns chunk labels and context
  windows.

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

The cache lookup uses the YouTube video ID as the filename stem. It does not
guess by newest file. Older `Video title [id]` filenames are still recognized
and migrated to `id.*` when no ID-named target exists.

This prevents a serious UX failure: running the script on a new URL and seeing a
previous video launch because it happened to be the newest local media file.

### Lossy-Only Media Storage

The script avoids lossless video and audio yields. YouTube streams are already
lossy, and Whisper only needs compact speech audio. This keeps disk use under
control.

### Resumable yt-dlp Downloads

The script uses yt-dlp's `.part` files and `--continue` behavior. This keeps
interrupted downloads resumable: if the script is stopped during the download
or before yt-dlp finishes merging streams, the next run can reuse the partial
state instead of downloading the video bytes again.

When the wrapper is interrupted while streaming yt-dlp output, it explicitly
terminates the child process before exiting. That prevents a second run from
starting a duplicate download while an orphaned yt-dlp process is still active.

The cache lookup still treats only final merged media files as durable video
yields. Partial files and yt-dlp intermediate stream files are left for yt-dlp
to resume or clean up on the next download attempt.

### Indexed-Cue OpenAI Translation

The OpenAI path translates indexed cue text in bounded chunks, derived from the
compacted and gap-extended SRT. Each chunk keeps the script's strict cue-count
contract and includes neighboring cue context for terminology continuity. This
is the resilient version of the original whole-document architecture:

- The model sees enough surrounding discourse for local consistency.
- It can translate recurring terms consistently.
- It can avoid locally plausible but globally wrong choices.
- It avoids the large single response that can trigger remote connection resets.
- Completed chunks are checkpointed and reused on rerun.
- The explicit cue JSON format reduces semantic drift where the model merges a
  split sentence into one cue and shifts later translations by one index.
- The source cue JSON is emitted in an even-then-odd index order inside each
  chunk. This keeps neighboring source cues away from each other in the prompt
  while preserving the numeric indexes used to reconstruct the final SRT.

The script then validates the structured output and applies translations to the
original compacted and gap-extended cue timings itself. The model is not trusted
to output final SRT timestamps.

### Compact Before Translating

Compacting and extending cue gaps before translation was chosen because the
final Dutch and English files should describe the same subtitle regions. If
English were translated from uncompacted Dutch and then compacted or
gap-extended independently, the two languages could drift in cue count and
timing.

Default OpenAI mode therefore creates this invariant:

```text
primary compacted and gap-extended SRT cues == English translated SRT cues
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

## Internal Module Map

High-level groups:

| Module | Responsibility |
| --- | --- |
| `yt_whisper_subs.cli` | CLI definition and source disambiguation. |
| `yt_whisper_subs.opts` | Language, translation-provider, compaction, and soft-period policy helpers. |
| `yt_whisper_subs.cfg` | Shared defaults and choices. |
| `yt_whisper_subs.proc` | Subprocess execution, hidden/visible and isolated Windows child policy, process-tree termination, external command checks, CUDA probing, and `.venv` maintenance. |
| `yt_whisper_subs.runlog` | Timestamped run logging and log-path creation. |
| `yt_whisper_subs.youtube` | Exact YouTube ID cache lookup, yield migration, and yt-dlp invocation. |
| `yt_whisper_subs.media` | Local video path validation and ffmpeg audio extraction. |
| `yt_whisper_subs.whisper_local` | Whisper CLI execution and model-cache cleanup. |
| `yt_whisper_subs.srt` | `SubtitleCue`, SRT parsing/rendering, file-yield validation, cue compaction, and gap extension. |
| `yt_whisper_subs.openai_client` | `.env` loading, Responses API requests, retries, response text extraction, JSON cleanup, and shared usage reporting. |
| `yt_whisper_subs.openai_translate` | `OpenAISrtTranslator`, chunk objects, checkpoint persistence, prompts, repair requests, validation, and English SRT rendering. |
| `yt_whisper_subs.openai_chapters` | Transcript windowing, stateless bilingual chapter prompting, structured validation/repair, and trusted timestamp mapping. |
| `yt_whisper_subs.chapters` | Versioned chapter JSON, validation, atomic persistence, and derived mpv FFmetadata rendering. |
| `yt_whisper_subs.subtitle_files` | `SubtitlePair` sidecar/archive hydration, syncing, backups, timing alignment, and finalization. |
| `yt_whisper_subs.playback` | ASS secondary subtitles, launch-scoped mpv policy, and thread-safe ownership of the active library player. |
| `yt_whisper_subs.mpv_ipc` | Duplex ephemeral named-pipe connection, queued exact seeks, paced property observation, and EOF handling. |
| `yt_whisper_subs.playback_progress` | Typed playback updates, worker-signal encoding, and completion-aware fraction math. |
| `yt_whisper_subs.pipeline` | `PipelineRunner`, yield directory/path objects, skip logic, generation routing, and playback handoff. |
| `yt_whisper_subs.pipeline_progress` | Opt-in structured phase protocol, stage weights, overall progress math, and yt-dlp/Whisper percentage recognition. |
| `yt_whisper_subs.library_pipeline` | Controlled CLI pipeline launch, process-tree pause/resume, trace forwarding, and durable progress checkpointing. |
| `yt_whisper_subs.app` | Top-level CLI, logging, error handling, and pipeline wiring. |
| `yt_whisper_subs.library_types` | Compositional channel, video metadata, local media, playback, and catalog records. |
| `yt_whisper_subs.library_db` | Thread-safe SQLite subscriptions, complete-snapshot retention, metadata, settings, local downloads, and playback state. |
| `yt_whisper_subs.library_job_db` | Focused SQLite mixin for active, paused, and interrupted pipeline recovery records. |
| `yt_whisper_subs.library_artifacts` | File-stamped SRT-health and parsed-chapter cache for I/O-free Qt painting. |
| `yt_whisper_subs.library_feed` | Bounded adaptive yt-dlp Videos/Streams discovery, Atom timestamps, and full metadata lookup. |
| `yt_whisper_subs.library_service` | Local scanning, retention policy, bounded metadata hydration, channel checks, safe auto-download, and playback orchestration. |
| `yt_whisper_subs.library_model` | Indexed sortable Qt table, in-memory channel/search/smart-view proxy, single-pass facets, and completion-aware watched presentation. |
| `yt_whisper_subs.library_progress` | Native pipeline and watched progress-bar rendering. |
| `yt_whisper_subs.library_widgets` | Native smart-filter shelf, dialogs, bilingual chapter inspector, selected-video details, and activity trace. |
| `yt_whisper_subs.library_chapter_actions` | GUI chapter generation, live-player seeking, and timestamp-aware playback fallback. |
| `yt_whisper_subs.library_window_actions` | Selection, settings, download/repair, and manifest-confirmed removal actions. |
| `yt_whisper_subs.library_yields` | Exact non-recursive per-video yield inventory and individual-file removal. |
| `yt_whisper_subs.task_cancel` | Qt-independent task controls for cooperative pause/resume, cancellation, and active process hooks. |
| `yt_whisper_subs.library_workers` | Background Qt task signalling with pause/resume plus distinct completion, failure, and cancellation outcomes. |
| `yt_whisper_subs.library_window_support` | Serialized controllable heavy-work scheduling, recovery UI, independent playback, metadata pacing, tray, and shutdown. |
| `yt_whisper_subs.library_theme` | Central native dark stylesheet and chapter-pane presentation. |
| `yt_whisper_subs.library_gui` | Main native window layout, persistent table-header state, and user interaction. |
| `yt_whisper_subs.library_bootstrap` / `library_app` | Interruptible managed Qt runtime bootstrap and desktop entry point. |
| `yt_whisper_subs.windows_startup` | Exact per-user HKCU Run registration and safely quoted quiet-tray launch command. |

The central data model is:

```python
class SubtitleCue(NamedTuple):
    start_ms: int
    end_ms: int
    text: str
```

This keeps SRT logic timestamp-based internally and avoids string manipulation
until final rendering.

Other structural data models are deliberately close to their behavior:

- `SubtitlePair` owns the two-file sidecar/archive subtitle contract.
- `RunYields` owns the concrete files requested by one source run.
- `TranslationCheckpoint` owns reusable partial OpenAI translation state.
- `ChapterSet` and `ChapterFiles` own authoritative bilingual plans and their
  derived mpv sidecars.
- `VideoMeta` composes identity, origin, and optional details without coupling
  remote metadata to local file state.
- `VideoRecord` adds subscription ownership, optional `LocalMedia`, and optional
  durable `PlaybackState` to that metadata.
- `LibraryDb` is the only module that owns catalog SQL.

## Future Codex Maintenance Notes

Start by preserving these invariants:

1. A second run on the same URL with all yields present must skip yt-dlp,
   ffmpeg, CUDA, Whisper, and OpenAI.
2. URL cache hits must be by exact YouTube video ID, not newest file.
3. Default OpenAI English translation must use the compacted and gap-extended
   primary SRT.
4. Default OpenAI English output must have the same cue count and timestamps as
   the compacted and gap-extended primary SRT.
5. Do not independently compact OpenAI English output after translation.
6. Do not print or commit `.env` or API keys.
7. Keep video and subtitle yields under `~/Videos/yt-whisper-subs` by default.
8. Keep video storage lossy.
9. Keep local Whisper/PyTorch dependencies in `.venv` beside the script.
10. Preserve sidecar/archive repair behavior.
11. Preserve the timestamped run log and avoid logging secrets.
12. Keep the first channel check as a no-download baseline.
13. Keep GUI downloads routed through the existing one-video CLI pipeline.
14. Keep GUI and CLI mpv behavior routed through `PlaybackPrefs` and
    `playback.play_video`.
15. Keep YouTube metadata sidecars outside `videos\` and the SQLite catalog
    rebuildable from local files.
16. Keep hidden subprocesses observable through the timestamped activity trace.
17. Keep structured GUI progress opt-in so direct CLI output remains unchanged.
18. Keep a terminal-launched library interruptible without orphaning pythonw.exe.
19. Keep watched progress monotonic and reserve 100% for a confirmed mpv EOF
    event stored separately from the numeric position.
20. Keep playback IPC launch-scoped; never rewrite or bypass the user's mpv
    configuration.
21. Keep smart-view predicates single-sourced with their facet counts; channel,
    search, and view scopes must remain independently composable.
22. Persist new channel placeholders before network discovery, allow multiple
    additions while busy, and serialize their lookups with video work.
23. Keep chapter requests stateless and separate from translation requests;
    model output selects transcript-window indexes, never authoritative times.
24. Keep chapter JSON authoritative and FFmetadata derived; never rewrite the
    downloaded media or the user's mpv configuration for chapters.
25. Preserve a minimum default chapter density of one per four minutes, so an
    hour-long video receives at least 15 chapters.
26. Route chapter jumps only to a matching active library player; otherwise use
    the existing launch-at-time path, without persistent mpv configuration.
27. Keep table layout state in the existing settings store, with model-provided
    default widths and a visible reset action.
28. Bound each channel section, expand only to recover known overlap, prune only
    complete snapshots, and never remove a downloaded video through retention.
29. Ignore Shorts in subscriptions and treat an absent Streams tab as a valid
    empty section rather than a partial-refresh failure.
30. Do not establish the automatic-download baseline from a partial channel
    refresh; surface the warning and retry safely.
31. Keep visible mpv playback independent from the serialized heavy-work lane,
    while retaining live watched progress and pipeline status.
32. A completed stage must correspond to usable durable yields; missing, empty,
    corrupt, and cue-less SRT files remain repairable Issues.
33. Cancel only the active heavy task and its isolated child process tree; do
    not stop independent playback or discard already queued channel work.
34. Remove one video's yields only through an exact inspected manifest, one
    non-recursive unlink at a time, while preserving its tracked catalog row.

When changing the project, useful verification commands are:

```powershell
python -m compileall .\yt_whisper_subs.py .\yt_whisper_subs .\tests
python -m unittest discover -s tests
python -m py_compile .\yt_whisper_subs.py
python .\yt_whisper_subs.py --help
python -m yt_whisper_subs --help
python -m yt_whisper_subs.library_app --help
```

The tracked tests cover the OpenAI translator's timing preservation, chunk
repair, and checkpoint cleanup; bilingual chapter density, stateless payloads,
validation repair, exact timestamp mapping, persistence, and mpv sidecars;
`SubtitlePair` archive hydration, syncing,
compaction, and backup behavior; metadata-preserving yt-dlp commands; shared
playback policy; channel normalization and timestamp mapping; SQLite catalog
semantics; playback IPC event handling; watched completion persistence; smart
view classification, live transitions, search-scoped counts, sidecar ingestion;
native table-header resizing, reordering, persistence, and reset behavior;
channel additions queued during active video work; the crucial future-only
automatic-download baseline; bounded adaptive feed scans; and complete-snapshot
retention that preserves local media; playback dispatch during active work;
single-Dutch-track mpv selection; invalid-yield repair classification; exact
single-video removal isolation; and active process-tree cancellation.
The progress tests additionally cover protocol round trips, opt-in CLI behavior,
phase weighting, tool percentage recognition, GUI-child scoping, and terminal
failure reporting.
Bootstrap tests cover normal managed-GUI exit propagation and Ctrl+C cleanup;
the subprocess tests independently require child termination before an
interrupt returns terminal control.

Mock the OpenAI translation path without making an API call:

```powershell
python -c "import argparse, tempfile, json; from pathlib import Path; from yt_whisper_subs import openai_client, openai_translate; d=Path(tempfile.mkdtemp()); src=d/'nl.srt'; dst=d/'en.srt'; src.write_text('1\n00:00:00,000 --> 00:00:01,200\nGoedemiddag allemaal.\n\n2\n00:00:01,200 --> 00:00:02,800\nWelkom bij de persconferentie.\n', encoding='utf-8'); openai_client.responses_api_request=lambda args,payload: {'output_text': json.dumps({'translations':[{'index':1,'text':'Good afternoon, everyone.'},{'index':2,'text':'Welcome to the press conference.'}]})}; args=argparse.Namespace(openai_translation_model='mock', openai_reasoning_effort='low', openai_translation_chunk_cues=120, openai_translation_context_cues=3, compact_line_width=50); openai_translate.translate_srt_with_openai(src,dst,args); print(dst.read_text(encoding='utf-8'))"
```

Expected property: the English output should retain the exact two timestamp
ranges from the input.

Check compaction routing for default OpenAI mode:

```powershell
python -c "import argparse; from yt_whisper_subs import opts; args=argparse.Namespace(compact_subs='english', english_translation_provider='openai', compact_primary_for_openai_translation=True); print(opts.should_compact_subtitles(args, is_english=False)); print(opts.should_compact_subtitles(args, is_english=True))"
```

Expected output:

```text
True
False
```

For a real API smoke test, use a tiny SRT and the `.env` file. Keep it tiny to
avoid unnecessary cost.

## Troubleshooting

### The terminal prompt does not return after closing the library window

Closing the main window hides the library in the system tray by default so
scheduled channel checks continue. That is a running application, so a terminal
launcher correctly remains attached. Choose **Library → Quit**, use **Quit**
from the tray menu, or disable **Keep checking when the window is closed** in
Library Settings for a normal exit. If it was launched from a terminal,
**Ctrl+C** is also supported: version `0.2.5` and newer terminate the managed
`pythonw.exe` child, avoid leaving an orphan tray process, and return exit code
`130` to the shell.

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

If the API reports that a reasoning effort is unsupported, pass a supported
effort explicitly. For the default `gpt-5-mini` model, use one of:

```text
minimal, low, medium, high
```

### OpenAI connection reset or timeout

Errors such as this are treated as transient:

```text
[WinError 10054] An existing connection was forcibly closed by the remote host
```

The script retries transient OpenAI network/server failures with exponential
backoff. Increase retries for flaky connections:

```powershell
python .\yt_whisper_subs.py --openai-max-retries 5 <source>
```

If failures persist with longer videos, reduce the OpenAI chunk size:

```powershell
python .\yt_whisper_subs.py --openai-translation-chunk-cues 60 <source>
```

If OpenAI returns fewer translations than requested for a chunk, the script
keeps the valid indexed translations and automatically asks OpenAI to repair
only the missing cue indexes. If that repair request also fails validation,
rerunning normally should reuse any previously checkpointed chunks and retry the
incomplete chunk.

If the failure happened after the primary SRT was generated, rerunning normally
should reuse the downloaded video and primary subtitles, then retry only the
missing English subtitle yield. If a `.en.partial.json` checkpoint exists, the
script also reuses completed OpenAI translation chunks. Avoid `--force` unless
you intentionally want to redownload and regenerate everything.

### CUDA is not visible

The script prints PyTorch CUDA visibility before Whisper. If CUDA is false:

- confirm Windows can see the NVIDIA GPU with `nvidia-smi`,
- update NVIDIA drivers if `nvidia-smi` is missing or cannot see the GPU,
- rerun with `--install-python-deps` so the script reinstalls the CUDA Torch
  wheel into `.venv`,
- or use `--device cpu` for a slower run.

Useful local check:

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no cuda')"
```

Expected CUDA output has a Torch version ending in something like `+cu128`,
prints a CUDA runtime version such as `12.8`, and reports `True` for
`torch.cuda.is_available()`.

### Whisper prints no transcript lines

The wrapper now reports long periods where Whisper is alive but silent. A single
heartbeat can be normal while CUDA initializes or the model loads, and repeated
heartbeats can happen when Whisper buffers transcript output while it works. If
heartbeat lines continue for a long time with no GPU or CPU activity, press
Ctrl-C once so the wrapper can terminate the child process, then rerun. The run
log will show the last command and heartbeat timing.

### Whisper reports `UnicodeEncodeError`

Whisper prints recognized text while transcribing. The wrapper forces UTF-8 mode
for Python subprocesses so characters outside the Windows console code page do
not abort Whisper before it writes the `.srt` file. Re-run the same command with
the fixed version; if a partial temporary subtitle directory remains under
`subtitles\whisper-*`, it can be ignored because the next run creates a fresh
temporary directory.

### yt-dlp warns about JavaScript runtimes or returns HTTP 403

The script installs yt-dlp's EJS component and automatically uses Deno or Node
when either executable is on `PATH`. The managed yt-dlp package is also checked
for updates weekly. Restart the library once to trigger an overdue update. If
the warning remains, install a current Deno (preferred by yt-dlp) or Node release
and ensure its executable is visible from PowerShell. Cookies may still be
needed for private, age-gated, or account-specific videos.

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
VIDEO_ID.mkv
```

Unsupported URL shapes may not yield a video ID, in which case no cache hit is
used.

### I want only to regenerate English subtitles

Avoid `--force` if you do not want to redownload the URL video or rerun
Whisper. Use `--force-english` instead:

```powershell
python .\yt_whisper_subs.py --force-english "https://www.youtube.com/watch?v=VIDEO_ID"
```

The script should reuse the video and primary subtitles, then regenerate
English with the selected Dutch-to-English provider.

### I want chapters for an existing download

Select the downloaded row in the native library and click **Generate** in the
chapter pane. From the CLI, request only the missing chapter yield with:

```powershell
python .\yt_whisper_subs.py --chapters "https://www.youtube.com/watch?v=VIDEO_ID"
```

If a plan already exists and should be replaced, use `--force-chapters` instead.
Both paths reuse the local video and subtitle yields; they do not rerun yt-dlp
or Whisper when those prerequisite yields exist. If subtitles are missing, the
normal pipeline creates them first. The new stage and OpenAI token usage appear
in the activity trace and timestamped run log.

### `mpv` does not auto-detect subtitles

The script writes sidecar subtitles beside the video specifically for
auto-detection:

```text
VIDEO_ID.srt
VIDEO_ID.en.srt
```

However, when the script launches `mpv`, it passes subtitle files explicitly and
uses `--sub-auto=no`. That prevents mpv from adding extra auto-detected tracks
on top of the ones the script selected.

### mpv audio plays but no video window appears

Current releases launch mpv with the visible-application child policy. Its
console remains suppressed on Windows, while the video window must appear in
the taskbar and Alt+Tab switcher. Background tools use the separate hidden
policy. If audio plays without a window, confirm that the running package is
version `0.2.4` or newer and restart the library so it loads the updated process
policy.

### What are `.ass` files?

ASS is Advanced SubStation Alpha, a subtitle format with styling. The script
creates a temporary ASS file for the secondary subtitle track so it can have a
different color, position, and font size. These temporary files are not the
authoritative subtitle yields.

## License

This repository is licensed under the GNU General Public License version 3. See
[LICENSE](LICENSE).

## Known Limitations

- The OpenAI translation path is chunked by cue count. Very long videos can
  still be expensive.
- Chapter generation is a separate whole-transcript OpenAI request and can add
  cost for long videos; input is compressed to at most 240 timed windows.
- `--language auto` does not trigger automatic English-for-Dutch translation.
- Existing `.en.srt` files are treated as ready regardless of whether they were
  produced by Whisper or OpenAI. Use `--force-english` or `--force` to
  regenerate them.
- The YouTube `.info.json` sidecar does not yet record subtitle-generation
  provenance such as Whisper/OpenAI models, prompt version, or compaction
  settings.
- SRT parsing is intentionally pragmatic, not a full subtitle spec
  implementation.
- Compaction heuristics are tuned for readability, not linguistic perfection.
- The script is Windows-first. Some paths and executable names assume Windows.
- `--force` is broad: for URL inputs it redownloads the video too.
- Periodic channel checks require the native app process to be open or in the
  system tray; the optional current-user login setting starts it quietly, but
  there is intentionally no privileged Windows service or scheduled task.
- YouTube flat channel listings omit some upload timestamps. Atom enriches the
  latest entries, while remaining retained rows are hydrated one at a time at
  the configured gentle pace and can temporarily show `—` for their date.

## Possible Future Improvements

These are intentionally not implemented yet:

- Extend the existing YouTube metadata sidecar with subtitle-generation models,
  compaction settings, prompt version, and generation timestamps.
- Add a `--translate-existing-srt` mode for translating an SRT without touching
  video/audio.
- Add an optional OpenAI SDK implementation if API usage grows.
- Broaden tests around SRT parser edge cases, CLI validation, and cheap-reuse
  orchestration without invoking external tools.
- Add provider metadata into generated subtitle comments or adjacent JSON.
- Add better language detection handoff so `--language auto` can still trigger
  English translation when Whisper detects Dutch.

## Minimal Mental Model

If you remember only one thing, remember this:

```text
Downloaded video is the durable media yield.
YouTube info JSON is the durable source metadata yield.
SQLite is a rebuildable catalog plus channel-subscription state.
Primary compacted and gap-extended SRT is the timing authority.
OpenAI translates text only.
The script renders English onto the primary SRT timings.
Sidecars are for mpv; archives are for durable reuse.
Second runs should be cheap.
Initial channel checks must never auto-download the backlog.
```
