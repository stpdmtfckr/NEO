"""Test-owned values and an argument oracle independent of returned proposals.

Catalogue aliases are normalized by the published compatibility contract; search
and playback are NOT treated as equivalent. Only declared template parameters
are compared here. Independent scenario cases supply exact reaction contracts.
"""
from __future__ import annotations
import json
from pathlib import Path, PureWindowsPath
import re
import unicodedata
import zipfile
from jarvis.tools.argument_validation import normalize_tool_call

SLOT_MAP = {
 'autoclicker.set_rate': {'value':'cps'},
 'browser.open_url': {'url':'value'}, 'browser.open': {'name':'browser'},
 'browser.search': {'query':'value'}, 'browser.search_first': {'query':'value'},
 'browser.activate_tab_title': {'title':'value'},
 'browser_advanced.activate_by_topic': {'topic':'query'}, 'browser_advanced.group_by_topic': {'topic':'query'},
 'clipboard.paste_history': {'index':'offset'}, 'clipboard.copy_history': {'index':'offset'},
 'developer.run_python_file': {'path':'file_path'},
 'filesystem.write_text': {'text':'content'}, 'filesystem.create_text_file': {'text':'content'},
 'filesystem.append_text': {'text':'content'}, 'filesystem.insert_text': {'text':'content'},
 'filesystem.replace_text': {'new_text':'content'},
 'system.terminate_process': {'name':'process_name'}, 'system.set_power_plan': {'name':'power_plan'},
 'overlay.checklist_check': {'index':'item'}, 'overlay.checklist_uncheck': {'index':'item'},
 'overlay.checklist_remove': {'index':'item'}, 'overlay.checklist_add': {'text':'item'},
 'window_advanced.list_instances': {'application':'query'},
 'window_advanced.focus_instance': {'application':'query'},
 'window_advanced.move_instance': {'application':'query'},
}
_NUMBERS = {'seconds','minutes','value','volume','percent','count','index','offset','item','monitor',
            'width','height','x','y','zoom','font_size','position_seconds','cps'}
_LITERAL = {'text','content','body','subject','new_text','old_text','instruction','description'}
_PATHS = {'source','destination','path','file_path','folder'}


def prepare_fixtures(session) -> None:
    """Only files under the random test profile are created; never user files."""
    root = session.workspace
    (root/'destination').mkdir(exist_ok=True)
    (root/'source.txt').write_text('NEO QA text\n', encoding='utf-8', newline='\n')
    (root/'script.py').write_text('print("NEO QA")\n', encoding='utf-8', newline='\n')
    with zipfile.ZipFile(root/'source.zip', 'w') as archive:
        archive.writestr('source.txt', 'NEO QA text\n')
    session.config.data.setdefault('scenarios', {})['items'] = [
        {'id':'NEO_QA_SCENARIO', 'name':'NEO QA', 'phrase':'NEO QA',
         'steps':[{'type':'wait','seconds':0}]}]
    session.config.save()
    from jarvis.ui.shelf_store import ShelfStore
    shelf = ShelfStore(session.config.paths.root)
    shelf.create('note', title='NEO QA', content='NEO QA text')
    shelf.create('checklist', title='NEO QA Checklist', items=['First QA item','Second QA item'])


def render(case: dict, workspace: Path) -> tuple[str, dict, dict, list[str]]:
    intent = case['expected_tool'] + '.' + case['expected_action']
    args = {'action':case['expected_action']}
    values = {
      'source':str(workspace/'source.txt'), 'destination':str(workspace/'destination'),
      'path':str(workspace/'source.txt'), 'folder':str(workspace),
      'seconds':'900', 'minutes':'15', 'title':'NEO QA', 'content':'NEO QA text',
      'text':'NEO QA text', 'new_name':'renamed.txt', 'name':'NEO QA', 'query':'NEO QA',
      'volume':'25','percent':'25','count':'2', 'value':'25', 'index':'2', 'number':'2',
      'to':'neo-qa@example.invalid','recipient':'neo-qa@example.invalid',
      'subject':'NEO QA','body':'NEO QA text','url':'https://example.invalid/',
      'server':'NEO QA','channel':'NEO QA','id':'NEO_QA_TIMER',
      'application':'NEO QA','button':'left','city':'Paris','date':'2030-01-15',
      'description':'NEO QA test program','device_name':'NEO QA Speakers',
      'event_id':'NEO_QA_EVENT','font_size':'14','height':'480','host':'example.invalid',
      'instruction':'NEO QA screen description','key':'NEO_QA_KEY','keys':'ctrl+shift+k',
      'label':'NEO QA','message_id':'NEO_QA_MESSAGE','monitor':'1',
      'new_text':'NEO QA replacement','old_text':'NEO QA text','playlist':'NEO QA',
      'position_seconds':'30','reply_to_message_id':'1','sender':'NEO QA',
      'target':'NEO QA','target_language':'en','time':'10:30','topic':'NEO QA',
      'width':'640','x':'80','y':'100','zoom':'100',
      'voice':'ru_RU-denis-medium','script':'Write-Output NEO_QA_157',
    }
    if intent in {'filesystem.list','filesystem.tree','filesystem.disk_usage','filesystem.search_text'}:
        values['source'] = str(workspace)
    if intent == 'filesystem.unzip': values['source'] = str(workspace/'source.zip')
    if intent == 'filesystem.zip': values['destination'] = str(workspace/'output.zip')
    if intent == 'developer.run_python_file': values['path'] = str(workspace/'script.py')
    if intent == 'speech.set_rate': values['value'] = '1'
    if intent == 'autoclicker.set_rate': values['value'] = '5'
    if intent == 'audio.change_volume': values['value'] = '15'
    if intent == 'system.change_mouse_sensitivity': values['value'] = '1'
    if intent == 'system.set_mouse_sensitivity': values['value'] = '10'
    if intent == 'system.set_power_plan': values['name'] = 'balanced'
    if intent == 'browser.open': values['name'] = 'Edge'
    if intent == 'shelf.check': values['title'] = 'NEO QA Checklist'
    if intent == 'discord.music_youtube_url': values['query'] = 'https://www.youtube.com/watch?v=NEO_QA_FAKE'
    if intent == 'telegram_messages.send': values['target'] = 'NEO_QA_CHAT'
    used, missing = {}, []
    def replace(match):
        key = match[1]
        if key not in values:
            missing.append(key); return match[0]
        used[key] = values[key]
        field = SLOT_MAP.get(intent, {}).get(key,key)
        value = values[key]
        # Conversion follows documented slot types, not what the router returns.
        if key in _NUMBERS and field not in {'item'}:
            value = int(value)
        if intent in {'clipboard.paste_history','clipboard.copy_history','shelf.check'} and key == 'index':
            value = int(value) - 1
        if intent == 'input_control.hotkey' and key == 'keys': value = [v.strip().lower() for v in str(value).split('+')]
        args[field] = value
        return values[key]
    rendered = re.sub(r'\{([A-Za-z_][A-Za-z_0-9]*)\}', replace, case['phrase'])
    tool, args = canonical_expected(case['expected_tool'], args)
    return rendered, used, {'tool':tool,'arguments':args}, sorted(set(missing))


def canonical_expected(tool: str, arguments: dict) -> tuple[str,dict]:
    """Published aliases plus explicit existing cross-tool compatibility paths."""
    tool, args = normalize_tool_call(tool, dict(arguments))
    if tool == 'filesystem' and args.get('action') == 'find':
        tool, args = 'search', {'action':'files', **({'query':args['name']} if 'name' in args else {})}
    if tool == 'browser' and args.get('action') == 'open':
        tool, args = 'application', {'action':'open', **({'name':args['browser'], 'kind':'app'} if 'browser' in args else {})}
    if tool == 'translate':
        # This public single-operation tool has no action field in its schema.
        # The catalogue key is translate.translate; do not change runtime arguments.
        args['action'] = 'translate'
    if tool == 'powershell':
        # Like translate, this is a single-operation schema. The catalogue's
        # empty action marker is not an actual tool parameter.
        args.pop('action', None)
    return tool, args


def _same(field: str, expected, actual) -> bool:
    if isinstance(expected, bool): return actual is expected
    if isinstance(expected, (int,float)):
        if isinstance(actual, bool): return False
        try: return float(actual) == expected
        except (TypeError,ValueError): return False
    if field in _PATHS:
        e,a=str(expected),str(actual)
        if re.match(r'^[A-Za-z]:|^\\\\',e):
            return str(PureWindowsPath(e)).casefold()==str(PureWindowsPath(a)).casefold()
        return e.rstrip('/')==a.rstrip('/')
    if field in _LITERAL: return str(expected)==str(actual)
    return unicodedata.normalize('NFKC',str(expected)).casefold()==unicodedata.normalize('NFKC',str(actual)).casefold()


def pending_call(state, choice) -> dict | None:
    """Only typed pending state proves which action a clarification concerns."""
    pending = state.pending_intent
    if pending is not None:
        tool, args = canonical_expected(pending.tool, pending.arguments)
        return {'tool': tool, 'arguments': args}
    # This menu is created exclusively by application launch disambiguation.
    if isinstance(choice, dict) and choice.get('options') and choice.get('kind') in {'app', 'game'}:
        return {'tool': 'application', 'arguments': {'action': 'open', 'kind': choice['kind']}}
    # A generic shell such as "open" contains no verified object/action pair.
    return None


def evaluate(case, turn, calls, executions, state, choice, expected: dict):
    actual = []
    for call in calls:
        tool,args=canonical_expected(call['tool'],call['arguments'])
        actual.append({'tool':tool,'arguments':args})
    wanted=(expected['tool'], expected['arguments'].get('action'))
    exact=case.get('expected_args')
    required={k:v for k,v in expected['arguments'].items() if k!='action'}
    if exact is not None: required.update(exact)
    differences=[]
    for call in actual:
        if (call['tool'],call['arguments'].get('action')) == wanted:
            for key,value in required.items():
                if key not in call['arguments'] or not _same(key,value,call['arguments'][key]):
                    differences.append({'parameter':key,'expected':value,'actual':call['arguments'].get(key),'missing':key not in call['arguments']})
    kind=case.get('expected_kind','')
    has_pending=bool(state.pending_intent or state.pending_shell or choice)
    observed_pending = pending_call(state, choice)
    correct_pending = bool(observed_pending and
                           (observed_pending['tool'], observed_pending['arguments'].get('action')) == wanted)
    if any(isinstance(ex.data,dict) and ex.data.get('invalid_tool_call') for ex in executions):
        status='INVALID_ARGUMENTS'
    elif kind == 'no_action':
        status='NEGATIVE_PASS' if not actual and not turn.pending else 'UNEXPECTED_ACTION'
    elif kind == 'clarification':
        status='CLARIFICATION_PASS' if not actual and has_pending and correct_pending else 'REACTION_MISMATCH'
    elif not case.get('expected_tool'):
        status='UNEXPECTED_ACTION' if actual else 'DIALOGUE_RESPONSE' if turn.text.strip() else 'EMPTY_RESPONSE'
    elif actual:
        matches=[(c['tool'],c['arguments'].get('action'))==wanted for c in actual]
        allowed_reads = {('discord','music_queue_status'),('music','background_status'),('overlay','list')}
        extras = [c for c,m in zip(actual,matches) if not m]
        allowed = sum(matches) == 1 and all((c['tool'],c['arguments'].get('action')) in allowed_reads for c in extras)
        if not all(matches) and not allowed: status='ROUTE_MISMATCH' 
        elif differences: status='ARGUMENT_MISMATCH'
        else: status='CONFIRMATION_REQUIRED' if turn.pending else 'ARGUMENTS_PASS' if required else 'ROUTE_PASS'
    else:
        status=('NEEDS_INPUT' if correct_pending else 'REACTION_MISMATCH') if has_pending else 'NO_ROUTE'
    return status,differences,required
