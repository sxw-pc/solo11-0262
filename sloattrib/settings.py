DEMO_MODULES = ('slo', 'cause', 'credit')
MODULES = (*DEMO_MODULES, 'custody')
DEMO = [(name, f'fixtures/{name}_normal.json') for name in DEMO_MODULES] + [
    ('slo', 'fixtures/slo_failure.json'),
    ('cause', 'fixtures/cause_failure.json'),
    ('credit', 'fixtures/credit_failure.json'),
    ('credit', 'fixtures/path.json'),
]
