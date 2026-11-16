"""Show Dutch comprehension trends, partial credit and shared word evidence.

Example: an improving content score remains visible before every complete answer becomes correct.
"""

import json
from datetime import datetime

from PySide6 import QtWidgets

from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import comprehension_stats
from yt_whisper_subs import library_practice_progress as charts
from yt_whisper_subs import library_practice_tables


def percent(value) -> str:
    """Display missing observations honestly; e.g. no graded answers gives an em dash rather than zero percent."""

    return '—' if value is None else f'{value:.0%}'


class Progress(QtWidgets.QWidget):
    """Keep understanding and Dutch writing denominators visible; e.g. an article error does not lower content comprehension."""

    def __init__(self, db, direction) -> None:
        """Construct read-only charts without preparing exercises; e.g. opening tracking never spends model tokens."""

        super().__init__()
        self._db, self._direction = db, comp.Direction(direction)
        self._totals = QtWidgets.QLabel()
        self._totals.setWordWrap(True)
        self._totals.setObjectName('practiceStats')
        self._score = charts.TrendChart('Begrip · deelpunten', True, empty_text='Je oefengeschiedenis verschijnt hier.')
        self._activity = charts.TrendChart('Beoordeelde vragen per dag', False, empty_text='Je oefengeschiedenis verschijnt hier.')
        self._tables = library_practice_tables.StatsTables({
            'Antwoorden': ['Datum', 'Soort', 'Vaardigheid', 'Begrip', 'Taal correct', 'Vraag', 'Jouw antwoord'],
            'Vaardigheden': ['Vaardigheid', 'Beoordeeld', 'Volledig goed', 'Begrip'],
            'Woorden': ['Woord', 'Richting', 'Beoordeeld', 'Fouten', 'Goed', 'Volgende herhaling'],
            'Woordverwarringen': ['Datum', 'Getypt', 'Correctie', 'Uitleg'],
            'Rondes': ['Begonnen', 'Status', 'Vragen', 'Ingediend', 'Begrip'],
        })
        refresh = QtWidgets.QPushButton('Statistieken vernieuwen')
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
        self._totals.setText(f"{self._direction.label} · begrip {percent(data['score'])} · {data['correct']}/{data['graded']} volledig goed\n"
                             f"{data['submitted']} ingediend · {data['pending']} wachtend · taal correct {percent(data['language'])} "
                             f"({data['language_answers']} open antwoorden) · {data['plays']} beluisterde fragmenten")
        for chart, metric in ((self._score, 'score'), (self._activity, 'answers')):
            series = []
            for kind, label, color in (('mc', 'Meerkeuze', '#ffd166'), ('open', 'Open vragen', '#83e5af')):
                points = [(r['day'], r[metric] * (100 if metric == 'score' else 1), f"{r['answers']} beoordelingen · begrip {r['score']:.0%}")
                          for r in data['daily'] if r['kind'] == kind]
                series.append(dict(label=label, color=color, points=points))
            chart.set_series(series)
        self._tables.fill('Antwoorden', [[datetime.fromtimestamp(r['answered_at']).strftime('%d-%m %H:%M'),
                          'Meerkeuze' if r['kind'] == 'mc' else 'Open', comp.SKILLS[r['skill']], percent(r['score']),
                          'Gemeld' if r['outcome'] == 'invalid' else '—' if r['language_correct'] is None else 'Ja' if r['language_correct'] else 'Nee', r['prompt'], r['answer_text']] for r in data['answers']])
        self._tables.fill('Vaardigheden', [[comp.SKILLS[r['skill']], r['answers'], r['correct'], percent(r['score'])] for r in data['skills']])
        self._tables.fill('Woorden', [[r['lemma'], 'NL → EN' if r['direction'] == 'nl_en' else 'EN → NL', r['assessed'], r['failures'],
                          percent(r['accuracy']), datetime.fromtimestamp(r['due_at']).strftime('%d-%m %H:%M') if r['due_at'] else '—'] for r in data['words']])
        errors = []
        for row in data['errors']:
            fix = json.loads(row['body_json'])
            errors.append([datetime.fromtimestamp(row['answered_at']).strftime('%d-%m %H:%M'), fix['typed'], ' … '.join(fix['dutch']), fix['note']])
        self._tables.fill('Woordverwarringen', errors)
        states = dict(active='Bezig', completed='Afgerond', stopped='Gestopt', ready='Voorbereid')
        self._tables.fill('Rondes', [[datetime.fromtimestamp(r['created_at']).strftime('%d-%m %H:%M'), states[r['status']], r['planned'], r['submitted'], percent(r['score'])] for r in data['rounds']])
