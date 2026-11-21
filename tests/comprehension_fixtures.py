"""Supply offline Dutch passages with independently controlled audits and grades.

Example: partial understanding and a veilig/zeker confusion exercise real SQLite and shared FSRS.
"""

from yt_whisper_subs import comprehension as comp


PARAGRAPHS = [
    'De slimste buurman zou eventueel naar de fietsen lopen. De gemeente wil minder verkeer in de wijk, maar de bewoners vinden dat goede afspraken belangrijker zijn dan een snelle beslissing. Het besluit is nog niet definitief.',
    'Tijdens de vergadering vertelde Noor dat de proef alleen op zaterdagen zou gelden. Ondernemers vrezen minder klanten, terwijl enkele bewoners juist verwachten dat mensen langer blijven. Daarom vraagt de gemeente eerst om ervaringen voordat zij verder beslist.',
    'Na drie maanden willen de onderzoekers de resultaten bespreken. Een rustigere straat betekent volgens hen niet automatisch dat iedereen tevreden is. Ook de bereikbaarheid en de ervaringen van oudere bewoners moeten worden onderzocht voordat de proef kan worden uitgebreid.',
]


def options(**values):
    """Use a small complete mixed round; e.g. two choices and one open response accompany three paragraphs."""

    return comp.Options(**dict(dict(paragraphs=3, mc=2, open=1, words=3, include_early=True), **values))


class Provider:
    """Count requests while preserving production schemas; e.g. a restart must reuse an unfinished audit rather than generate another story."""

    model, reasoning = 'fixture-comprehension', 'low'

    def __init__(self):
        """Control failures at the actual provider boundaries; e.g. an audio outage is independent from generation availability."""

        self.generated, self.audited, self.graded = [], [], []
        self.repaired = []
        self.available, self.audit_available = True, True
        self.reject_first, self.omit_question = False, False
        self.grade_result = None
        self.verify_available = True
        self.accepted = None

    def generate(self, document):
        """Create requested counts with grounded exact evidence; e.g. all target forms occur in a naturally inflected sentence."""

        self.generated.append(document)
        if not self.available:
            raise OSError('Provider unavailable')
        opts = document['options']
        paragraphs = [PARAGRAPHS[i % 3] for i in range(opts['paragraphs'])]
        forms = dict(slim=['slimste'], fiets=['fietsen'], lopen=['lopen'])
        targets = [comp.Target(word_id=w['word_id'], forms=forms[w['lemma']]) for w in document['words']]
        questions = []
        count = opts['mc'] + opts['open'] - int(self.omit_question)
        for idx in range(count):
            multiple = idx < opts['mc']
            choices = [comp.Choice(text=text, explanation=note) for text, note in (
                ('Omdat iedereen de proef al heeft goedgekeurd.', 'De bewoners verschillen nog van mening.'),
                ('Om eerst de ervaringen met minder verkeer te onderzoeken.', 'De gemeente vraagt eerst ervaringen voordat zij beslist.'),
                ('Omdat de onderzoekers geen gegevens meer nodig hebben.', 'Er moeten nog resultaten worden verzameld.'),
                ('Om onmiddellijk alle straten definitief te veranderen.', 'Het besluit is juist nog niet definitief.'))] if multiple else []
            questions.append(comp.Question(key=idx + 1, kind='mc' if multiple else 'open', skill='cause' if multiple else 'inference',
                prompt=f"Waarom kiest de gemeente voor een proef in plaats van een definitief besluit? Vraag {idx + 1}.",
                choices=choices, correct_idx=1 if multiple else None,
                answer='De gemeente wil minder verkeer, maar het besluit is nog niet definitief.',
                rubric=[] if multiple else [comp.Criterion(key='doel', text='Noemt minder verkeer.'), comp.Criterion(key='voorbehoud', text='Noemt het voorlopige karakter.')],
                evidence=[comp.Evidence(paragraph=1, quote='Het besluit is nog niet definitief.')], word_ids=[targets[0].word_id]))
        return comp.Passage(title=f'Een proef in de wijk {len(self.generated)}', genre='article' if opts['genre'] == 'mixed' else opts['genre'],
                            paragraphs=paragraphs, targets=targets, questions=questions)

    def audit(self, passage, level):
        """Return controlled independent judgements; e.g. the first ambiguity can trigger exactly one repaired draft."""

        self.audited.append(passage)
        if not self.audit_available:
            raise OSError('Auditor unavailable')
        flag = self.reject_first and len(self.audited) == 1
        return comp.Audit(natural_dutch=True, coherent=True, level_issue='', questions=[
            comp.AuditQuestion(key=q.key, valid=not (flag and q.key == 1), answer_idx=q.correct_idx,
                               explanation='Vraag 2 verraadt het antwoord.' if flag and q.key == 1 else 'Eenduidig onderbouwd.')
            for q in passage.questions])

    def repair_questions(self, passage, rejected, level):
        """Mark the replacement while leaving the text and other questions unchanged."""

        self.repaired.append(rejected)
        questions = [q.model_copy(update={'prompt': f'Herziene vraag {q.key} over de tekst?'})
                     for q in passage.questions if any(item.key == q.key for item in rejected)]
        return comp.QuestionRepairs(questions=questions).apply(passage, rejected)

    def grade(self, document):
        """Award one of two content points with separate language status; e.g. a missing qualification gives genuine partial credit."""

        self.graded.append(document)
        if not self.available:
            raise OSError('Grader unavailable')
        if self.grade_result:
            return self.grade_result
        q = document['question']
        return comp.Grade(confident=True, criteria=[comp.CriterionResult(key=c['key'], met=i == 0, explanation='Wel genoemd.' if i == 0 else 'Dit voorbehoud ontbreekt.') for i, c in enumerate(q['rubric'])],
            language_correct=True, corrected='De gemeente wil minder verkeer, maar het besluit is nog niet definitief.',
            corrected_english='The council wants less traffic, but the decision is not final yet.', explanation='Het doel klopt; het voorlopige karakter ontbreekt.',
            words=[comp.WordResult(word_id=wid, correct=None, explanation='Deze reactie bewijst geen afzonderlijke woordkennis.') for wid in q['word_ids']], errors=[], vocabulary=[])

    def verify(self, document, vocabulary):
        """Control independent dictionary acceptance; e.g. a rejected real-word claim never becomes vocabulary."""

        if not self.verify_available:
            raise OSError('Lexical verifier unavailable')
        return set(range(len(vocabulary))) if self.accepted is None else self.accepted
