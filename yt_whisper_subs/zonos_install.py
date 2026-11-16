"""Install the full open-source Zonos runtime and verified models in a reusable user cache.

Example: `python -m yt_whisper_subs.zonos_install` builds locally and fills the standard Hugging Face cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
from pathlib import Path

from yt_whisper_subs import proc


SOURCE = "96d9e0ec1e39083ed336ac78f24e3dbc93721c0f"
REVISION = "d2d142a86b1e6aeb4252679673c86ecd86177bf8"
REPO = "Zyphra/ZONOS3-Mini"
MODELS = {"gguf/backbone-ptq.gguf", "gguf/zac-keep2.gguf", "gguf/steer.gguf", "gguf/spk-encoder.gguf"}


def root() -> Path:
    """Locate the shared installation independently from a project checkout; e.g. another app can run the same executable and weights."""

    return Path.home() / ".cache" / "zonos3-mini"


def installation() -> dict:
    """Read only a complete pinned install; e.g. selecting a voice never starts a model download."""

    manifest = root() / "install.json"
    if not manifest.is_file():
        raise RuntimeError("Install ZONOS3-Mini from Voices first.")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        from yt_whisper_subs import listening_voices

        names = MODELS | {row["file"] for row in listening_voices.catalog()["voices"]}
        required = [Path(data["binary"]), *(Path(data["files"][name]) for name in names)]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("ZONOS3-Mini installation record is incomplete. Use Install / repair in Voices.") from exc
    if data.get("revision") != REVISION or data.get("source_revision") != SOURCE:
        raise RuntimeError("Update the ZONOS3-Mini installation from Voices.")
    if any(not path.is_file() for path in required):
        raise RuntimeError("ZONOS3-Mini has missing files. Use Install / repair in Voices.")
    return data


def _tools(report) -> Path:
    """Prepare optional build/download tools only during installation; e.g. ordinary Piper playback needs no compiler or HF client."""

    python = proc.venv_paths()["python"]
    scripts = python.parent
    report("Preparing Zonos installation tools…")
    proc.run(["uv", "pip", "install", "--python", python, "huggingface-hub>=0.36,<2", "cmake>=3.31", "ninja"],
             capture_stdout=True, stream_stdout=True)
    return scripts / ("cmake.exe" if os.name == "nt" else "cmake")


def _source(report) -> Path:
    """Check out the official pinned runtime and submodules; e.g. builds use source matching the shipped control catalogue."""

    directory = root() / "source"
    if not (directory / ".git").is_dir():
        report("Fetching the open-source Zonos runtime…")
        proc.run(["git", "clone", "--no-checkout", "https://github.com/Zyphra/ZONOS3-Mini.git", directory])
    proc.run(["git", "-C", directory, "fetch", "--depth", "1", "origin", SOURCE])
    proc.run(["git", "-C", directory, "checkout", "--detach", SOURCE])
    proc.run(["git", "-C", directory, "submodule", "update", "--init", "--depth", "1", "servers/ggml/ggml", "third_party/zyphra-textnorm"])
    return directory


def _build(cmake: Path, source: Path, report) -> Path:
    """Build a native CPU runtime with the local toolchain; e.g. speech generation has no server or Python dependency afterward."""

    report("Building Zonos from source…")
    args = [cmake, "-S", source / "servers/ggml", "-B", root() / "build", "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF"]
    if os.name == "nt":
        where = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
        if not where.is_file():
            raise RuntimeError("Building Zonos needs Visual Studio C++ Build Tools and Rust. Install these tools, then retry.")
        args.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
    if not shutil.which("cargo"):
        raise RuntimeError("Building Zonos needs Rust (cargo). Install Rust, then retry.")
    proc.run(args)
    proc.run([cmake, "--build", root() / "build", "--config", "Release", "--target", "zonos3-cli", "--parallel", "6"])
    name = "zonos3-cli.exe" if os.name == "nt" else "zonos3-cli"
    candidates = [root() / "build/Release" / name, root() / "build" / name]
    return next(path for path in candidates if path.is_file())


def _models(report) -> dict:
    """Download only required inference assets and all published voices, checking upstream content hashes; e.g. model files remain reusable by other HF apps."""

    from huggingface_hub import hf_hub_download

    url = f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true"
    with urllib.request.urlopen(url, timeout=60) as response:
        metadata = json.load(response)
    rows = [row for row in metadata["siblings"] if row["rfilename"] in MODELS or row["rfilename"].startswith("gguf/voices/")]
    files = {}
    for idx, row in enumerate(rows, 1):
        name = row["rfilename"]
        report(f"Checking Zonos model and voice files · {idx}/{len(rows)}")
        # Explicit user-cache location is shared with other apps, independent of their working directory.
        path = Path(hf_hub_download(REPO, name, revision=REVISION, cache_dir=Path.home() / ".cache/huggingface/hub")).resolve()
        if "lfs" in row:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            expected = row["lfs"]["sha256"]
        else:
            data = path.read_bytes()
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            expected = row["blobId"]
        if actual != expected:
            raise ValueError(f"Zonos file integrity check failed: {name}")
        files[name] = str(path)
    return files


def install(report) -> dict:
    """Publish an installation record only after build and verification succeed; e.g. interrupted setup never masquerades as a usable engine."""

    root().mkdir(parents=True, exist_ok=True)
    cmake = _tools(report)
    source = _source(report)
    binary = _build(cmake, source, report)
    files = _models(report)
    data = {"repo": REPO, "revision": REVISION, "source_revision": SOURCE, "binary": str(binary), "files": files}
    temp = root() / "install.json.part"
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(root() / "install.json")
    report("ZONOS3-Mini installed in your shared user cache.")
    return data


if __name__ == "__main__":
    install(print)
