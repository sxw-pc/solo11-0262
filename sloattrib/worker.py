"""Apply parsed synthetic jobs once per stream and frame number."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS wire_deliveries(
    stream TEXT NOT NULL, frame INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(stream, frame), FOREIGN KEY(stream, frame) REFERENCES wire_frames(stream, frame)
);
"""


def handle(db, command):
    if command['op'] != 'execute':
        return {'decision': 'unknown_operation'}
    key = (command['stream'], command['frame'])
    frame = one(db, 'SELECT payload FROM wire_frames WHERE stream=? AND frame=?', key)
    if not frame:
        return {'decision': 'frame_pending'}
    if one(db, 'SELECT 1 FROM wire_deliveries WHERE stream=? AND frame=?', key):
        return {'decision': 'already_executed'}
    db.execute('INSERT INTO wire_deliveries VALUES (?,?,?)', (*key, frame['payload']))
    return {'decision': 'executed', 'payload': frame['payload']}
