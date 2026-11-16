"""Extract, validate and compile the GUI's standard gettext catalogs.

Example: `.venv/Scripts/python tools/update_translations.py --check` checks release readiness.
"""

import argparse
import ast
import io
import string
import sys
from pathlib import Path

from babel.messages import catalog, mofile, pofile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Running this file directly puts tools/, rather than the project, on sys.path.
from yt_whisper_subs import i18n

LOCALES = ROOT / "yt_whisper_subs" / "locales"
LANGUAGES = tuple(code for code in i18n.LANGUAGES if code != "en")


def template() -> catalog.Catalog:
    """Extract literal message sources and references; e.g. dynamic exercise text is never catalogued."""

    result = catalog.Catalog(project="yt-whisper-subs", charset="utf-8")
    for path in sorted((ROOT / "yt_whisper_subs").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "i18n":
                continue
            if node.func.attr not in {"tr", "mark", "nt"}:
                continue
            count = 2 if node.func.attr == "nt" else 1
            args = node.args[:count]
            # Babel's token extractor can mistake a dynamic lookup's key for a
            # message (tr(SKILLS[row['skill']])). Only literal sources belong here.
            if len(args) == count and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in args):
                texts = tuple(arg.value for arg in args)
                message = texts if count == 2 else texts[0]
                context = next((kw.value.value for kw in node.keywords if kw.arg == "ctx"
                                and isinstance(kw.value, ast.Constant)), None)
                entry = result.add(message, context=context,
                                   locations=[(path.relative_to(ROOT).as_posix(), node.lineno)])
                entry.flags.discard("python-format")  # Percent signs are literal; interpolation uses braces only.
    return result


def fields(text: str) -> set[tuple[str, str, str | None]]:
    """Compare named placeholders and formats; e.g. a translator cannot drop an answer count."""

    return {(name, spec, conversion) for _, name, spec, conversion in string.Formatter().parse(text)
            if name is not None}


def validate(source: catalog.Catalog, translated: catalog.Catalog) -> list[str]:
    """Reject missing, fuzzy or unsafe messages; e.g. every plural form must preserve its variables."""

    errors = []
    for message in source:
        if not message.id:
            continue
        entry = translated.get(message.id, context=message.context)
        if entry is None or not entry.string or "fuzzy" in entry.flags:
            errors.append(f"Untranslated: {message.id!r}")
            continue
        sources = message.id if isinstance(message.id, tuple) else (message.id,)
        values = entry.string if isinstance(entry.string, tuple) else (entry.string,)
        for idx, text in enumerate(values):
            expected = sources[min(idx, len(sources) - 1)]
            if not text or fields(text) != fields(expected):
                errors.append(f"Invalid placeholders or empty translation: {message.id!r}")
        for check in entry.check(translated):
            errors.append(f"{message.id!r}: {check}")
    return errors


def _write_source(path: Path, messages: catalog.Catalog) -> None:
    """Keep generated catalogs free of obsolete entries and trailing blank lines; e.g. clean release diffs."""

    stream = io.BytesIO()
    pofile.write_po(stream, messages, sort_output=True, width=100, ignore_obsolete=True)
    path.write_bytes(stream.getvalue().rstrip() + b"\n")


def main() -> int:
    """Update source references or check compiled catalogs; e.g. CI runs `--check` without editing files."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = template()
    LOCALES.mkdir(exist_ok=True)
    if not args.check:
        _write_source(LOCALES / "ui.pot", source)
    errors = []
    for code in LANGUAGES:
        path = LOCALES / code / "LC_MESSAGES" / "ui.po"
        if not path.exists():
            errors.append(f"Missing {path}")
            continue
        with path.open("rb") as stream:
            translated = pofile.read_po(stream, locale=code)
        translated.update(source, no_fuzzy_matching=True, update_creation_date=False)
        for entry in translated:
            entry.flags.discard("python-format")
        errors.extend(f"{code}: {error}" for error in validate(source, translated))
        compiled = io.BytesIO()
        mofile.write_mo(compiled, translated)
        if args.check:
            if not path.with_suffix(".mo").exists() or path.with_suffix(".mo").read_bytes() != compiled.getvalue():
                errors.append(f"{code}: compiled catalog is out of date")
        else:
            _write_source(path, translated)
            path.with_suffix(".mo").write_bytes(compiled.getvalue())
    if errors:
        print("\n".join(errors))
        return 1
    print(f"{len(source)} messages validated in {len(LANGUAGES) + 1} UI languages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
