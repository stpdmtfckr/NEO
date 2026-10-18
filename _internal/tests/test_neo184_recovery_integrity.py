"""Reproductions against immutable archive or NEO_AUDIT_SOURCE correction copy."""
import os,sys,json,sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
SOURCE=Path(__file__).resolve().parents[1]
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from jarvis.testing.isolation import external_io_blocked
from jarvis.settings_storage184 import prepare_settings
from jarvis.database import Database
from jarvis.runtime.conversation import PersonalAliases

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
 for k in ('HOME','LOCALAPPDATA','APPDATA','XDG_CACHE_HOME'):
  p=tmp_path/k;p.mkdir();monkeypatch.setenv(k,str(p))
 with external_io_blocked():yield

@pytest.mark.parametrize('backup',['settings.last-good.json','settings.migration-backup.json'])
def test_missing_primary_restores_valid_backup(tmp_path,backup):
 root=tmp_path/'profile';(root/'настройки').mkdir(parents=True)
 original=b'{"user_name":"Preserved","custom":42}'
 (root/'настройки'/backup).write_bytes(original)
 defaults=tmp_path/'defaults.json';defaults.write_text('{"user_name":"Default"}')
 target=prepare_settings(root,defaults,fresh_model_offer=True)
 assert target.read_bytes()==original
 assert (root/'настройки'/backup).read_bytes()==original

@pytest.fixture
def db(tmp_path):
 database=Database(tmp_path/'memory.db');yield database;database.close()

def test_failed_alias_save_leaves_cached_command_unchanged(db):
 a=PersonalAliases(db);a.add('work please','open calculator','en',confirmed=True)
 with patch.object(db,'set_memory',side_effect=OSError('synthetic disk fail')):
  with pytest.raises(OSError):a.add('work please','open browser','en',confirmed=True)
 assert a.resolve('work please','en')=='open calculator'

def test_deleted_alias_invalidates_other_reader(db):
 a=PersonalAliases(db);b=PersonalAliases(db);a.add('work please','open calculator','en',confirmed=True)
 assert b.resolve('work please','en')=='open calculator';a.delete('work please','en')
 assert b.resolve('work please','en')==''

def test_alias_rows_are_detached_copy(db):
 a=PersonalAliases(db);a.add('copy please','copy selected text','en',confirmed=True)
 rows=a.rows('en');rows['copy please']['command']='paste clipboard'
 assert a.resolve('copy please','en')=='copy selected text'

@pytest.mark.parametrize('operation',['set','delete'])
def test_failed_memory_transaction_cannot_commit_with_next_write(db,operation):
 db.set_memory('old','original');connection=db._connection
 class FailCommitOnce:
  failed=False
  def __getattr__(self,n):return getattr(connection,n)
  def commit(self):
   if not self.failed:self.failed=True;raise sqlite3.OperationalError('synthetic commit failure')
   return connection.commit()
 db._connection=FailCommitOnce()
 with pytest.raises(sqlite3.Error):
  if operation=='set':db.set_memory('old','failed mutation')
  else:db.delete_memory('old')
 db.set_memory('other','successful unrelated write')
 check=sqlite3.connect(db.path)
 try:assert check.execute('select value from memories where key=?',('old',)).fetchone()[0]=='original'
 finally:check.close()

@pytest.mark.parametrize('bad_value',[float('inf'),float('-inf'),float('nan')])
def test_bad_numeric_setting_does_not_discard_valid_fields(tmp_path,monkeypatch,bad_value):
 from jarvis.config import ConfigManager
 monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *a:None)
 monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *a:None)
 config=ConfigManager(SOURCE/'config/default_settings.json')
 config.paths.settings.write_text(json.dumps({'settings_version':bad_value,'user_name':'Preserve this field'}))
 recovered=ConfigManager(SOURCE/'config/default_settings.json')
 assert recovered.get('user_name')=='Preserve this field'
 assert 'settings_version' in recovered.repaired_settings

def test_large_finite_integer_version_preserves_valid_fields(tmp_path,monkeypatch):
 from jarvis.config import ConfigManager
 monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *a:None)
 monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *a:None)
 config=ConfigManager(SOURCE/'config/default_settings.json')
 config.paths.settings.write_text(json.dumps({'settings_version':10**400,'user_name':'Preserve this field'}))
 recovered=ConfigManager(SOURCE/'config/default_settings.json')
 assert recovered.get('user_name')=='Preserve this field'
 assert not recovered.recovered_settings_reason

def test_legacy_profile_does_not_share_default_lists(tmp_path,monkeypatch):
 from jarvis.config import ConfigManager
 monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *a:None)
 monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *a:None)
 config=ConfigManager(SOURCE/'config/default_settings.json')
 config.paths.settings.write_text('{"user_name":"Saved"}')
 config.reload()
 before=list(config._defaults_cache['voice']['wake_aliases'])
 config.data['voice']['wake_aliases'].append('Synthetic user alias')
 assert config._defaults_cache['voice']['wake_aliases']==before


def test_alias_cache_observes_other_database_connection(db):
 a=PersonalAliases(db);a.add('work please','open calculator','en',confirmed=True)
 other=Database(db.path)
 try:
  b=PersonalAliases(other);assert b.resolve('work please','en')=='open calculator'
  a.delete('work please','en');assert b.resolve('work please','en')==''
 finally:other.close()


@pytest.mark.parametrize('action',['delete','disable'])
def test_failed_alias_mutation_keeps_current_value(db,action):
 a=PersonalAliases(db);a.add('work please','open calculator','en',confirmed=True)
 with patch.object(db,'set_memory',side_effect=OSError('synthetic disk fail')):
  with pytest.raises(OSError):
   if action=='delete':a.delete('work please','en')
   else:a.set_enabled('work please','en',False)
 assert a.resolve('work please','en')=='open calculator'

@pytest.mark.parametrize('bad_value',[10**400,-10**400])
def test_unrepresentable_float_setting_preserves_other_fields(tmp_path,monkeypatch,bad_value):
 from jarvis.config import ConfigManager
 monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *a:None)
 monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *a:None)
 config=ConfigManager(SOURCE/'config/default_settings.json')
 config.paths.settings.write_text(json.dumps({'voice':{'silence_seconds':bad_value},'user_name':'Keep valid fields'}))
 recovered=ConfigManager(SOURCE/'config/default_settings.json')
 assert recovered.get('user_name')=='Keep valid fields'
 assert recovered.get('voice.silence_seconds')==recovered._defaults_cache['voice']['silence_seconds']
 assert 'voice.silence_seconds' in recovered.repaired_settings
