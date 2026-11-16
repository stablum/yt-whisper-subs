"""Translate presentation text with gettext, without changing exercise content.

Example: `tr("Question {number}", number=3)` uses the saved interface language.
"""

from __future__ import annotations

import gettext
import re
import string
from datetime import date, datetime
from pathlib import Path

LANGUAGES = {"en": "English", "nl": "Nederlands", "fr": "Français", "it": "Italiano"}
LOCALE_DIR = Path(__file__).with_name("locales")
_language = "en"
_catalog: gettext.NullTranslations = gettext.NullTranslations()


def language() -> str:
    """Expose the active UI language; e.g. Settings selects its current entry."""

    return _language


def normalize(code: str) -> str:
    """Accept regional variants with an English fallback; e.g. `nl-NL` becomes `nl`."""

    base = code.replace("-", "_").split("_")[0].lower()
    return base if base in LANGUAGES else "en"


def configure(code: str) -> None:
    """Load a shipped catalog before constructing widgets; e.g. restart into French."""

    global _language, _catalog
    _language = normalize(code)
    _catalog = gettext.translation("ui", LOCALE_DIR, languages=[_language], fallback=True)


def mark(source: str, *, ctx: str | None = None) -> str:
    """Mark shared source labels for extraction without freezing translations; e.g. table specs."""

    return source


class _Formatter(string.Formatter):
    """Format translated placeholders without changing process locale; e.g. concurrent workers share French counts."""

    def format_field(self, value, spec: str) -> str:
        """Localize explicit grouping, decimals and dates; e.g. `{count:,}` keeps the locale's separators."""

        if isinstance(value, (date, datetime)) and spec.startswith("%"):
            return date_text(value, spec)
        rendered = super().format_field(value, spec)
        if isinstance(value, (int, float)) and re.search(r"[,%.efg]", spec):
            from babel import Locale
            from babel import numbers

            if match := re.fullmatch(r"(?:\.(\d+))?%", spec):
                digits = int(match[1] or 6)
                pattern = Locale.parse(_language).percent_formats[None].pattern
                if digits:
                    pattern = pattern.replace("0", "0." + "0" * digits, 1)
                return numbers.format_percent(value, format=pattern, locale=_language)
            decimal = numbers.get_decimal_symbol(_language)
            group = numbers.get_group_symbol(_language)
            rendered = rendered.replace(",", "\0").replace(".", decimal).replace("\0", group)
        return rendered


_formatter = _Formatter()


def tr(source: str, *, ctx: str | None = None, **values) -> str:
    """Translate a complete message before interpolation; e.g. a title can move around `{count}`."""

    # ctx is the gettext context: Clear as an action differs from Clear as a voice style.
    text = _catalog.pgettext(ctx, source) if ctx else _catalog.gettext(source)
    return _formatter.vformat(text, (), values) if values else text


def nt(singular: str, plural: str, count: int, *, ctx: str | None = None, **values) -> str:
    """Use the catalog's plural rule; e.g. zero new videos uses French's singular form."""

    text = _catalog.npgettext(ctx, singular, plural, count) if ctx else _catalog.ngettext(singular, plural, count)
    return _formatter.vformat(text, (), {"count": count, **values})


def number(value: int | float, spec: str = ",") -> str:
    """Format a standalone metric with UI separators; e.g. Italian `1.234,5` keeps its numeric sort key."""

    return _formatter.format_field(value, spec)


def date_text(value: date | datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Use locale-aware month and weekday names; e.g. the activity calendar follows the UI language."""

    from babel import dates

    tokens = {"%Y": "yyyy", "%m": "MM", "%d": "dd", "%H": "HH", "%M": "mm", "%S": "ss",
              "%a": "EEE", "%A": "EEEE", "%b": "MMM", "%B": "MMMM", "%Z": "z"}
    pattern = re.sub(r"%[YmdHMSaAbBZ]", lambda match: tokens[match[0]], fmt)
    if isinstance(value, datetime):
        return dates.format_datetime(value, pattern, locale=_language)
    return dates.format_date(value, pattern, locale=_language)
