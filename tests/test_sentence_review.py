"""Verify precise correction highlighting and native hover feedback.

Example: repeated zekerder errors highlight correctly even after emoji, without marking the valid word zeker.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_sentence_review as review


class SentenceReviewTests(unittest.TestCase):
    """Inspect actual QTextCursor ranges and tooltips; e.g. styling never rewrites stored text or reveals a fresh answer."""

    @classmethod
    def setUpClass(cls):
        """Share the native event loop; e.g. tooltip hit testing uses real Qt text layout."""

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_precise_repeated_unicode_spans_and_hover_correction(self):
        """Use audited evidence and UTF-16 offsets; e.g. emoji before two errors cannot shift the highlighted characters."""

        editor = review.ReviewEdit()
        self.addCleanup(editor.close)
        editor.resize(1000, 180)
        editor.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        editor.show()
        text = "🙂 zekerder, zekerder; zeker en zekerdere blijven gewoon."
        editor.setPlainText(text)
        editor.setReadOnly(True)
        grade = {"errors": [{"evidence": "zekerder", "correction": "veiliger", "explanation": "Use safe, rather than certain."}]}
        editor.annotate(grade, "en_nl")
        self.app.processEvents()
        selections = editor.extraSelections()
        self.assertEqual([row.cursor.selectedText() for row in selections], ["zekerder", "zekerder"])
        self.assertEqual(editor.toPlainText(), text)
        cursor = selections[0].cursor
        cursor.setPosition(cursor.selectionStart() + 2)
        point = editor.cursorRect(cursor).center()
        event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, point, editor.viewport().mapToGlobal(point))
        self.app.sendEvent(editor.viewport(), event)
        self.assertIn("veiliger", QtWidgets.QToolTip.text())
        self.assertIn("rather than certain", QtWidgets.QToolTip.text())
        editor.setPlainText("Een nieuw antwoord")
        self.assertEqual(editor.extraSelections(), [])

    def test_vocabulary_metadata_supplies_directional_hover_and_omissions_stay_unmarked(self):
        """Reuse existing lexical evidence safely; e.g. reverse practice shows English while an omission never invents a typed span."""

        grade = {"errors": [{"evidence": "", "correction": "the", "explanation": "An omitted article."}],
                 "vocabulary": [{"typed": "certain", "dutch": ["veiliger"], "english": ["safer"], "note": "Safety differs from certainty."}]}
        spans = review.marks("The road is certain.", grade, "nl_en")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].correction, "safer")

    def test_source_highlights_use_bilingual_evidence_in_both_directions(self):
        """Highlight corresponding source concepts without guessing translations; e.g. safer links to veiliger even for supporting vocabulary."""

        content = {"english": "A safer road.", "dutch": "Een veiliger pad.",
                   "words": [{"word_id": 1, "dutch_forms": ["veiliger"], "english_forms": ["safer"]}]}
        for direction, source, corrected in (("en_nl", "english", "veiliger"), ("nl_en", "dutch", "safer")):
            with self.subTest(direction=direction):
                grade = {"errors": [{"word_ids": [1], "correction": corrected, "explanation": "Safety differs from certainty."}],
                         "vocabulary": []}
                row = {"direction": direction, "content": content, "grade": grade}
                spans = review.source_marks(row)
                expected = "safer" if direction == "en_nl" else "veiliger"
                self.assertEqual([content[source][mark.start:mark.end] for mark in spans], [expected])
                self.assertEqual(spans[0].correction, corrected)

    def test_source_annotations_are_literal_and_clear_before_the_next_question(self):
        """Render safe local hover links only after feedback; e.g. original angle brackets stay literal and no correction leaks into the next task."""

        label = review.SourceLabel()
        self.addCleanup(label.close)
        source = "A <safer> & calmer road."
        label.setText(source)
        row = {"direction": "en_nl", "content": {"english": source, "words": []},
               "grade": {"errors": [{"source_forms": ["safer"], "word_ids": [], "correction": "veiliger", "explanation": "Use the safety concept."}]}}
        label.annotate(row)
        document = QtGui.QTextDocument()
        document.setHtml(label.text())
        self.assertEqual(document.toPlainText(), source)
        self.assertIn('href="error:0"', label.text())
        label.linkHovered.emit("error:0")
        self.assertIn("veiliger", QtWidgets.QToolTip.text())
        label.setText("Another sentence <unchanged>.")
        self.assertEqual(label.text(), "Another sentence <unchanged>.")
        self.assertEqual(label.textFormat(), QtCore.Qt.TextFormat.PlainText)

    def test_tooltip_merges_context_and_dictionary_duplicates_with_larger_correction(self):
        """Show the contextual form once; e.g. safer/certain feedback must not repeat veiliger and the same contrast as two separate blocks."""

        matching = [review.Mark(start=0, end=8, correction="veiliger", explanation="Zekerder means more certain; veiliger means safer.", kind="meaning"),
                    review.Mark(start=0, end=8, correction="veiliger", explanation="Zeker means certain; veilig means safe.", kind="vocabulary")]
        document = QtGui.QTextDocument()
        document.setHtml(review.tooltip_html(matching))
        text = document.toPlainText()
        self.assertIn("veiliger\nZekerder", text)
        self.assertIn("Zekerder means more certain", text)
        self.assertNotIn("Zeker means certain", text)
        headings = []
        block = document.begin()
        while block.isValid():
            cursor = block.begin()
            while not cursor.atEnd():
                fragment = cursor.fragment()
                if fragment.charFormat().foreground().color().name() == "#83e5af":
                    headings.append(fragment.text())
                    self.assertGreaterEqual(fragment.charFormat().fontPointSize(), 18)
                cursor += 1
            block = block.next()
        self.assertEqual(headings, ["veiliger"])

    def test_tooltip_prefers_full_corrected_phrase_over_repeated_dictionary_word(self):
        """Merge a word with its contextual corrected phrase; e.g. small bill needs one kleine wetsvoorstelletje heading and one explanation."""

        matching = [review.Mark(start=0, end=10, correction="kleine wetsvoorstelletje", kind="meaning",
                               explanation="A wet is a law; a wetsvoorstel is a bill or proposed law."),
                    review.Mark(start=0, end=10, correction="wetsvoorstelletje", kind="vocabulary",
                               explanation="A wet is a law; a wetsvoorstel is a proposed law or bill.")]
        rendered = review.tooltip_html(matching)
        self.assertEqual(rendered.count("font-size:18pt"), 1)
        self.assertIn("kleine wetsvoorstelletje", rendered)
        self.assertNotIn("proposed law or bill", rendered)

    def test_tooltip_keeps_independent_errors_under_one_corrected_form(self):
        """Remove repeated headings while retaining distinct lessons; e.g. agreement and word order can both matter for werkt."""

        matching = [review.Mark(start=0, end=4, correction="werkt", kind="agreement", explanation="Add -t for jij."),
                    review.Mark(start=0, end=4, correction="werkt", kind="word_order", explanation="Place the finite verb second.")]
        rendered = review.tooltip_html(matching)
        self.assertEqual(rendered.count("font-size:18pt"), 1)
        self.assertIn("Add -t for jij.", rendered)
        self.assertIn("Place the finite verb second.", rendered)
