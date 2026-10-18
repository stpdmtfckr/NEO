"""NEO 188 NEO-PRIVACY-002 / TZ-CORRECTION-TRAINING-DATA-CONTROL-01 (synthetic data only)."""
import json
from types import SimpleNamespace

from jarvis.runtime import memory169
from jarvis.runtime import training_corrections188 as tc


def config(tmp_path, **values):
    data = {'memory.enabled': True, **values}
    return SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: data.get(key, default))


def correct(cfg, text, tool, args, code='ru'):
    assert memory169.record_correction(cfg, code, text, None, (tool, args))


def test_ctdp01_correction_works_but_is_not_trained_without_opt_in(tmp_path):
    cfg = config(tmp_path)
    correct(cfg, 'включи мой трек Закат', 'music', {'action': 'play_track', 'query': 'Закат'})
    assert memory169.correction_rule(cfg, 'включи мой трек Закат', 'ru') == ('music', {'action': 'play_track', 'query': 'Закат'})
    assert tc.eligible(cfg) == []
    copy, key = tc.write_training_copy(cfg, tmp_path / 'train.jsonl')
    assert copy is None and key == 'none' and not (tmp_path / 'train.jsonl').exists()


def test_ctdp01_per_example_exclusion(tmp_path):
    cfg = config(tmp_path, **{'privacy.train_on_corrections': True})
    correct(cfg, 'открой мой Steam', 'application', {'action': 'open', 'name': 'Steam'})
    correct(cfg, 'открой мой Discord', 'application', {'action': 'open', 'name': 'Discord'})
    rows = memory169.load_corrections(cfg)
    assert len(tc.eligible(cfg)) == 2
    tc.set_excluded(cfg, tc.example_id(rows[0]))
    left = tc.eligible(cfg)
    assert len(left) == 1 and left[0]['id'] == tc.example_id(rows[1])
    assert memory169.correction_rule(cfg, 'открой мой Steam', 'ru') is not None     # the exact rule still works


def test_ctdp02_consistent_typed_replacement_on_both_sides(tmp_path):
    row = {'language': 'ru', 'text': r'скопируй C:\Users\Vova\Отчёт.docx в C:\Users\Vova\Архив',
           'right': {'tool': 'filesystem', 'arguments': {'action': 'copy', 'source': r'C:\Users\Vova\Отчёт.docx',
                                                          'destination': r'C:\Users\Vova\Архив'}}}
    copy = tc.minimize(row)
    assert copy is not None and 'Vova' not in json.dumps(copy, ensure_ascii=False)
    args = copy['right']['arguments']
    assert args['action'] == 'copy' and args['source'] in copy['text'] and args['destination'] in copy['text']
    assert args['source'] != args['destination']
    assert row['right']['arguments']['source'].endswith('Отчёт.docx')                 # operational data untouched


def test_ctdp03_units_and_numbers_keep_their_meaning():
    row = {'language': 'ru', 'text': 'поставь таймер на 20 минут', 'right': {'tool': 'timer', 'arguments': {'action': 'timer', 'seconds': 1200}}}
    copy = tc.minimize(row)
    assert copy['text'] == 'поставь таймер на 20 минут' and copy['right']['arguments']['seconds'] == 1200


def test_ctdp04_free_text_secrets_and_unmatched_values_exclude_the_whole_example():
    rows = [
        {'language': 'ru', 'text': 'запиши заметку купить хлеб', 'right': {'tool': 'shelf', 'arguments': {'action': 'create', 'text': 'купить хлеб'}}},
        {'language': 'en', 'text': 'open https://x.test/?token=SECRET123', 'right': {'tool': 'browser', 'arguments': {'action': 'open_url', 'url': 'https://x.test/?token=SECRET123'}}},
        {'language': 'ru', 'text': 'включи песню про закат', 'right': {'tool': 'music', 'arguments': {'action': 'play_track', 'query': 'Закатные облака'}}},
        {'language': 'en', 'text': 'mail me at a@b.test', 'right': {'tool': 'email', 'arguments': {'action': 'compose'}}},
    ]
    assert [tc.minimize(row) for row in rows] == [None, None, None, None]


def test_ctdp05_stale_prepared_copy_is_not_reused_after_an_exclusion(tmp_path):
    cfg = config(tmp_path, **{'privacy.train_on_corrections': True})
    correct(cfg, 'открой мой Steam', 'application', {'action': 'open', 'name': 'Steam'})
    _, before = tc.write_training_copy(cfg, tmp_path / 'train.jsonl')
    tc.set_excluded(cfg, tc.example_id(memory169.load_corrections(cfg)[0]))
    copy, after = tc.write_training_copy(cfg, tmp_path / 'train.jsonl')
    assert before != after and copy is None and not (tmp_path / 'train.jsonl').exists()


def test_ctdp06_unreadable_eligibility_fails_closed(tmp_path):
    cfg = config(tmp_path, **{'privacy.train_on_corrections': True})
    correct(cfg, 'открой мой Steam', 'application', {'action': 'open', 'name': 'Steam'})
    (tmp_path / 'neo_model' / 'training_exclusions.json').write_text('{broken', encoding='utf-8')
    assert tc.eligible(cfg) == []
    assert memory169.correction_rule(cfg, 'открой мой Steam', 'ru') is not None


def test_ctdp07_settings_text_states_the_limits():
    note = tc.settings_note()
    assert 'обезличенном' in note and 'Уже обученная модель может сохранять влияние' in note


def test_pipelines_use_only_the_minimized_copy():
    source = open('jarvis/neo_llm/pipeline.py', encoding='utf-8').read() + open('jarvis/neo_model/jobs.py', encoding='utf-8').read()
    assert 'training_corrections188.eligible(self.config)' in source and 'write_training_copy' in source
    assert "command += ['--corrections', str(corrections)]" not in source
