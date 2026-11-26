"""Small synthetic flash blocks with a one-way bit programming rule."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS flash_blocks(
    id TEXT PRIMARY KEY, p0 INTEGER NOT NULL, p1 INTEGER NOT NULL,
    erases INTEGER NOT NULL DEFAULT 0
);
"""


def page(db, block_id, index):
    if type(index) is not int or index not in (0, 1):
        return None
    row = one(db, 'SELECT p0,p1 FROM flash_blocks WHERE id=?', (block_id,))
    return row[f'p{index}'] if row else None


def handle(db, command):
    block_id, op = command['block'], command['op']
    exists = one(db, 'SELECT id FROM flash_blocks WHERE id=?', (block_id,))
    if op == 'format':
        if exists:
            return {'decision':'block_exists'}
        db.execute('INSERT INTO flash_blocks (id,p0,p1) VALUES (?,15,15)', (block_id,))
        return {'decision':'formatted'}
    if not exists:
        return {'decision':'block_missing'}
    if op == 'erase':
        db.execute('UPDATE flash_blocks SET p0=15,p1=15,erases=erases+1 WHERE id=?', (block_id,))
        return {'decision':'erased'}
    if op != 'program':
        return {'decision':'unknown_operation'}
    index, value = command['page'], command['value']
    old = page(db,block_id,index)
    if old is None or type(value) is not int or not 0 <= value < 16:
        return {'decision':'invalid_page_value'}
    if (old & value) != value:
        return {'decision':'erase_required'}
    column = 'p0' if index == 0 else 'p1'
    db.execute(f'UPDATE flash_blocks SET {column}=? WHERE id=?', (value,block_id))
    return {'decision':'programmed'}
