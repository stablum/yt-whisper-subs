"""End-to-end orchestration for download, subtitle generation, reuse, and playback.

Example: `pipeline.run_pipeline(args, paths, out_dir, log_path)`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from yt_whisper_subs import cfg
from yt_whisper_subs import media
from yt_whisper_subs import openai_translate
from yt_whisper_subs import opts
from yt_whisper_subs import playback
from yt_whisper_subs import proc
from yt_whisper_subs import subtitle_files
from yt_whisper_subs import whisper_local
from yt_whisper_subs import youtube


def resolve_output_dir(out_dir: str | None) -> Path:
    """Resolve the configured output root or the default yield root.

    Example: `resolve_output_dir(args.out_dir)`.
    """

    if out_dir:
        return Path(out_dir).expanduser().resolve()
    return cfg.DEFAULT_OUTPUT_DIR.resolve()


def print_yield_paths(
    video_path: Path,
    audio_path: Path,
    log_path: Path,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    *,
    make_english_subs: bool,
    english_sidecar_srt_path: Path,
    english_archive_srt_path: Path,
) -> None:
    """Print the expected media, log, sidecar, and archive yields.

    Example: `print_yield_paths(video, audio, log, srt, archive, make_english_subs=True, ...)`.
    """

    print()
    print(f"Video: {video_path}")
    print(f"Audio: {audio_path}")
    print(f"Log:   {log_path}")
    print(f"SRT:   {sidecar_srt_path}")
    print(f"Archive SRT: {archive_srt_path}")
    if make_english_subs:
        print(f"English SRT: {english_sidecar_srt_path}")
        print(f"English Archive SRT: {english_archive_srt_path}")


def print_done(
    video_path: Path,
    audio_path: Path,
    log_path: Path,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    *,
    make_english_subs: bool,
    english_sidecar_srt_path: Path,
    english_archive_srt_path: Path,
) -> None:
    """Print the final run summary after generation or cheap reuse.

    Example: `print_done(video, audio, log, srt, archive, make_english_subs=False, ...)`.
    """

    print()
    print("Done.")
    print(f"Video: {video_path}")
    print(f"Audio: {audio_path if audio_path.exists() else '(deleted)'}")
    print(f"Log:   {log_path}")
    print(f"Subs:  {sidecar_srt_path}")
    print(f"Archive Subs: {archive_srt_path}")
    if make_english_subs:
        print(f"English Subs: {english_sidecar_srt_path}")
        print(f"English Archive Subs: {english_archive_srt_path}")


def run_pipeline(args: argparse.Namespace, paths: dict[str, Path], out_dir: Path, log_path: Path) -> int:
    """Run the full subtitle pipeline with cheap-yield reuse as the first-class path.

    Example: `run_pipeline(args, proc.venv_paths(), out_dir, log_path)`.
    """

    if args.install_tools:
        proc.install_tools()

    if not args.no_play:
        proc.require_command("mpv")

    video_dir = out_dir / "videos"
    audio_dir = out_dir / "audio"
    subs_dir = out_dir / "subtitles"
    video_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    subs_dir.mkdir(parents=True, exist_ok=True)
    python_deps_ready = False

    if args.url:
        source_video_id = youtube.youtube_video_id(args.url)
        video_path = None if args.force else youtube.latest_downloaded_video(video_dir, args.url)
        if video_path:
            print()
            print(f"Found existing video yield: {video_path}")
        else:
            proc.ensure_python_deps(paths, args)
            python_deps_ready = True
            proc.require_command("ffmpeg")
            print()
            print("Downloading compressed lossy video stream...")
            video_path = youtube.download_video(args.url, video_dir, paths, args)
        if source_video_id:
            video_path = youtube.canonicalize_youtube_video_filename(video_path, source_video_id)
            youtube.migrate_legacy_youtube_yields_to_video_id(source_video_id, video_dir, audio_dir, subs_dir)
    else:
        source_video_id = None
        video_path = media.resolve_video_path(args.video_file)

    video_base = source_video_id or video_path.stem
    audio_path = audio_dir / f"{video_base}.{args.audio_format}"
    sidecar_srt_path = video_path.with_suffix(".srt")
    archive_srt_path = subs_dir / f"{video_base}.srt"
    make_english_subs = args.english_for_dutch and args.task == "transcribe" and opts.is_dutch_language(args.language)
    args.compact_primary_for_openai_translation = make_english_subs and opts.uses_openai_english_translation(args)
    english_sidecar_srt_path = video_path.with_name(f"{video_base}.en.srt")
    english_archive_srt_path = subs_dir / f"{video_base}.en.srt"

    subtitle_files.hydrate_subtitle_pair(
        "primary",
        sidecar_srt_path,
        archive_srt_path,
        args,
        is_english=False,
        force=args.force,
    )
    if make_english_subs:
        subtitle_files.hydrate_subtitle_pair(
            "English",
            english_sidecar_srt_path,
            english_archive_srt_path,
            args,
            is_english=True,
            force=args.force,
        )

    subtitle_files.ensure_compacted_subtitle_pair(
        sidecar_srt_path,
        archive_srt_path,
        args,
        is_english=False,
        label="primary",
        force=args.force,
    )
    subtitle_files.ensure_extended_subtitle_gap_pair(
        sidecar_srt_path,
        archive_srt_path,
        args,
        label="primary",
        force=args.force,
    )
    if make_english_subs:
        subtitle_files.ensure_compacted_subtitle_pair(
            english_sidecar_srt_path,
            english_archive_srt_path,
            args,
            is_english=True,
            label="English",
            force=args.force,
        )
        subtitle_files.ensure_extended_subtitle_gap_pair(
            english_sidecar_srt_path,
            english_archive_srt_path,
            args,
            label="English",
            force=args.force,
        )
        if opts.uses_openai_english_translation(args):
            subtitle_files.ensure_matching_subtitle_timing_pair(
                sidecar_srt_path,
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                label="English",
                force=args.force,
            )

    print_yield_paths(
        video_path,
        audio_path,
        log_path,
        sidecar_srt_path,
        archive_srt_path,
        make_english_subs=make_english_subs,
        english_sidecar_srt_path=english_sidecar_srt_path,
        english_archive_srt_path=english_archive_srt_path,
    )

    primary_ready = subtitle_files.subtitle_pair_ready(sidecar_srt_path, archive_srt_path)
    english_ready = (not make_english_subs) or subtitle_files.subtitle_pair_ready(
        english_sidecar_srt_path,
        english_archive_srt_path,
    )
    all_yields_ready = video_path.exists() and primary_ready and english_ready

    if all_yields_ready and not args.force:
        if args.install_python_deps and not python_deps_ready:
            proc.ensure_python_deps(paths, args)
            python_deps_ready = True

        print()
        print("All requested yields are already present; skipping yt-dlp, ffmpeg, CUDA, Whisper, and OpenAI.")

        if not args.keep_audio and audio_path.exists():
            audio_path.unlink()

        if args.no_play:
            print_done(
                video_path,
                audio_path,
                log_path,
                sidecar_srt_path,
                archive_srt_path,
                make_english_subs=make_english_subs,
                english_sidecar_srt_path=english_sidecar_srt_path,
                english_archive_srt_path=english_archive_srt_path,
            )
        else:
            print()
            print("Opening in mpv with subtitles...")
            srt_paths = [sidecar_srt_path]
            if make_english_subs:
                srt_paths.append(english_sidecar_srt_path)
            playback.play_video(video_path, srt_paths, args)

        return 0

    need_primary_generation = (not primary_ready) or args.force
    need_english_generation = make_english_subs and (
        not subtitle_files.subtitle_pair_ready(english_sidecar_srt_path, english_archive_srt_path) or args.force
    )
    need_whisper = need_primary_generation or (
        need_english_generation and not opts.uses_openai_english_translation(args)
    )

    if args.install_python_deps and not python_deps_ready:
        proc.ensure_python_deps(paths, args)
        python_deps_ready = True

    if need_whisper:
        if not python_deps_ready:
            proc.ensure_python_deps(paths, args)
            python_deps_ready = True
        proc.require_command("ffmpeg")

        print()
        print("Checking PyTorch CUDA visibility...")
        if args.device == "cuda" and not proc.check_cuda(paths):
            raise RuntimeError(
                "CUDA is not visible to PyTorch. Fix the NVIDIA driver/PyTorch CUDA install, "
                "or re-run with --device cpu."
            )

    if primary_ready and not args.force:
        print()
        print("Subtitle file already exists. Use --force to regenerate.")
    else:
        print()
        print(f"Extracting mono 16 kHz lossy {args.audio_format} audio...")
        media.extract_audio(video_path, audio_path, args.audio_format, args.force)

        print()
        print("Running Whisper...")
        whisper_local.run_whisper(audio_path, sidecar_srt_path, subs_dir, paths, args)
        subtitle_files.finalize_subtitle_pair(
            sidecar_srt_path,
            archive_srt_path,
            args,
            is_english=False,
            label="primary",
        )

    if make_english_subs:
        if subtitle_files.subtitle_pair_ready(english_sidecar_srt_path, english_archive_srt_path) and not args.force:
            print()
            print("English subtitle file already exists. Use --force to regenerate.")
        elif opts.uses_openai_english_translation(args):
            if not sidecar_srt_path.exists():
                raise RuntimeError("primary subtitles are required before OpenAI English translation can run.")

            print()
            print("Generating English subtitles from indexed primary cue text with OpenAI...")
            openai_translate.translate_srt_with_openai(sidecar_srt_path, english_sidecar_srt_path, args)
            subtitle_files.ensure_matching_subtitle_timing_pair(
                sidecar_srt_path,
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                label="English",
                force=False,
            )
        else:
            print()
            print("Generating English subtitles from Dutch audio...")
            media.extract_audio(video_path, audio_path, args.audio_format, args.force)
            whisper_local.run_whisper(
                audio_path,
                english_sidecar_srt_path,
                subs_dir,
                paths,
                args,
                task="translate",
                language=args.language,
                model=opts.english_model(args),
            )
            subtitle_files.finalize_subtitle_pair(
                english_sidecar_srt_path,
                english_archive_srt_path,
                args,
                is_english=True,
                label="English",
            )

    if not args.keep_audio and audio_path.exists():
        audio_path.unlink()

    if args.no_play:
        print_done(
            video_path,
            audio_path,
            log_path,
            sidecar_srt_path,
            archive_srt_path,
            make_english_subs=make_english_subs,
            english_sidecar_srt_path=english_sidecar_srt_path,
            english_archive_srt_path=english_archive_srt_path,
        )
    else:
        print()
        print("Opening in mpv with subtitles...")
        srt_paths = [sidecar_srt_path]
        if make_english_subs:
            srt_paths.append(english_sidecar_srt_path)
        playback.play_video(video_path, srt_paths, args)

    return 0
