"""Check real multilingual widgets, locale formatting and catalog safety.

Example: `python -m unittest tests.test_i18n` uses temporary catalogs and no remote services.
"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from tests import practice_fixtures
from tools import update_translations
from yt_whisper_subs import comprehension
from yt_whisper_subs import i18n
from yt_whisper_subs import library_db
from yt_whisper_subs import library_gui
from yt_whisper_subs import library_locale
from yt_whisper_subs import library_practice
from yt_whisper_subs import library_voice_controls
from yt_whisper_subs import library_widgets
from yt_whisper_subs import practice_service
from yt_whisper_subs import practice_tracks


class CatalogTests(unittest.TestCase):
    """Exercise shipped catalogs rather than mock lookups; e.g. every translated placeholder can actually render."""

    def tearDown(self) -> None:
        """Restore the default for unrelated tests; e.g. English UI expectations remain independent of order."""

        i18n.configure("en")

    def test_every_shipped_message_renders_and_compiled_catalogs_match(self) -> None:
        """Cover all plural forms and date/number formats; e.g. no translated action can raise a missing-field error."""

        source = update_translations.template()
        with mock.patch("sys.argv", ["update_translations.py", "--check"]):
            self.assertEqual(update_translations.main(), 0)
        for code in i18n.LANGUAGES:
            i18n.configure(code)
            for message in source:
                if not message.id:
                    continue
                texts = message.id if isinstance(message.id, tuple) else (message.id,)
                values = {}
                for name, spec, _conversion in update_translations.fields(texts[0]):
                    values[name] = datetime(2026, 11, 17, 14, 30).astimezone() if spec.startswith("%") else 12
                with self.subTest(language=code, source=message.id):
                    if isinstance(message.id, tuple):
                        values.pop("count", None)
                        for count in (0, 1, 2, 1000):
                            self.assertTrue(i18n.nt(*texts, count, ctx=message.context, **values))
                    else:
                        self.assertTrue(i18n.tr(message.id, ctx=message.context, **values))

    def test_fallback_regions_and_literal_interpolation(self) -> None:
        """Preserve user text during interpolation; e.g. a title containing braces is never interpreted again."""

        i18n.configure("fr-FR")
        self.assertEqual(i18n.tr("Practice"), "Entraînement")
        self.assertEqual(i18n.tr("Clear"), "Effacer")
        self.assertEqual(i18n.tr("Clear", ctx="voice style"), "Clair")
        self.assertEqual(i18n.tr("Playing {title}", title="{do not format}"), "Lecture de {do not format}")
        self.assertEqual(i18n.tr("An uncatalogued message"), "An uncatalogued message")
        i18n.configure("xx")
        self.assertEqual(i18n.language(), "en")
        self.assertEqual(i18n.tr("Practice"), "Practice")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(i18n, "LOCALE_DIR", Path(folder)):
            i18n.configure("it")
            self.assertEqual(i18n.tr("Practice"), "Practice")

    def test_plural_rules_and_localized_values(self) -> None:
        """Check differences that English-only tests miss; e.g. French zero is singular and Italian groups with a dot."""

        singular, plural = "{count:,} video", "{count:,} videos"
        i18n.configure("fr")
        self.assertEqual(i18n.nt(singular, plural, 0), "0 vidéo")
        self.assertEqual(i18n.nt(singular, plural, 2), "2 vidéos")
        self.assertEqual(i18n.number(1234.5, ",.1f"), "1\u202f234,5")
        self.assertEqual(i18n.number(.375, ".1%"), "37,5\u00a0%")
        self.assertIn("novembre", i18n.date_text(datetime(2026, 11, 17), "%d %B %Y"))
        i18n.configure("it")
        self.assertEqual(i18n.number(1234.5, ",.1f"), "1.234,5")
        i18n.configure("nl")
        self.assertEqual(i18n.nt(singular, plural, 0), "0 video's")


class WidgetTests(unittest.TestCase):
    """Build actual Qt controls in every language; e.g. source identities still select the same exercise direction."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share one Qt event loop; e.g. translated native button captions require QApplication."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        """Use isolated learner data; e.g. changing UI language cannot mutate the user's practice history."""

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = library_db.LibraryDb(Path(folder.name) / "catalog.sqlite3")
        self.db.initialize()
        practice_fixtures.seed(self.db)
        self.addCleanup(library_locale.install, self.app, "en")

    def test_settings_and_practice_navigation_in_all_languages(self) -> None:
        """Include the newly added comprehension pages; e.g. French UI retains Dutch content and stable track IDs."""

        expected = {"en": ("Library settings", "Reading comprehension", "Save"),
                    "nl": ("Bibliotheekinstellingen", "Leesbegrip", "Opslaan"),
                    "fr": ("Paramètres de la bibliothèque", "Compréhension écrite", "Enregistrer"),
                    "it": ("Impostazioni della raccolta", "Comprensione scritta", "Salva")}
        values = library_widgets.SettingsValues(4, "", True, False, 50, "")
        engine = practice_service.PracticeService(self.db, practice_fixtures.FakeGenerator())
        for code, (heading, comprehension_label, save) in expected.items():
            with self.subTest(language=code):
                library_locale.install(self.app, code)
                settings = library_widgets.SettingsDialog(values._replace(ui_language=code))
                self.assertEqual(settings.windowTitle(), heading)
                selector = settings.findChild(QtWidgets.QComboBox, "uiLanguage")
                self.assertEqual(selector.currentData(), code)
                self.assertEqual(settings.values().ui_language, code)
                self.assertEqual([selector.itemText(idx) for idx in range(4)], list(i18n.LANGUAGES.values()))
                buttons = settings.findChild(QtWidgets.QDialogButtonBox)
                caption = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).text().replace("&", "")
                self.assertEqual(caption, save)
                dialog = library_practice.PracticeDialog(SimpleNamespace(db=self.db), engine=engine)
                self.assertEqual(dialog._tabs.tabText(library_practice.Page.READ_COMPREHENSION).splitlines()[0], comprehension_label)
                self.assertEqual(comprehension.Direction.READ.value, "read")
                self.assertEqual(practice_tracks.Track.READ_COMPREHENSION.settings_key, "comprehension:read_options")
                genre = dialog._comprehension[comprehension.Direction.READ]._view.options._genre
                self.assertEqual(genre.currentData(), "mixed")
                for progress in dialog._comprehension_progress.values():
                    progress.refresh()  # Stable English table identities must still find their localized views.
                dialog.reject()
                settings.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    @mock.patch("yt_whisper_subs.library_voice_controls.listening_voices.catalog", return_value={"voices": [], "controls": []})
    def test_translated_voice_presets_keep_their_action_identity(self, _catalog) -> None:
        """Click every style in a non-English UI; e.g. translated Clear still selects the original sound recipe."""

        library_locale.install(self.app, "fr")
        editor = library_voice_controls.VoiceEditor()
        for button in editor._quick.findChildren(QtWidgets.QPushButton):
            button.click()
        editor.deleteLater()

    @mock.patch("yt_whisper_subs.library_window_actions.QtWidgets.QMessageBox.information")
    @mock.patch("yt_whisper_subs.library_window_actions.windows_startup.set_enabled")
    @mock.patch("yt_whisper_subs.library_window_actions.windows_startup.is_enabled", return_value=False)
    @mock.patch("yt_whisper_subs.library_window_actions.library_widgets.SettingsDialog")
    def test_saving_language_persists_without_restarting_work(self, dialog, _enabled, _startup, notice) -> None:
        """Follow the real settings save path; e.g. Italian survives a fresh catalog connection while the running UI stays English."""

        dialog.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
        dialog.return_value.values.return_value = library_widgets.SettingsValues(4, "", True, False, 50, "", ui_language="it")
        window = mock.Mock()
        window._service.db = self.db
        window._service.apply_retention.return_value = 0
        library_gui.LibraryWindow._edit_settings(window)
        reopened = library_db.LibraryDb(self.db.path)
        self.assertEqual(reopened.setting("ui_language", "en"), "it")
        self.assertEqual(i18n.language(), "en")
        notice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
