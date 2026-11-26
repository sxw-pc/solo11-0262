"""Expose named simulated device pages to storage users."""
from .flash import page
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS flash_bindings(
    name TEXT PRIMARY KEY, block_id TEXT NOT NULL REFERENCES flash_blocks(id),
    page_index INTEGER NOT NULL
);
"""


def handle(db, command):
    op, name = command['op'], command['name']
    binding = one(db, 'SELECT block_id,page_index FROM flash_bindings WHERE name=?', (name,))
    if op == 'bind':
        if binding:
            return {'decision':'already_bound'}
        index, block_id = command['page'], command['block']
        if page(db, block_id, index) is None:
            return {'decision':'physical_page_missing'}
        db.execute('INSERT INTO flash_bindings VALUES (?,?,?)', (name,block_id,index))
        return {'decision':'bound'}
    if op != 'read':
        return {'decision':'unknown_operation'}
    if not binding:
        return {'decision':'binding_missing'}
    return {'decision':'read', 'value':page(db,binding['block_id'],binding['page_index'])}
