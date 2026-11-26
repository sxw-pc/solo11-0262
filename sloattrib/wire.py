"""Reassemble synthetic length-prefixed commands from a local wire fixture."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS wire_position(
    stream TEXT PRIMARY KEY, remaining BLOB NOT NULL, next_frame INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wire_frames(
    stream TEXT NOT NULL, frame INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(stream, frame)
);
"""


def handle(db, command):
    if command['op'] != 'receive':
        return {'decision': 'unknown_operation'}
    stream = command['stream']
    position = one(db, 'SELECT * FROM wire_position WHERE stream=?', (stream,))
    try:
        received = bytes.fromhex(command['hex'])
    except (ValueError, TypeError):
        return {'decision': 'invalid_bytes'}
    data = (position['remaining'] if position else b'') + received
    frame = position['next_frame'] if position else 0
    completed = []
    while data:
        size = data[0]
        if size == 0:
            return {'decision': 'invalid_frame'}
        if len(data) <= size:
            break
        try:
            payload = data[1:size+1].decode('utf-8')
        except UnicodeDecodeError:
            return {'decision': 'invalid_frame'}
        completed.append((stream, frame, payload))
        data = data[size+1:]
        frame += 1
    db.executemany('INSERT INTO wire_frames VALUES (?,?,?)', completed)
    db.execute('INSERT INTO wire_position VALUES (?,?,?) ON CONFLICT(stream) DO UPDATE '
               'SET remaining=excluded.remaining, next_frame=excluded.next_frame', (stream, data, frame))
    return {'decision': 'received', 'frames': [record[1] for record in completed], 'buffered': len(data)}
