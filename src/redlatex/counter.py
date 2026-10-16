"""A persistent count of the documents compared, kept in one small file."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path


class Counter:
    def __init__(self, path, start=0):
        self.path, self.lock = Path(path), threading.Lock()
        self.value = start
        try:
            self.value = int(json.loads(self.path.read_text())['documents'])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def increment(self):
        with self.lock:
            self.value += 1
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps({'documents': self.value}))
                os.replace(tmp, self.path)
            except OSError as e:
                print(f'redlatex: cannot persist counter ({e}); keeping it in memory', file=sys.stderr)
            return self.value


def default_counter():
    data = Path(os.environ.get('REDLATEX_DATA_DIR') or Path(tempfile.gettempdir()) / 'redlatex-data')
    return Counter(data / 'counter.json', start=int(os.environ.get('REDLATEX_COUNTER_START', 0)))
