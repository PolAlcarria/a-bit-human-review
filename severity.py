"""The severities a finding can have, the letter its id starts with, its label, and the words reviews use for each.

Shared by the markdown parser, Claude's extraction of findings, the re-review, the side chat and the hand-off summary.
"""

SEVERITIES = ('critical', 'warning', 'suggestion', 'question', 'finding')
LETTER = {'critical': 'C', 'warning': 'W', 'suggestion': 'S', 'question': 'Q', 'finding': 'F'}
LETTER_SEVERITY = {'C': 'critical', 'W': 'warning', 'S': 'suggestion', 'Q': 'question'}
LABEL = {'critical': 'Critical', 'warning': 'Warning', 'suggestion': 'Suggestion', 'question': 'Question',
         'finding': 'Finding'}
SEVERITY_WORDS = {
    'critical': 'critical', 'blocking': 'critical', 'blocker': 'critical', 'blockers': 'critical',
    'must-fix': 'critical', 'mustfix': 'critical', 'must fix': 'critical', 'must-fixes': 'critical', 'high': 'critical',
    'warning': 'warning', 'warnings': 'warning', 'should-fix': 'warning', 'should fix': 'warning',
    'shouldfix': 'warning', 'should-fixes': 'warning', 'major': 'warning', 'medium': 'warning',
    'suggestion': 'suggestion', 'suggestions': 'suggestion', 'nice-to-have': 'suggestion',
    'nice to have': 'suggestion', 'nice-to-haves': 'suggestion', 'nit': 'suggestion', 'nits': 'suggestion',
    'minor': 'suggestion', 'low': 'suggestion', 'optional': 'suggestion',
    'question': 'question', 'questions': 'question', 'ask': 'question', 'asks': 'question',
}
