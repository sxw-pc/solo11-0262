MODULES = ('slo', 'cause', 'credit')
DEMO = [(name, f'fixtures/{name}_normal.json') for name in MODULES] + [
    ('slo', 'fixtures/slo_failure.json'),
    ('cause', 'fixtures/cause_failure.json'),
    ('credit', 'fixtures/credit_failure.json'),
    ('credit', 'fixtures/path.json'),
]
