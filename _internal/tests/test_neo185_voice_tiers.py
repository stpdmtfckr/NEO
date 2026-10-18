"""NEO185 voice tiers: light (Windows voices), medium (pinned 0.6B), heavy (resolved 1.7B); robot timbre."""
from __future__ import annotations
import hashlib, io, json, os, threading
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest

from jarvis.response_voice import model_store
from jarvis.response_voice.model_store import (HeavyModelStore, ModelStore, VoiceModelError, manifest_from_listing)
from jarvis.response_voice.robot import robotize
from jarvis.response_voice.system_worker import pick_voice
from jarvis.response_voice.system_store import SystemVoiceStore
from jarvis.response_voice.service import VoiceService, options


def test_robotize_keeps_level_length_and_changes_timbre():
    rate = 22050
    t = np.arange(rate) / rate
    voice = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = robotize(voice, rate, 0.6)
    assert out.shape == voice.shape and out.dtype == np.float32
    assert abs(np.max(np.abs(out)) - 0.5) < 1e-3
    assert np.mean(np.abs(out - voice)) > 0.02
    assert np.array_equal(robotize(voice, rate, 0.0), voice)


@pytest.mark.parametrize('language,speaker,name', [('ru', 'Aiden', 'Pavel'), ('ru', 'Serena', 'Irina'),
                                                    ('en', 'Serena', 'Zira'), ('de', 'Aiden', 'Hedda')])
def test_system_voice_choice_by_language_then_gender(language, speaker, name):
    voices = [{'name': 'Irina', 'language': '419', 'gender': 'Female'}, {'name': 'Pavel', 'language': '419', 'gender': 'Male'},
              {'name': 'David', 'language': '409', 'gender': 'Male'}, {'name': 'Zira', 'language': '409;809', 'gender': 'Female'},
              {'name': 'Hedda', 'language': '407', 'gender': 'Female'}]
    assert pick_voice(voices, language, speaker)['name'] == name
    assert pick_voice(voices, 'it', 'Aiden') is None


def _git_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


FILES = {'config.json': b'{"a": 1}', 'model.safetensors': b'weights-weights', 'speech_tokenizer/model.safetensors': b'tok'}
REV = 'a' * 40


def listing():
    siblings = [{'rfilename': '.gitattributes', 'size': 5, 'blobId': 'b' * 40}]
    for name, data in FILES.items():
        if name.endswith('.safetensors'):
            siblings.append({'rfilename': name, 'size': len(data), 'blobId': 'c' * 40,
                             'lfs': {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'pointerSize': 130}})
        else:
            siblings.append({'rfilename': name, 'size': len(data), 'blobId': _git_sha(data)})
    return {'sha': REV, 'siblings': siblings}


class Response(io.BytesIO):
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class FakeHub:
    def __init__(self, files=FILES, listing_value=None):
        self.files, self.urls = dict(files), []
        self.listing = listing_value or listing()
    def open(self, url, offset):
        self.urls.append(url)
        if '/api/models/' in url:
            return Response(json.dumps(self.listing).encode())
        name = url.split('/resolve/' + REV + '/', 1)[1]
        return Response(self.files[name][offset:])


def test_manifest_from_listing_pins_commit_and_mixed_digests():
    manifest = manifest_from_listing(listing())
    assert manifest['revision'] == REV and manifest['model_id'] == 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
    rows = {row['path']: row for row in manifest['files']}
    assert '.gitattributes' not in rows
    assert rows['model.safetensors']['integrity']['algorithm'] == 'sha256'
    assert rows['config.json']['integrity']['algorithm'] == 'git-blob-sha1'
    assert manifest['total_bytes'] == sum(len(v) for v in FILES.values())


@pytest.mark.parametrize('mutate', ['no_sha', 'bad_digest', 'weights_as_git', 'unsafe_path', 'too_big'])
def test_manifest_from_listing_rejects_bad_listings(mutate):
    value = listing()
    if mutate == 'no_sha': value['sha'] = 'main'
    if mutate == 'bad_digest': value['siblings'][1]['blobId'] = 'xyz'
    if mutate == 'weights_as_git':
        for row in value['siblings']: row.pop('lfs', None)
    if mutate == 'unsafe_path': value['siblings'][1]['rfilename'] = '../evil.json'
    if mutate == 'too_big': value['siblings'][2]['lfs']['size'] = 9 * 1024 ** 3
    with pytest.raises(VoiceModelError):
        manifest_from_listing(value)


def test_heavy_store_resolves_downloads_verifies_and_deletes(tmp_path):
    hub = FakeHub()
    store = HeavyModelStore(tmp_path, transport=hub)
    assert store.verification_error() == 'manifest_unresolved'
    result = store.download(threading.Event(), explicit=True)
    assert result['state'] == 'verified'
    assert hub.urls[0].endswith('/api/models/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice/revision/main?blobs=true')
    assert all('/resolve/' + REV + '/' in url for url in hub.urls[1:])
    again = HeavyModelStore(tmp_path)                       # saved listing is reused offline
    assert again.verification_error() is None and again.check()['state'] == 'verified'
    assert (tmp_path / 'models/qwen-response-voice-1.7b/model.safetensors').read_bytes() == FILES['model.safetensors']
    again.delete()
    assert not again.root.exists() and not again.manifest_path.exists() and again.tombstone.exists()
    # the medium model is a different folder and tombstone
    assert ModelStore(tmp_path).root.name == 'qwen-response-voice' and not ModelStore(tmp_path).tombstone.exists()


def test_heavy_store_rejects_tampered_weights(tmp_path):
    hub = FakeHub(files=dict(FILES, **{'model.safetensors': b'weights-tampered'[:len(FILES['model.safetensors'])]}))
    with pytest.raises(VoiceModelError):
        HeavyModelStore(tmp_path, transport=hub).download(threading.Event(), explicit=True)
    assert not (tmp_path / 'models/qwen-response-voice-1.7b/model.safetensors').exists()


class Config:
    def __init__(self, root, **voice):
        self.paths = SimpleNamespace(root=root)
        self.data = {'response_voice': dict(voice)}
    def get(self, key, default=None):
        node = self.data
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node: return default
            node = node[part]
        return node


def test_service_follows_tier_and_page_selection(tmp_path):
    config = Config(tmp_path, tier='light')
    service = VoiceService(config)
    assert isinstance(service.store, SystemVoiceStore) and service.tier == 'light'
    assert service._runtime_check(tmp_path) is True                  # no voice runtime needed
    config.data['response_voice']['tier'] = 'heavy'
    service.snapshot()
    assert isinstance(service.store, HeavyModelStore)
    service.select_tier('standard')
    assert type(service.store) is ModelStore
    service.select_tier(None)
    assert isinstance(service.store, HeavyModelStore)
    assert options(config)['robot_effect'] == 45 and options(Config(tmp_path, tier='bogus'))['tier'] == 'standard'
    service.close()


def test_light_tier_sends_robot_amount_to_the_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemVoiceStore, 'verification_error', lambda self: None)
    sent = []

    class Fake:
        def __init__(self, *a):
            import queue
            self.events = queue.Queue()
        def send(self, request):
            sent.append(request)
            for state in ('synthesizing', 'playing', 'done'): self.events.put({'state': state})
        def receive(self, timeout): return self.events.get(timeout=timeout)
        def close(self): pass
        def reap(self): pass

    config = Config(tmp_path, tier='light', enabled=True, robot_effect=70)
    service = VoiceService(config)
    service._process_factory = Fake
    assert service.submit('Привет', 'ru')
    for _ in range(200):
        if sent and service.snapshot()['voice_state'] == 'idle': break
        threading.Event().wait(0.01)
    assert sent and sent[0]['robot'] == 70 and sent[0]['language'] == 'ru'
    service.close()


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_voice_page_offers_three_tiers_with_descriptions(tmp_path, language):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from jarvis.ui.response_voice_page import ResponseVoicePage
    from jarvis.response_voice.text import text
    config = Config(tmp_path, tier='standard')
    config.data['app'] = {'language': language}
    page = ResponseVoicePage(config)
    try:
        assert [page.tier.itemData(i) for i in range(page.tier.count())] == ['light', 'standard', 'heavy']
        page.tier.setCurrentIndex(0)
        assert page.help_label.text() == text('help_light', language)
        assert page.buttons['download'].isHidden() and page.service.tier == 'light'
        page.tier.setCurrentIndex(2)
        assert page.help_label.text() == text('help_heavy', language) and not page.buttons['download'].isHidden()
        assert page.values()['tier'] == 'heavy' and 0 <= page.values()['robot_effect'] <= 100
    finally:
        page.close_operations()
        page.deleteLater()
