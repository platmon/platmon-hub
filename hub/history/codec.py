"""Raw payload compression with shared, content-addressed dictionaries (ADR-0006).

Pure functions and writer-thread state only; SQLite rows are written by the store.
"""

import hashlib
import zlib

WINDOW = 32768          # zlib uses at most the last 32 KiB of a dictionary
LEVEL = 6
BASELINE_ROWS = 20      # compressed sizes that define a dictionary's normal ratio
RECENT_ROWS = 100       # sizes compared with that baseline
DRIFT = 2               # re-select when recent rows average more than this times the baseline
MAX_STREAMS = 4096


def decode(data_json, payload_zlib, dictionary):
    """The stored compact JSON of a raw row, whichever form it was stored in."""
    if payload_zlib is None:
        return data_json
    if dictionary is None:
        raise ValueError('raw dictionary missing')
    inflater = zlib.decompressobj(zdict=bytes(dictionary))
    raw = inflater.decompress(bytes(payload_zlib)) + inflater.flush()
    if not inflater.eof:
        raise ValueError('truncated raw payload')
    return raw.decode('utf-8')


def compress(raw, dictionary):
    """Compressed bytes that decode back to raw exactly, or None."""
    try:
        deflater = zlib.compressobj(LEVEL, zlib.DEFLATED, 15, 9, zlib.Z_DEFAULT_STRATEGY, dictionary)
        blob = deflater.compress(raw) + deflater.flush()
        inflater = zlib.decompressobj(zdict=dictionary)
        if inflater.decompress(blob) + inflater.flush() != raw or not inflater.eof:
            return None
    except zlib.error:
        return None
    return blob


class Dictionaries:
    """Current dictionary per (node, stream), chosen from a publication of the current instance."""

    def __init__(self):
        self._streams = {}

    def encode(self, node_id, instance_id, stream, payload):
        """(compressed bytes, dictionary content, dictionary digest), or (None, None, None) to store JSON.

        The fourth value is True when compression failed verification, as opposed to not saving space.
        """
        raw = payload.encode('utf-8')
        key = (node_id, stream)
        state = self._streams.get(key)
        if state is None or state['instance'] != instance_id or self._drifted(state):
            state = self._select(key, instance_id, raw)
        blob = compress(raw, state['content'])
        if blob is None:
            return None, None, None, True
        if len(blob) >= len(raw):
            return None, None, None, False
        if state['seed']:
            state['seed'] = False  # the row a dictionary was taken from compresses against itself
        elif len(state['baseline']) < BASELINE_ROWS:
            state['baseline'].append(len(blob))
        else:
            state['recent'].append(len(blob))
            del state['recent'][:-RECENT_ROWS]
        return blob, state['content'], state['digest'], False

    def _drifted(self, state):
        recent, baseline = state['recent'], state['baseline']
        return (len(recent) == RECENT_ROWS and len(baseline) == BASELINE_ROWS and
                sum(recent) / RECENT_ROWS > DRIFT * sum(baseline) / BASELINE_ROWS)

    def _select(self, key, instance_id, raw):
        self._streams.pop(key, None)
        if len(self._streams) >= MAX_STREAMS:
            self._streams.pop(next(iter(self._streams)))
        content = raw[-WINDOW:]
        state = dict(instance=instance_id, content=content, digest=hashlib.sha256(content).hexdigest(),
                     seed=True, baseline=[], recent=[])
        self._streams[key] = state
        return state
