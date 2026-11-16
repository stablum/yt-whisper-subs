"""Show Dutch comprehension trends, partial credit and shared word evidence.

Example: an improving content score remains visible before every complete answer becomes correct.
"""

import json
from datetime import datetime

from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_labels as labels
from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_stats
from yt_whisper_subs import library_practice_progress as charts
from yt_whisper_subs import library_practice_tables


def percent(value) -> str:
    """Display missing observations honestly; e.g. no graded answers gives an em dash rather than zero percent."""

    return '—' if value is None else i18n.number(value, '.0%')


class Progress(QtWidgets.QWidget):
    """Keep understanding and Dutch writing denominators visible; e.g. an article error does not lower content comprehension."""

    def __init__(self, db, direction) -> None:
        """Construct read-only charts without preparing exercises; e.g. opening tracking never spends model tokens."""

        super().__init__()
        self._db, self._direction = db, comp.Direction(direction)
        self._totals = QtWidgets.QLabel()
        self._totals.setWordWrap(True)
        self._totals.setObjectName('practiceStats')
        self._score = charts.TrendChart(i18n.tr('Comprehension · partial credit'), True, empty_text=i18n.tr('Your practice history will appear here'))
        self._activity = charts.TrendChart(i18n.tr('Questions graded per day'), False, empty_text=i18n.tr('Your practice history will appear here'))
        self._tables = library_practice_tables.StatsTables({
            i18n.mark('Answers'): [i18n.tr('Date'), i18n.tr('Type'), i18n.tr('Skill'), i18n.tr('Comprehension'), i18n.tr('Language correct'), i18n.tr('Question'), i18n.tr('Your answer')],
            i18n.mark('Skills'): [i18n.tr('Skill'), i18n.tr('Graded'), i18n.tr('Fully correct'), i18n.tr('Comprehension')],
            i18n.mark('Words'): [i18n.tr('Word'), i18n.tr('Direction'), i18n.tr('Graded'), i18n.tr('Mistakes'), i18n.tr('Correct'), i18n.tr('Next review')],
            i18n.mark('Word confusions'): [i18n.tr('Date'), i18n.tr('Typed'), i18n.tr('Correction'), i18n.tr('Explanation')],
            i18n.mark('Rounds'): [i18n.tr('Started'), i18n.tr('Status'), i18n.tr('Questions'), i18n.tr('Submitted'), i18n.tr('Comprehension')],
        })
        refresh = QtWidgets.QPushButton(i18n.tr('Refresh statistics'))
        refresh.clicked.connect(self.refresh)
        plots = QtWidgets.QHBoxLayout()
        plots.addWidget(self._score)
        plots.addWidget(self._activity)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addLayout(plots)
        layout.addWidget(self._tables, 1)
        layout.addWidget(refresh)

    def refresh(self) -> None:
        """Derive every figure from submitted history; e.g. pending and reported questions never become scored failures."""

        data = comprehension_stats.snapshot(self._db, self._direction.value)
        self._totals.setText(i18n.tr(
            '{label} · comprehension {percent} · {correct}/{graded} fully correct\n{submitted} '
            'submitted · {pending} pending · language correct {percent7} ({language_answers} open '
            'answers) · {plays} clips played',
            label=self._direction.label,
            percent=percent(data['score']),
            correct=data['correct'],
            graded=data['graded'],
            submitted=data['submitted'],
            pending=data['pending'],
            percent7=percent(data['language']),
            language_answers=data['language_answers'],
            plays=data['plays'],
        ))
        for chart, metric in ((self._score, 'score'), (self._activity, 'answers')):
            series = []
            for kind, label, color in (('mc', i18n.tr('Multiple choice'), '#ffd166'), ('open', i18n.tr('Open questions'), '#83e5af')):
                points = [(r['day'], r[metric] * (100 if metric == 'score' else 1), i18n.tr('{answers} graded · comprehension {score:.0%}', answers=r['answers'], score=r['score']))
                          for r in data['daily'] if r['kind'] == kind]
                series.append(dict(label=label, color=color, points=points))
            chart.set_series(series)
        self._tables.fill('Answers', [[i18n.date_text(datetime.fromtimestamp(r['answered_at']), '%d-%m %H:%M'),
                          i18n.tr('Multiple choice') if r['kind'] == 'mc' else i18n.tr('Open'), i18n.tr(comp.SKILLS[r['skill']]), percent(r['score']),
                          i18n.tr('Reported') if r['outcome'] == 'invalid' else '—' if r['language_correct'] is None else i18n.tr('Yes') if r['language_correct'] else i18n.tr('No'), r['prompt'], r['answer_text']] for r in data['answers']])
        self._tables.fill('Skills', [[i18n.tr(comp.SKILLS[r['skill']]), r['answers'], r['correct'], percent(r['score'])] for r in data['skills']])
        self._tables.fill('Words', [[r['lemma'], 'NL → EN' if r['direction'] == 'nl_en' else 'EN → NL', r['assessed'], r['failures'],
                          percent(r['accuracy']), i18n.date_text(datetime.fromtimestamp(r['due_at']), '%d-%m %H:%M') if r['due_at'] else '—'] for r in data['words']])
        errors = []
        for row in data['errors']:
            fix = json.loads(row['body_json'])
            errors.append([i18n.date_text(datetime.fromtimestamp(row['answered_at']), '%d-%m %H:%M'), fix['typed'], ' … '.join(fix['dutch']), fix['note']])
        self._tables.fill('Word confusions', errors)
        self._tables.fill('Rounds', [[i18n.date_text(datetime.fromtimestamp(r['created_at']), '%d-%m %H:%M'), labels.term(r['status']), r['planned'], r['submitted'], percent(r['score'])] for r in data['rounds']])
