"""NEO 187 REC-TTS-CALLER-01: callers let the Heavy store resolve its own file list."""
import threading

import pytest

import prepare_response_voice
from jarvis.response_voice.model_store import HeavyModelStore, ModelStore, VoiceModelError
from tests.test_neo185_voice_tiers import FakeHub


def test_prepare_reaches_store_download_for_unresolved_heavy_manifest(tmp_path):
    hub = FakeHub()
    store = HeavyModelStore(tmp_path, transport=hub)
    assert store.verification_error() == 'manifest_unresolved'
    result = prepare_response_voice.prepare(tmp_path, explicit=True, store=store, install=lambda *a: None)
    assert result.get('state') == 'verified', result
    assert hub.urls and hub.urls[0].endswith('?blobs=true')     # store resolved the listing itself
    assert result['voice_ready'] is False                       # never claims a tested voice


def test_malformed_saved_heavy_listing_still_fails_before_download(tmp_path):
    store = HeavyModelStore(tmp_path, manifest={'files': [{'path': 'x'}], 'revision': 'main', 'total_bytes': 0},
                            transport=FakeHub())
    with pytest.raises(VoiceModelError, match='manifest_invalid'):
        store.validate_before_download()


def test_standard_store_preflight_is_unchanged(tmp_path):
    store = ModelStore(tmp_path)
    assert store.validate_before_download() is None
