"""NEO 185: light tier — the voices already installed in Windows. Nothing is downloaded."""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path


class SystemVoiceStore:
    """Duck-typed like ModelStore so VoiceService needs no special cases."""
    model_id = 'windows-sapi'

    def __init__(self, profile):
        self.profile = Path(profile)
        self.root = self.profile / 'models' / 'system-voice'      # never created
        self.tombstone = self.profile / 'response_voice_system.deleted.json'  # never written
        self.manifest = {'total_bytes': 0, 'files': []}

    def verification_error(self):
        return None if os.name == 'nt' else 'system_voice_unavailable'

    def _validate(self):
        return None

    def ready_cached(self):
        return True

    def check(self, cancel=None, progress=lambda **kw: None):
        return {'state': 'verified', 'done': 0, 'total': 0, 'verification_mode': 'system'}

    def download(self, cancel, progress=lambda **kw: None, *, explicit=False):
        return self.check(cancel, progress)

    def delete(self):
        # Windows voices belong to Windows; NEO never removes them.
        return {'state': 'verified', 'done': 0, 'total': 0}

    @contextmanager
    def operation_lock(self):
        yield
