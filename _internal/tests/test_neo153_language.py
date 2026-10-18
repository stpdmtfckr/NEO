"""User-facing input against a real Agent with actions/credentials blocked."""
from dataclasses import asdict
from pathlib import Path
import json,time
from types import SimpleNamespace
import pytest
from jarvis.runtime.social_atlas import SocialAtlas,catalog,dialogue_additions
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked
from tools.atlas_all_test import Phrase,check_phrase

ROOT=Path(__file__).resolve().parents[1]
CASES=json.loads((ROOT/'config/regression153.json').read_text())

@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('neo153')/'run');prepare_fixtures(s)
  yield s
  s.finish()

@pytest.mark.parametrize('case',[r for r in CASES if not r.get('scenario_id')],ids=lambda r:r['locale']+':'+r['phrase'])
def test_new_short_and_conversation_contract(session,case):
 session.reset_case(case['locale'])
 r=check_phrase(session,asdict(Phrase(**case)))
 allowed={'NEGATIVE_PASS'} if case.get('expected_kind')=='no_action' else {'ROUTE_PASS','ARGUMENTS_PASS','CONFIRMATION_REQUIRED'}
 assert r['status'] in allowed,r
 assert r['response'].strip()
 assert not r.get('localization_warning'),r

@pytest.mark.parametrize('code',LANGUAGES)
def test_context_survives_only_inside_explicit_dialogue(session,code):
 session.reset_case(code)
 for case in (r for r in CASES if r.get('scenario_id')==code+'-plans153'):
  r=check_phrase(session,asdict(Phrase(**case)))
  assert r['status']=='NEGATIVE_PASS',r
  assert r['response'].strip()

@pytest.mark.parametrize('code',LANGUAGES)
def test_social_followup_is_scoped_and_expires(code,monkeypatch):
 s=SocialAtlas();data=json.loads((ROOT/'config/social_dialogue153.json').read_text())
 plan=next(g for g in data['groups'] if g['family']=='plans153')['phrases'][code][0]
 word=next(p for p,v in data['followups'][code].items() if v['topic']=='work')
 expected=data['followups'][code][word]['answer']
 assert s.reply(plan,code,session='A')
 assert s.reply(word,code,session='B')!=expected
 assert s.reply(word,code,session='A')==expected
 s.clear_context('A');assert s.reply(word,code,session='A')!=expected
 s.reply(plan,code,session='A')
 s._topics[('A',code)]=('plans153',time.monotonic()-121)
 assert s.reply(word,code,session='A')!=expected

@pytest.mark.parametrize('text',['далше','дальш','далши','dalsze','Dalshe.','на зад','назат','назадд','назать'])
def test_quick_alias_never_calls_general_language_pipeline(text,monkeypatch):
 from jarvis import quick_media_commands as q
 monkeypatch.setattr(q,'command_language_violation',lambda *a:pytest.fail('fast alias must not wait for language gate'))
 monkeypatch.setattr(q,'normalize_multilingual_command',lambda *a:pytest.fail('fast alias must not wait for normalizer'))
 assert q.parse_quick_media_command(text,'ru').action in {'next','previous'}

@pytest.mark.parametrize('text',['а потом пойдем дальше','не переключай назад','очередь дискорд удали','поставь песню назад','открой файл "очередь дискорд"'])
def test_short_lookup_not_substring_permission(text):
 from jarvis.runtime.command_contracts150 import short_request
 assert short_request(text,'ru') is None
