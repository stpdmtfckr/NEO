"""Inventory real registry actions and native examples; gaps stay visible.

This is a catalogue inventory, not proof of desktop execution. Run production
Agent examples through the full atlas; incomplete requests must expose a typed
pending target matching the intended tool/action.
"""
from __future__ import annotations
from pathlib import Path
from collections import defaultdict
import argparse,json,time
from jarvis.runtime.natural_commands import LANGUAGES,norm,strip_request_shell
from jarvis.runtime.canonical_commands import templates
from jarvis.runtime.command_contracts150 import _target
from jarvis.runtime.music_requests157 import FORMS as MUSIC
from jarvis.runtime.everyday_requests157 import FORMS as EVERYDAY,TARGETS
from jarvis.runtime.script_requests157 import HEADS as SCRIPT_HEADS
from jarvis.runtime.clarification_contracts157 import TYPED_EXAMPLES157
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
ROOT=Path(__file__).resolve().parents[1]

def build(output: Path, profile: Path):
    with external_io_blocked():
        s=AtlasSession(profile/str(time.time_ns()))
        actions={}
        for schema in s.agent.tools.schemas():
            fn=schema['function'];tool=fn['name'];spec=fn['parameters']
            enum=spec.get('properties',{}).get('action',{}).get('enum',[])
            if not enum:
                actions[f'{tool}.{tool}']={'tool':tool,'action':'','description':fn['description'],'actionless':True,'locales':{}}
            for action in enum:
                canonical=_target(tool,action)
                if canonical!=(tool,action):continue
                actions[f'{tool}.{action}']={'tool':tool,'action':action,'description':fn['description'],'locales':{}}
        s.finish()
    sources={code:defaultdict(set) for code in LANGUAGES}
    for code in LANGUAGES:
        data=json.loads((ROOT/'jarvis/locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
        for entry,phrases in data.get('action_phrases',{}).items():
            key='.'.join(_target(*entry.split('.',1)))
            if key not in actions:continue
            for text in phrases:sources[code][key].add(strip_request_shell(text,code).strip(' .!?'))
        for row in templates(code):
            key='.'.join(_target(*row.intent.split('.',1)))
            if key in actions:sources[code][key].add(strip_request_shell(row.phrase,code).strip(' .!?'))
        i=LANGUAGES.index(code)
        for action,rows in MUSIC.items():sources[code]['music.'+action].update(rows[i].split('|'))
        for intent, rows in TYPED_EXAMPLES157.items():sources[code][intent].update(rows[i])
        sources[code]['powershell.powershell'].update(head+': {script}' for head in SCRIPT_HEADS[i])
        if 'spotify.queue_status' in actions:
            suffix=('в Spotify','in Spotify','in Spotify','dans Spotify','en Spotify','in Spotify','no Spotify')[i]
            forms=MUSIC['queue_status'][i].split('|')
            sources[code]['spotify.queue_status'].update(p+' '+suffix for p in (forms[0],forms[-1]))
        for key,rows in EVERYDAY.items():
            tool,args=TARGETS[key];sources[code][tool+'.'+args['action']].update(rows[i].split('|'))
    missing=[];cases=[]
    for key,row in sorted(actions.items()):
        for code in LANGUAGES:
            phrases=sorted(sources[code][key],key=lambda x:(len(x.split()),len(x),x))
            # Independent examples retain user data placeholders and action.
            # Short means least-word existing meaningful phrase, not substrings.
            short=phrases[0] if phrases else ''
            expanded=max(phrases,key=lambda x:('{' in x,len(x)),default='')
            if expanded==short:expanded=next((x for x in reversed(phrases) if x!=short),'')
            row['locales'][code]={'short':short,'expanded':expanded,'native_examples':len(phrases),
                                  'status':'examples_present' if short and expanded else 'missing_example'}
            if not short or not expanded:missing.append({'intent':key,'locale':code,'short':bool(short),'expanded':bool(expanded)})
            for kind,phrase in [('short',short),('expanded',expanded)]:
                if phrase:cases.append({'locale':code,'phrase':phrase,'expected_tool':row['tool'],
                                       'expected_action':row['action'],'source':'function157_'+kind})
    for case in cases:
        if case['expected_tool']=='desktop' and case['expected_action']=='close_folder':
            fixture={'hwnd':157,'pid':157,'created':1.0,'title':'NEO QA Folder'}
            case['folder_windows_fixture']=[fixture]
            case['expected_args']={'query':json.dumps(fixture,ensure_ascii=False,separators=(',',':'))}
    payload={'version':157,'scope':'registered schema actions after published alias normalization plus actionless tools',
             'note':'Examples are declarations. Only matching Agent calls or typed pending target pass routing validation; generic clarification is a failure.',
             'tools':len({r['tool'] for r in actions.values()}),'actions':len(actions)-sum(bool(x.get('actionless')) for x in actions.values()),'function_count':len(actions),'locales':list(LANGUAGES),
             'action_language_pairs':len(actions)*len(LANGUAGES),'missing_examples':missing,'functions':actions}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'config/regression157_functions.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'tools':payload['tools'],'actions':payload['actions'],'missing_example_pairs':len(missing),'cases':len(cases)}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'config/function_coverage157.json');p.add_argument('--profile',type=Path,required=True)
    a=p.parse_args();build(a.output,a.profile)
