from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from jarvis.security import Risk
from jarvis.runtime.scenarios import find_scenario, scenario_risk
from jarvis.tools.desktop_features import register_desktop_features
from jarvis.ui.desktop_events import drain_desktop_events


class Config:
    def __init__(self,root):
        self.paths=SimpleNamespace(root=Path(root),screenshots=Path(root)/'screenshots')
        self.data={'app':{'language':'ru'}}
    def get(self,key,default=None):
        value=self.data
        for bit in key.split('.'):
            if not isinstance(value,dict) or bit not in value:return default
            value=value[bit]
        return value
    def save(self,data):self.data=data


class DesktopFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Config(self.temp.name)
        self.tools={}
        register_desktop_features(SimpleNamespace(config=self.config,register=lambda tool:self.tools.setdefault(tool.name,tool)))
        drain_desktop_events(128)
    def tearDown(self):self.temp.cleanup()
    def call(self,tool_name,**args):return self.tools[tool_name].handler(args)
    def test_shelf_persists_across_registry_and_conflicts(self):
        result=self.call('shelf',action='create',kind='note',title='My заметка',text='payload')
        row=result.data
        self.assertEqual(row['content'],'payload')
        changed=self.call('shelf',action='update',id=row['id'],version=1,content='new')
        self.assertEqual(changed.data['version'],2)
        from jarvis.ui.shelf_store import VersionConflict,ShelfStore
        with self.assertRaises(VersionConflict):self.call('shelf',action='update',id=row['id'],version=1,content='stale')
        self.assertEqual(ShelfStore(self.config.paths.root).get(row['id'])['content'],'new')
    def test_delete_card_never_deletes_original(self):
        source=Path(self.temp.name)/'original.txt';source.write_text('important')
        row=self.call('shelf',action='import_file',path=str(source)).data
        self.call('shelf',action='delete',id=row['id'],version=row['version'])
        self.assertEqual(source.read_text(),'important')
    def test_ui_request_is_not_reported_as_completed_action(self):
        result=self.call('shelf',action='show')
        self.assertTrue(result.data['queued'])
        self.assertEqual(drain_desktop_events()[-1]['action'],'shelf.show')
        self.assertEqual(result.text,'Открываю полку.')
        self.assertFalse(result.data['completed'])
        self.assertTrue(result.data['outcome_pending'])

    def test_queued_action_response_has_native_specific_action_all_locales(self):
        for lang in ('ru','en','de','fr','es','it','pt'):
            self.config.data['app']['language']=lang
            shelf=self.call('shelf',action='show')
            settings=self.call('desktop_features',action='settings')
            self.assertNotEqual(shelf.text,settings.text)
            self.assertEqual(shelf.data['desktop_action'],'shelf.show')
            self.assertFalse(shelf.data['completed'])
            self.assertNotIn('NEO',shelf.text)
        drain_desktop_events(128)

    def test_game_locations_missing_categories_are_explicit(self):
        with patch('jarvis.tools.desktop_features.find_game_locations',return_value=[]):
            result=self.call('game_locations',action='find',name='Darktide')
        self.assertIn('сохранения',result.text)
        self.assertIn('видеозаписи',result.text)
        self.assertIn('не найдены',result.text)
    def test_all_locales_keep_success_and_payload(self):
        for lang in ('ru','en','de','fr','es','it','pt'):
            self.config.data['app']['language']=lang
            result=self.call('downloads',action='status')
            self.assertTrue(result.success)
            self.assertFalse(result.data['enabled'])
            self.assertTrue(result.text)
    def test_history_pause_does_not_destroy_events(self):
        from jarvis.runtime.event_store import EventStore
        store=EventStore(self.config.paths.root)
        store.record('activity.window',{'title':'test'})
        self.call('activity',action='pause')
        self.assertFalse(self.config.get('activity.enabled'))
        self.assertEqual(len(self.call('activity',action='list').data),1)
        self.assertEqual(self.tools['activity'].risk_resolver({'action':'clear'}),Risk.HIGH)
    def test_scenario_id_and_command_risk(self):
        self.config.data['scenarios']={'items':[{'id':'abc','name':'Work','phrase':'go','steps':[{'type':'command','command':['tool.exe']}]},{'id':'def','name':'Work','steps':[]}]}
        self.assertEqual(find_scenario(self.config,'abc')['id'],'abc')
        with self.assertRaises(ValueError):find_scenario(self.config,'Work')
        self.assertEqual(scenario_risk(self.config,{'action':'run','id':'abc'}),Risk.HIGH)
    def test_update_without_read_version_cannot_overwrite(self):
        row=self.call('shelf',action='create',title='Original',content='keep').data
        result=self.call('shelf',action='update',id=row['id'],content='lost')
        self.assertFalse(result.success)
        self.assertTrue(result.data['version_required'])
        self.assertEqual(self.call('shelf',action='read',id=row['id']).text,'keep')
    def test_checklist_read_keeps_content_and_check_state(self):
        row=self.call('shelf',action='create',kind='checklist',title='Tasks',items=['one','two']).data
        self.call('shelf',action='check',id=row['id'],version=1,index=1,checked=True)
        result=self.call('shelf',action='read',id=row['id'])
        self.assertIn('1. [ ] one',result.text)
        self.assertIn('2. [x] two',result.text)
    def test_game_executable_location_opens_parent_never_binary(self):
        binary=Path(self.temp.name)/'game.exe';binary.write_bytes(b'MZ')
        rows=[{'path':str(binary),'kind':'exe','source':'user','confidence':'verified','confirmed':True}]
        with patch('jarvis.tools.desktop_features.find_game_locations',return_value=rows),patch('jarvis.tools.implementations._open_path') as opener:
            result=self.call('game_locations',action='open',name='Game',kind='exe')
            self.assertTrue(result.success)
            opener.assert_called_once_with(binary.parent)
        self.assertEqual(self.tools['game_locations'].risk_resolver({'action':'open'}),Risk.MEDIUM)
    def test_game_location_ambiguity_does_not_pick_first(self):
        rows=[{'path':'A'},{'path':'B'}]
        with patch('jarvis.tools.desktop_features.find_game_locations',return_value=rows):
            result=self.call('game_locations',action='open',name='Game',kind='saves')
        self.assertFalse(result.success)
        self.assertTrue(result.data['ambiguous'])


if __name__=='__main__':unittest.main()
