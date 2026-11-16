"""Check configurable speech without models, network requests or audible playback.

Example: switching styles must reuse approved questions and record the exact voice actually heard.
"""

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtTest
from PySide6 import QtWidgets
import shiboken6

from tests import listening_fixtures as audio
from tests import sentence_fixtures as fixtures
from yt_whisper_subs import library_db
from yt_whisper_subs import library_listening
from yt_whisper_subs import library_practice_ready
from yt_whisper_subs import library_voice_config
from yt_whisper_subs import library_voice_controls
from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices as voices
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import proc
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_service
from yt_whisper_subs import zonos_speech
from yt_whisper_subs import task_cancel


class VoiceTests(unittest.TestCase):
    """Exercise shared cache, provenance and native controls; e.g. adding a speaker never generates new sentences."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share a native event loop; e.g. asynchronous previews complete through ordinary queued signals."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        """Prepare synthetic vocabulary and two independent speech strategies; e.g. no real learner history is touched."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = library_db.LibraryDb(self.root / "catalog.sqlite3")
        self.db.initialize()
        fixtures.seed(self.db)
        self.provider = fixtures.FakeSentenceProvider()
        self.engines = {}

        def speech(_db, config):
            """Create deterministic sound per audible identity; e.g. label-only edits keep the same WAV assets."""

            key = config.identity()
            if key not in self.engines:
                engine = audio.Speech()
                engine.profile, engine.config = key, config
                self.engines[key] = engine
            return self.engines[key]

        patch = mock.patch.object(voices, "speech", side_effect=speech)
        patch.start()
        self.addCleanup(patch.stop)
        self._objects = []
        self.addCleanup(self._dispose)
        self.engine = sentence_service.SentenceService(self.db, self.provider)
        self.options = fixtures.options(direction=sentences.Direction.LISTEN)

    def _own(self, obj):
        """Track native ownership explicitly; e.g. even a hidden editor is deleted before QApplication teardown."""

        self._objects.append(obj)
        return obj

    def _dispose(self) -> None:
        """Drain workers before deleting their receiver trees; e.g. preview signals cannot survive their temporary catalog."""

        for obj in self._objects:
            if shiboken6.isValid(obj):
                obj.close() if isinstance(obj, QtWidgets.QWidget) else obj.stop()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        for obj in self._objects:
            if shiboken6.isValid(obj):
                obj.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _save(self, configs=None) -> voices.Voices:
        """Persist an ordered comparison set; e.g. disabled presets must never trigger synthesis."""

        configs = configs or [voices.Config(), voices.Config(engine="zonos", controls={"mood.clarity": .35})]
        settings = voices.Voices(presets=[voices.Preset(name=f"Voice {idx}", config=config) for idx, config in enumerate(configs)])
        self.db.set_setting(voices.SETTING, settings.model_dump_json())
        return settings

    def _wait(self, condition) -> None:
        """Wait only for the expected native state; e.g. a failed preview cannot hang a test indefinitely."""

        end = time.monotonic() + 5
        while not condition() and time.monotonic() < end:
            QtTest.QTest.qWait(10)
        self.assertTrue(condition())

    def test_each_enabled_voice_has_separate_reusable_audio_and_settings(self) -> None:
        """Reuse approved text and untouched voices on edits; e.g. a new pace replaces only that preset's sound identity."""

        settings = self._save()
        session = self.engine.start(self.options, lambda _: None)
        rows = self.db.listening_questions(session)
        self.assertTrue(self.engine.audio.ready(rows))
        assets = [self.db.listening_audio(rows[0]["id"], cfg.config.identity()) for cfg in settings.enabled]
        self.assertEqual(len({row["cache_key"] for row in assets}), 2)
        self.assertEqual(json.loads(assets[1]["settings_json"])["controls"], {"mood.clarity": .35})
        calls = sum(len(engine.calls) for engine in self.engines.values())
        settings.presets[1].name = "Renamed character"
        self.db.set_setting(voices.SETTING, settings.model_dump_json())
        self.provider.available = False
        self.engine.audio.prepare(rows, lambda _: None)
        self.assertEqual(sum(len(engine.calls) for engine in self.engines.values()), calls)
        settings.presets[1].config.pace = .8
        self.db.set_setting(voices.SETTING, settings.model_dump_json())
        self.assertFalse(self.engine.audio.ready(rows))
        self.engine.audio.prepare(rows, lambda _: None)
        self.assertTrue(self.engine.audio.ready(rows))
        self.assertEqual(sum(len(engine.calls) for engine in self.engines.values()), calls + 1)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listening_assets").fetchone()[0], 3)
        self.assertEqual(practice_activity.read(self.db).answers, 0)

    def test_disabled_voices_are_not_rendered(self) -> None:
        """Keep optional voices cheap until enabled; e.g. opening configuration does not start Zonos."""

        self.engine.start(self.options, lambda _: None)
        self.assertEqual(len(self.engines), 1)
        self.assertEqual(next(iter(self.engines.values())).config.engine, "piper")

    def test_two_voices_switch_on_the_same_draft_with_exact_play_provenance(self) -> None:
        """Drive real voice tabs without rendering again; e.g. listening twice never counts as two answers."""

        self._save()
        session = self.engine.start(self.options, lambda _: None)
        row = self.db.sentence_current(session)
        card = self._own(library_listening.ListeningCard(self.db, player=audio.Player(), audio_cache=lambda: self.engine.audio))
        card.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        card.present(row)
        card.show()
        self._wait(lambda: bool(card._player.calls))
        card.editor.setPlainText("Mijn unfinished draft")
        card._voices.setCurrentIndex(1)
        self.assertEqual(len(card._player.calls), 2)
        self.assertEqual(card.editor.toPlainText(), "Mijn unfinished draft")
        self.assertNotEqual(card._player.calls[0][0], card._player.calls[1][0])
        with self.db._connect() as conn:
            plays = conn.execute("SELECT a.settings_json FROM listening_plays p JOIN listening_assets a USING(cache_key) ORDER BY p.id").fetchall()
        self.assertEqual([json.loads(item[0])["engine"] for item in plays], ["piper", "zonos"])
        self.assertEqual(practice_activity.read(self.db).answers, 0)

    def test_upgrade_retains_old_piper_links_and_plays(self) -> None:
        """Upgrade a real single-voice table with history; e.g. adding profiles cannot orphan assistance events."""

        session = self.engine.start(self.options, lambda _: None)
        row = self.db.sentence_current(session)
        cached = self.db.listening_audio(row["question_id"])
        self.db.listening_play(row["id"], "normal", cache_key=cached["cache_key"])
        with self.db._connect() as conn:
            conn.execute("ALTER TABLE listening_assets DROP COLUMN settings_json")
            conn.execute("ALTER TABLE listening_audio RENAME TO old_links")
            conn.execute("CREATE TABLE listening_audio(question_id INTEGER PRIMARY KEY REFERENCES sentence_questions(id),"
                         "cache_key TEXT NOT NULL REFERENCES listening_assets(cache_key))")
            conn.execute("INSERT INTO listening_audio SELECT * FROM old_links")
            conn.execute("DROP TABLE old_links")
        self.db.initialize()
        self.assertEqual(self.db.listening_audio(row["question_id"])["cache_key"], cached["cache_key"])
        self._save()
        self.engine.audio.prepare(self.db.listening_questions(session), lambda _: None)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listening_plays").fetchone()[0], 1)
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_complete_editor_roundtrip_and_starting_styles(self) -> None:
        """Expose every published control with editable starting styles; e.g. duplicate a voice without losing its accents or seed."""

        editor = self._own(library_voice_controls.VoiceEditor())
        cfg = voices.Config(engine="zonos", voice="Imogen", pace=.85, volume=.7, seed=22,
                            controls={"mood.clarity": .35, "accents_en.irish": .25, "identity.age": -.5})
        editor.load(cfg)
        self.assertEqual(editor.config(), cfg)
        self.assertEqual(len(editor._strengths), 65)
        self.assertEqual(editor._voice.count(), 27)
        editor._style("Clear")
        self.assertEqual(editor.config().controls, {"mood.clarity": .35, "mood.articulation": .2})
        self.assertEqual(editor.config().voice, "Imogen")
        editor.neutral()
        self.assertFalse(editor.config().controls)

    def test_duplicate_save_and_cancel_are_independent(self) -> None:
        """Persist only explicit Save with a usable enabled set; e.g. an auditioned adjustment can be cancelled."""

        original = self.db.setting(voices.SETTING, "")
        dialog = self._own(library_voice_config.VoiceDialog(self.db))
        dialog._duplicate()
        dialog._editor._pace.setValue(85)
        self.assertEqual(dialog._presets[0].config.pace, 1)
        dialog.reject()
        self.assertEqual(self.db.setting(voices.SETTING, ""), original)
        saved = self._own(library_voice_config.VoiceDialog(self.db))
        saved._name.setText("My Piper")
        saved._save()
        self.assertEqual(voices.read(self.db).enabled[0].name, "My Piper")
        for idx in range(saved._list.count()):
            saved._list.item(idx).setCheckState(QtCore.Qt.CheckState.Unchecked)
        saved._save()
        self.assertIn("Enable at least one voice", saved._status.text())

    def test_custom_reference_content_changes_audio_identity(self) -> None:
        """Fingerprint content rather than its filename; e.g. replacing a local sample cannot reuse an old cloned voice."""

        path = self.root / "reference.wav"
        path.write_bytes(b"one")
        cfg = voices.Config(engine="zonos", reference=str(path))
        before = cfg.identity()
        path.write_bytes(b"two")
        self.assertNotEqual(cfg.identity(), before)
        with self.assertRaises(ValueError):
            voices.Config(controls={"emotions.happy": -.5})

    def test_overall_expression_keeps_the_mix_and_cache_identity_matches_its_sound(self) -> None:
        """Scale controls through one canonical calculation; e.g. half-strength expression equals manually halving the individual sliders."""

        cfg = voices.Config(engine="zonos", controls={"mood.clarity": 1, "identity.gender": -1}, expression=.5)
        same = voices.Config(engine="zonos", controls={"mood.clarity": .5, "identity.gender": -.5})
        self.assertEqual(cfg.identity(), same.identity())
        self.assertEqual(cfg.controls["mood.clarity"], 1)
        editor = self._own(library_voice_controls.VoiceEditor())
        editor.load(cfg)
        self.assertEqual(editor.config(), cfg)
        editor._expression.setValue(25)
        self.assertEqual(editor.config().steering()["mood.clarity"], .25)
        editor._strengths["identity.gender"].set_value(-.1725)
        self.assertEqual(editor.config().controls["identity.gender"], -.1725)

    def test_targeted_preparation_ignores_another_broken_preset(self) -> None:
        """Let a requested voice recover independently; e.g. an experimental preset cannot block a working Piper sentence."""

        settings = self._save()
        session = self.engine.start(self.options, lambda _: None)
        rows = self.db.listening_questions(session)
        good, bad = [self.engine.audio.engine(row.config) for row in settings.enabled]
        bad.available = False
        asset = self.db.listening_audio(rows[0]["id"], good.profile)
        self.db.listening_paths(asset["cache_key"])["normal"].unlink()
        with mock.patch.object(bad, "render_many", side_effect=AssertionError("unrelated voice was requested")):
            self.engine.audio.prepare(rows[:1], lambda _: None, preset=settings.enabled[0])
        self.assertTrue(self.db.listening_audio_valid(self.db.listening_audio(rows[0]["id"], good.profile)))

    def test_runaway_recording_names_the_speed_and_recommends_the_expression_control(self) -> None:
        """Reject a native success exit without a speech end token; e.g. a 30-second runaway slow clip never becomes cached speech."""

        engine = zonos_speech.ZonosSpeech(voices.Config(engine="zonos"))
        failed = subprocess.CompletedProcess([], 0, stdout="tts: 450 frames, eos_frame=-1")
        with mock.patch.object(zonos_speech.zonos_install, "installation", return_value={}), \
             mock.patch.object(zonos_speech.zonos_install, "root", return_value=self.root), \
             mock.patch.object(engine, "_command", return_value=["zonos"]), \
             mock.patch.object(zonos_speech.proc, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "slow recording.*Overall expression"):
                engine.render_many([("public sample", {"slow": self.root / "slow.wav"})], lambda _: None)
        self.assertFalse((self.root / "slow.wav").exists())

    def test_switching_away_from_a_failing_voice_recovers_the_requested_voice(self) -> None:
        """Finish the latest playback request after an earlier voice fails; e.g. an unavailable experimental mix cannot strand Piper."""

        settings = self._save()
        session = self.engine.start(self.options, lambda _: None)
        row = self.db.sentence_current(session)
        first, second = [self.engine.audio.engine(preset.config) for preset in settings.enabled]
        for engine in (first, second):
            asset = self.db.listening_audio(row["question_id"], engine.profile)
            self.db.listening_paths(asset["cache_key"])["normal"].unlink()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fail(_jobs, _report):
            """Hold the first request until another voice is selected; e.g. real Qt callback ordering must recover the newer request."""

            entered.set()
            release.wait(3)
            raise RuntimeError("Expression mix did not finish")

        card = self._own(library_listening.ListeningCard(self.db, player=audio.Player(), audio_cache=lambda: self.engine.audio))
        card.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        with mock.patch.object(first, "render_many", side_effect=fail):
            card.present(row)
            card.show()
            self._wait(entered.is_set)
            card._voices.setCurrentIndex(1)
            release.set()
            self._wait(lambda: bool(card._player.calls))
        asset = self.db.listening_audio(row["question_id"], second.profile)
        self.assertEqual(card._player.calls[-1][0], self.db.listening_paths(asset["cache_key"])["normal"])

    def test_native_controls_are_calibrated_and_do_not_request_missing_release_bundle(self) -> None:
        """Build supported native arguments only; e.g. the published bundle has no temporal steering release data."""

        cfg = voices.Config(engine="zonos", controls={"mood.clarity": .5}, volume=.8)
        files = {name: name for name in ("gguf/backbone-ptq.gguf", "gguf/zac-keep2.gguf", "gguf/steer.gguf")}
        voice = next(item for item in voices.catalog()["voices"] if item["name"] == cfg.voice)
        files[voice["file"]] = "voice.npy"
        command = zonos_speech.ZonosSpeech(cfg)._command({"binary": "zonos", "files": files}, "Public sample", Path("test.wav"), "slow")
        self.assertIn("mood.clarity=1", command)
        self.assertIn("delivery.tempo=-0.6", command)
        self.assertNotIn("--steer-hold", command)

    def test_native_capture_never_prints_hidden_script(self) -> None:
        """Capture diagnostics privately instead of echoing a spoken answer; e.g. a hidden Dutch sentence stays out of progress logs."""

        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result = proc.run([os.sys.executable, "-c", "print('concealed script')"], capture_stdout=True, stream_stdout=True, quiet=True)
        self.assertIn("concealed script", result.stdout)
        self.assertNotIn("concealed script", output.getvalue())

    def test_readiness_repairs_added_voices_on_an_unopened_round(self) -> None:
        """Warm existing sessions as well as standby stock; e.g. a saved voice edit cannot leave an unopened round falsely ready."""

        session = self.engine.start(self.options, lambda _: None)
        track = practice_tracks.Track.LISTENING
        ready = self._own(library_practice_ready.PracticeReady(self.db, modes={"sentence": (self.options, self.engine)}))
        self.assertFalse(ready._needed(track, self.options, self.engine))
        self._save()
        self.assertTrue(ready._needed(track, self.options, self.engine))
        self.provider.available = False
        ready.prepare(lambda _: None)
        self.assertEqual(self.db.sentence_active("listen_nl"), session)
        self.assertTrue(self.engine.audio.ready(self.db.listening_questions(session)))

    def test_reordering_and_removing_presets_keeps_each_configuration(self) -> None:
        """Avoid intermediate list-selection writes; e.g. moving a row cannot copy its sliders into another speaker."""

        self._save([voices.Config(pace=.8), voices.Config(pace=1.2), voices.Config(volume=.5)])
        dialog = self._own(library_voice_config.VoiceDialog(self.db))
        dialog._list.setCurrentRow(0)
        dialog._move(1)
        self.assertEqual([row.config.pace for row in dialog._presets], [1.2, .8, 1])
        dialog._remove()
        self.assertEqual([row.config.pace for row in dialog._presets], [1.2, 1])
        self.assertEqual(dialog._presets[1].config.volume, .5)
        dialog.reject()

    def test_preview_is_cached_separate_from_practice_and_cannot_play_after_closure(self) -> None:
        """Use public sample text without learner events; e.g. cancelling a dialog suppresses even an already queued completion."""

        with mock.patch.object(library_listening, "WavePlayer", audio.Player), mock.patch.object(library_voice_config.zonos_install, "root", return_value=self.root):
            dialog = self._own(library_voice_config.VoiceDialog(self.db))
            dialog._audition()
            self._wait(lambda: bool(dialog._player.calls))
            engine = next(iter(self.engines.values()))
            self.assertNotEqual(engine.calls[0][0], fixtures.DUTCH)
            calls = len(engine.calls)
            dialog._audition()
            self._wait(lambda: len(dialog._player.calls) == 2)
            self.assertEqual(len(engine.calls), calls)
            dialog.reject()
            dialog._previewed({})
            self.assertEqual(len(dialog._player.calls), 2)
        self.assertEqual(practice_activity.read(self.db).answers, 0)

    def test_cancelling_a_native_stream_stops_its_process(self) -> None:
        """Exercise real cancellation rather than a cooperative fake; e.g. closing a preview cannot leave the speech process working."""

        control, errors = task_cancel.TaskControl(), []
        ready = self.root / "started.txt"
        code = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(60)"

        def work():
            """Enter the same task scope as GUI workers; e.g. cancellation reaches the native child through registered hooks."""

            try:
                with task_cancel.activate(control):
                    proc.run([os.sys.executable, "-c", code, ready], capture_stdout=True, stream_stdout=True, quiet=True)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._wait(ready.is_file)
        control.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], task_cancel.CancelledError)


if __name__ == "__main__":
    unittest.main()
