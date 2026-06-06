"""End-to-end orchestration for download, subtitle generation, reuse, and playback.

Example: `pipeline.run_pipeline(args, paths, out_dir, log_path)`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import media
from yt_whisper_subs import openai_translate
from yt_whisper_subs import opts
from yt_whisper_subs import playback
from yt_whisper_subs import proc
from yt_whisper_subs import subtitle_files
from yt_whisper_subs import whisper_local
from yt_whisper_subs import youtube


class YieldDirs(NamedTuple):
    """Output directories that hold durable video, audio, and subtitle yields.

    Example: `YieldDirs.from_output_root(out_dir).create()`.
    """

    video: Path
    audio: Path
    subs: Path

    @classmethod
    def from_output_root(cls, out_dir: Path) -> YieldDirs:
        """Build the standard yield directory layout under one output root.

        Example: `YieldDirs.from_output_root(Path("~/Videos"))`.
        """

        return cls(video=out_dir / "videos", audio=out_dir / "audio", subs=out_dir / "subtitles")

    def create(self) -> None:
        """Create all yield directories before a run starts writing files.

        Example: `dirs.create()`.
        """

        for directory in self:
            directory.mkdir(parents=True, exist_ok=True)


class RunYields(NamedTuple):
    """Concrete file yields for one source video and optional English subtitles.

    Example: `run_yields.primary.ready()`.
    """

    video: Path
    audio: Path
    primary: subtitle_files.SubtitlePair
    english: subtitle_files.SubtitlePair
    make_english: bool

    def all_ready(self) -> bool:
        """Check whether all requested durable yields are already present.

        Example: `if run_yields.all_ready(): ...`.
        """

        english_ready = (not self.make_english) or self.english.ready()
        return self.video.exists() and self.primary.ready() and english_ready

    def srt_paths(self) -> list[Path]:
        """Return subtitle sidecars in the playback order expected by mpv.

        Example: `playback.play_video(video, run_yields.srt_paths(), args)`.
        """

        paths = [self.primary.sidecar]
        if self.make_english:
            paths.append(self.english.sidecar)
        return paths

    def print_paths(self, log_path: Path) -> None:
        """Print the expected media, log, sidecar, and archive yields.

        Example: `run_yields.print_paths(log_path)`.
        """

        print()
        print(f"Video: {self.video}")
        print(f"Audio: {self.audio}")
        print(f"Log:   {log_path}")
        print(f"SRT:   {self.primary.sidecar}")
        print(f"Archive SRT: {self.primary.archive}")
        if self.make_english:
            print(f"English SRT: {self.english.sidecar}")
            print(f"English Archive SRT: {self.english.archive}")

    def print_done(self, log_path: Path) -> None:
        """Print the final run summary after generation or cheap reuse.

        Example: `run_yields.print_done(log_path)`.
        """

        print()
        print("Done.")
        print(f"Video: {self.video}")
        print(f"Audio: {self.audio if self.audio.exists() else '(deleted)'}")
        print(f"Log:   {log_path}")
        print(f"Subs:  {self.primary.sidecar}")
        print(f"Archive Subs: {self.primary.archive}")
        if self.make_english:
            print(f"English Subs: {self.english.sidecar}")
            print(f"English Archive Subs: {self.english.archive}")


class PipelineRunner:
    """Coordinate one subtitle run while keeping state local to the workflow.

    Example: `PipelineRunner(args, paths, out_dir, log_path).run()`.
    """

    def __init__(self, args: argparse.Namespace, paths: dict[str, Path], out_dir: Path, log_path: Path) -> None:
        self._args = args
        self._paths = paths
        self._log_path = log_path
        self._dirs = YieldDirs.from_output_root(out_dir)
        self._python_deps_ready = False

    def run(self) -> int:
        """Run the full pipeline with cheap-yield reuse as the first-class path.

        Example: `runner.run()`.
        """

        self._ensure_requested_tools()
        self._dirs.create()

        source_video_id, video_path = self._resolve_video()
        run_yields = self._build_yields(video_path, source_video_id)
        self._prepare_existing_subtitles(run_yields)
        run_yields.print_paths(self._log_path)

        if run_yields.all_ready() and not self._args.force:
            return self._reuse_ready_yields(run_yields)

        need_primary_generation = (not run_yields.primary.ready()) or self._args.force
        need_english_generation = run_yields.make_english and ((not run_yields.english.ready()) or self._args.force)
        need_whisper = need_primary_generation or (
            need_english_generation and not opts.uses_openai_english_translation(self._args)
        )

        if self._args.install_python_deps and not self._python_deps_ready:
            self._ensure_python_deps()
        if need_whisper:
            self._ensure_whisper_ready()

        if need_primary_generation:
            self._generate_primary_subs(run_yields)
        else:
            print()
            print("Subtitle file already exists. Use --force to regenerate.")

        if run_yields.make_english:
            self._generate_english_subs(run_yields)

        self._finish(run_yields)
        return 0

    def _ensure_requested_tools(self) -> None:
        """Install optional tools and fail early if requested playback cannot run.

        Example: called once at runner startup.
        """

        if self._args.install_tools:
            proc.install_tools()

        if not self._args.no_play:
            proc.require_command("mpv")

    def _ensure_python_deps(self) -> None:
        """Create or validate the managed Python environment at most once.

        Example: `self._ensure_python_deps()` before Whisper or yt-dlp.
        """

        proc.ensure_python_deps(self._paths, self._args)
        self._python_deps_ready = True

    def _resolve_video(self) -> tuple[str | None, Path]:
        """Resolve, reuse, or download the source video for this run.

        Example: `source_video_id, video_path = self._resolve_video()`.
        """

        if not self._args.url:
            return None, media.resolve_video_path(self._args.video_file)

        source_video_id = youtube.youtube_video_id(self._args.url)
        video_path = None if self._args.force else youtube.latest_downloaded_video(self._dirs.video, self._args.url)
        if video_path:
            print()
            print(f"Found existing video yield: {video_path}")
        else:
            self._ensure_python_deps()
            proc.require_command("ffmpeg")
            print()
            print("Downloading compressed lossy video stream...")
            video_path = youtube.download_video(self._args.url, self._dirs.video, self._paths, self._args)

        if source_video_id:
            video_path = youtube.canonicalize_youtube_video_filename(video_path, source_video_id)
            youtube.migrate_legacy_youtube_yields_to_video_id(
                source_video_id,
                self._dirs.video,
                self._dirs.audio,
                self._dirs.subs,
            )

        return source_video_id, video_path

    def _build_yields(self, video_path: Path, source_video_id: str | None) -> RunYields:
        """Derive every concrete yield path from the resolved source video.

        Example: `run_yields = self._build_yields(video, "abc123")`.
        """

        video_base = source_video_id or video_path.stem
        primary_pair = subtitle_files.SubtitlePair(
            sidecar=video_path.with_suffix(".srt"),
            archive=self._dirs.subs / f"{video_base}.srt",
        )
        make_english = (
            self._args.english_for_dutch
            and self._args.task == "transcribe"
            and opts.is_dutch_language(self._args.language)
        )
        self._args.compact_primary_for_openai_translation = (
            make_english and opts.uses_openai_english_translation(self._args)
        )
        english_pair = subtitle_files.SubtitlePair(
            sidecar=video_path.with_name(f"{video_base}.en.srt"),
            archive=self._dirs.subs / f"{video_base}.en.srt",
        )
        audio_path = self._dirs.audio / f"{video_base}.{self._args.audio_format}"
        return RunYields(
            video=video_path,
            audio=audio_path,
            primary=primary_pair,
            english=english_pair,
            make_english=make_english,
        )

    def _prepare_existing_subtitles(self, run_yields: RunYields) -> None:
        """Hydrate and normalize reusable subtitles before skip decisions.

        Example: `self._prepare_existing_subtitles(run_yields)`.
        """

        run_yields.primary.hydrate("primary", self._args, is_english=False, force=self._args.force)
        if run_yields.make_english:
            run_yields.english.hydrate("English", self._args, is_english=True, force=self._args.force)

        run_yields.primary.ensure_compacted(
            self._args,
            is_english=False,
            label="primary",
            force=self._args.force,
        )
        run_yields.primary.ensure_extended_gaps(self._args, label="primary", force=self._args.force)
        if not run_yields.make_english:
            return

        run_yields.english.ensure_compacted(
            self._args,
            is_english=True,
            label="English",
            force=self._args.force,
        )
        run_yields.english.ensure_extended_gaps(self._args, label="English", force=self._args.force)
        if opts.uses_openai_english_translation(self._args):
            run_yields.english.align_timings_to(
                run_yields.primary.sidecar,
                self._args,
                label="English",
                force=self._args.force,
            )

    def _reuse_ready_yields(self, run_yields: RunYields) -> int:
        """Handle the cheap path when all requested yields already exist.

        Example: `return self._reuse_ready_yields(run_yields)`.
        """

        if self._args.install_python_deps and not self._python_deps_ready:
            self._ensure_python_deps()

        print()
        print("All requested yields are already present; skipping yt-dlp, ffmpeg, CUDA, Whisper, and OpenAI.")
        self._delete_audio_if_requested(run_yields)

        if self._args.no_play:
            run_yields.print_done(self._log_path)
        else:
            self._play(run_yields)

        return 0

    def _ensure_whisper_ready(self) -> None:
        """Validate local tools and CUDA visibility before Whisper work.

        Example: `self._ensure_whisper_ready()` before generation.
        """

        if not self._python_deps_ready:
            self._ensure_python_deps()
        proc.require_command("ffmpeg")

        print()
        print("Checking PyTorch CUDA visibility...")
        if self._args.device == "cuda" and not proc.check_cuda(self._paths):
            raise RuntimeError(
                "CUDA is not visible to PyTorch. Fix the NVIDIA driver/PyTorch CUDA install, "
                "or re-run with --device cpu."
            )

    def _generate_primary_subs(self, run_yields: RunYields) -> None:
        """Generate the primary SRT from extracted audio and finalize its pair.

        Example: `self._generate_primary_subs(run_yields)`.
        """

        print()
        print(f"Extracting mono 16 kHz lossy {self._args.audio_format} audio...")
        media.extract_audio(run_yields.video, run_yields.audio, self._args.audio_format, self._args.force)

        print()
        print("Running Whisper...")
        whisper_local.run_whisper(
            run_yields.audio,
            run_yields.primary.sidecar,
            self._dirs.subs,
            self._paths,
            self._args,
        )
        run_yields.primary.finalize(self._args, is_english=False, label="primary")

    def _generate_english_subs(self, run_yields: RunYields) -> None:
        """Generate or reuse English subtitles with the selected provider.

        Example: `self._generate_english_subs(run_yields)`.
        """

        if run_yields.english.ready() and not self._args.force:
            print()
            print("English subtitle file already exists. Use --force to regenerate.")
            return

        if opts.uses_openai_english_translation(self._args):
            self._generate_openai_english_subs(run_yields)
        else:
            self._generate_whisper_english_subs(run_yields)

    def _generate_openai_english_subs(self, run_yields: RunYields) -> None:
        """Translate primary cue text through OpenAI while preserving timings.

        Example: `self._generate_openai_english_subs(run_yields)`.
        """

        if not run_yields.primary.sidecar.exists():
            raise RuntimeError("primary subtitles are required before OpenAI English translation can run.")

        print()
        print("Generating English subtitles from indexed primary cue text with OpenAI...")
        openai_translate.translate_srt_with_openai(
            run_yields.primary.sidecar,
            run_yields.english.sidecar,
            self._args,
        )
        run_yields.english.align_timings_to(
            run_yields.primary.sidecar,
            self._args,
            label="English",
            force=False,
        )

    def _generate_whisper_english_subs(self, run_yields: RunYields) -> None:
        """Generate English subtitles with Whisper's audio translation task.

        Example: `self._generate_whisper_english_subs(run_yields)`.
        """

        print()
        print("Generating English subtitles from Dutch audio...")
        media.extract_audio(run_yields.video, run_yields.audio, self._args.audio_format, self._args.force)
        whisper_local.run_whisper(
            run_yields.audio,
            run_yields.english.sidecar,
            self._dirs.subs,
            self._paths,
            self._args,
            task="translate",
            language=self._args.language,
            model=opts.english_model(self._args),
        )
        run_yields.english.finalize(self._args, is_english=True, label="English")

    def _finish(self, run_yields: RunYields) -> None:
        """Clean up optional audio and either print summary or open playback.

        Example: `self._finish(run_yields)`.
        """

        self._delete_audio_if_requested(run_yields)
        if self._args.no_play:
            run_yields.print_done(self._log_path)
        else:
            self._play(run_yields)

    def _delete_audio_if_requested(self, run_yields: RunYields) -> None:
        """Remove the extracted audio cache only when the user requested it.

        Example: `self._delete_audio_if_requested(run_yields)`.
        """

        if not self._args.keep_audio and run_yields.audio.exists():
            run_yields.audio.unlink()

    def _play(self, run_yields: RunYields) -> None:
        """Launch mpv with the sidecar subtitles selected for this run.

        Example: `self._play(run_yields)`.
        """

        print()
        print("Opening in mpv with subtitles...")
        playback.play_video(run_yields.video, run_yields.srt_paths(), self._args)


def resolve_output_dir(out_dir: str | None) -> Path:
    """Resolve the configured output root or the default yield root.

    Example: `resolve_output_dir(args.out_dir)`.
    """

    if out_dir:
        return Path(out_dir).expanduser().resolve()
    return cfg.DEFAULT_OUTPUT_DIR.resolve()


def run_pipeline(args: argparse.Namespace, paths: dict[str, Path], out_dir: Path, log_path: Path) -> int:
    """Run the subtitle pipeline through the cohesive runner object.

    Example: `run_pipeline(args, proc.venv_paths(), out_dir, log_path)`.
    """

    return PipelineRunner(args, paths, out_dir, log_path).run()
