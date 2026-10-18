"""Rebuild auditable missing-slot coverage and native multi-turn fixtures.
Runs deterministic parsing only inside the isolated AtlasSession profile.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import re
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def build():
    from jarvis.runtime.clarification_contracts156 import index
    from jarvis.runtime.canonical_commands import parse_canonical_command, templates
    from jarvis.runtime.command_contracts150 import _missing_index
    from jarvis.runtime.command_contracts149 import missing_contract
    from jarvis.runtime.conversation import ConversationState
    from jarvis.runtime.dialogue_tasks import prepare_dialogue
    from jarvis.runtime.feature_commands import parse_feature_command
    from jarvis.runtime.natural_commands import LANGUAGES, norm
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    extras=(
        ('открой','открой папку','открой файл','открой приложение','закрой приложение','создай заметку','скопируй файл','перемести файл','переименуй файл','таймер'),
        ('open','open a folder','open a file','open an application','close an application','create a note','copy a file','move a file','rename a file','timer'),
        ('öffne','öffne einen Ordner','öffne eine Datei','öffne eine Anwendung','schließe eine Anwendung','erstelle eine Notiz','kopiere eine Datei','verschiebe eine Datei','benenne eine Datei','Timer'),
        ('ouvre','ouvre un dossier','ouvre un fichier','ouvre une application','ferme une application','crée une note','copie un fichier','déplace un fichier','renomme un fichier','minuteur'),
        ('abre','abre una carpeta','abre un archivo','abre una aplicación','cierra una aplicación','crea una nota','copia un archivo','mueve un archivo','renombra un archivo','temporizador'),
        ('apri','apri una cartella','apri un file','apri una applicazione','chiudi una applicazione','crea una nota','copia un file','sposta un file','rinomina un file','timer'),
        ('abre','abre uma pasta','abre um ficheiro','abre uma aplicação','fecha uma aplicação','cria uma nota','copia um ficheiro','move um ficheiro','renomeia um ficheiro','temporizador'),
    )
    durations=('15 минут','15 minutes','15 Minuten','15 minutes','15 minutos','15 minuti','15 minutos')
    samples={'name':'NEO fixture','path':r'C:\NEO_QA\folder','absolute_source':r'C:\NEO_QA\fixture.txt','text':'NEO literal payload','percent156':'35','bounded_index':'2','filename':'renamed.txt','email':'neo@example.invalid','url':'https://example.invalid/','positive_integer':'2','clock':'12:30','open_kind':'2','confirm':'yes','overlay_position156':'top_right'}
    coverage={};scenarios=[];seen_scenarios=set();rejected=[]
    with tempfile.TemporaryDirectory(prefix='neo_clarification156_') as private, external_io_blocked():
        session=AtlasSession(Path(private)/'run')
        try:
            for i,code in enumerate(LANGUAGES):
                pack=json.loads((ROOT/'jarvis/locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
                candidates=set(index(code)) | set(_missing_index(code)) | set(extras[i])
                for row in templates(code):
                    match=re.search(r'\{[a-z_]+\}',row.phrase)
                    if match:
                        head=row.phrase[:match.start()].strip()
                        if len(head.split())>=2:candidates.add(head)
                for phrases in pack.get('action_phrases',{}).values():
                    for phrase in phrases:
                        if '{' not in phrase and missing_contract(phrase,code):candidates.add(phrase)
                def parse(text):
                    feature=parse_feature_command(text,code)
                    if feature:return feature
                    native=parse_canonical_command(text,code,lambda tool:session.agent.tools.get(tool).parameters)
                    return (native.tool,dict(native.arguments or {})) if native and not native.error else None
                rows=[]
                for phrase in sorted(candidates,key=lambda p:(len(p),p)):
                    state=ConversationState(language=code)
                    decision=prepare_dialogue(phrase,code,state,parse,now=10)
                    pending=state.pending_intent
                    if not pending or decision.feature or not decision.reply:continue
                    action=pending.tool+'.'+str(pending.arguments.get('action',''))
                    row={'phrase':phrase,'tool':pending.tool,'action':pending.arguments.get('action',''),'fixed_arguments':dict(pending.arguments),'slots':[asdict(slot) for slot in pending.slots],'question':decision.reply}
                    replies=[];questions=[decision.reply]
                    for step in range(10):
                        active=state.pending_intent
                        if active is None:break
                        slot=active.missing()
                        answer=durations[i] if slot.kind=='duration' else samples.get(slot.kind)
                        if not answer:break
                        replies.append(answer)
                        decision=prepare_dialogue(answer,code,state,parse,now=11+step)
                        if decision.reply:questions.append(decision.reply)
                    if decision.feature is None:
                        rejected.append({'language':code,'phrase':phrase,'action':action,'reason':'fixture could not complete','questions':questions});continue
                    tool,args=decision.feature
                    from jarvis.tools.argument_validation import validate
                    errors=validate(args,session.agent.tools.get(tool).parameters)
                    if errors:
                        rejected.append({'language':code,'phrase':phrase,'action':action,'reason':'schema','errors':errors});continue
                    row['result_tool']=tool;row['result_action']=args.get('action','')
                    rows.append(row)
                    key=(code,action,tuple((slot['name'],slot['kind']) for slot in row['slots']))
                    if key not in seen_scenarios:
                        seen_scenarios.add(key)
                        scenarios.append({'language':code,'request':phrase,'replies':replies,'questions':questions,'expected_tool':tool,'expected_arguments':args})
                coverage[code]={'supported_actions':len({r['result_tool']+'.'+r['result_action'] for r in rows}),'literal_heads':len(rows),'contracts':rows}
        finally:session.finish()
    body={'version':156,'mode':'deterministic_missing_slots','claim':'Exact reviewed native request heads and typed follow-ups; not arbitrary fragments or unrestricted functions. Folder window context is separately tested on simulated Explorer snapshots.','languages':coverage,'scenarios':len(scenarios),'actions_total':len({row['result_tool']+'.'+row['result_action'] for c in coverage.values() for row in c['contracts']}),'fixture_rejections':rejected}
    folder_requests=('закрой папку','close a folder','schließe einen Ordner','ferme un dossier','cierra una carpeta','chiudi una cartella','fecha uma pasta')
    folder_rows=[{'hwnd':11,'pid':7,'created':10.0,'title':'Downloads'},{'hwnd':12,'pid':7,'created':10.0,'title':'Documents'}]
    contextual=[]
    for code,request in zip(LANGUAGES,folder_requests):
        for count in (0,1,2):
            state=ConversationState(language=code)
            first=prepare_dialogue(request,code,state,lambda text:None,now=10,folder_context=lambda:folder_rows[:count])
            replies=['2'] if count==2 else []
            result=prepare_dialogue('2',code,state,lambda text:None,now=11) if count==2 else first
            contextual.append({'language':code,'request':request,'snapshot':folder_rows[:count],'initial_question':first.reply,'replies':replies,'expected_feature':result.feature,'expected_no_action':count==0})
    body.update(context_actions=['desktop.close_folder'],actions_total_with_context=body['actions_total']+1,contextual_scenarios=contextual,
                limits={'folder_selection_max_windows':40,'folder_scope':'Explorer windows; closing a window also closes its contained tabs','folder_validation':'Recheck same HWND, Explorer process, process creation time and window title before WM_CLOSE. No process termination or fuzzy fallback.', 'unit_test_meaning':'Routing, slot validation and proposed tool arguments only; no actual desktop mutation or live integrations.'})
    destination=ROOT/'config/clarification_coverage156.json';destination.write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'tests/fixtures/clarification_sequences156.json').write_text(json.dumps(scenarios,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'languages':{code:{k:v for k,v in entry.items() if k!='contracts'} for code,entry in coverage.items()},'scenarios':len(scenarios),'actions_total':body['actions_total'],'fixture_rejections':len(rejected)},ensure_ascii=False,indent=2))
    if rejected:print(json.dumps(rejected[:30],ensure_ascii=False,indent=2))

if __name__=='__main__':build()
