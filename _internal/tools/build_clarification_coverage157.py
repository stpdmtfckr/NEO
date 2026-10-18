"""Generate explicit typed dialogue fixtures, bound to registered schemas."""
import json
import tempfile
from pathlib import Path
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.runtime.clarification_contracts156 import index,partial
from jarvis.runtime.clarification_contracts157 import extra_partial
from jarvis.runtime.command_contracts150 import _missing_index
from jarvis.runtime.canonical_commands import parse_canonical_command
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import prepare_dialogue
from jarvis.tools.argument_validation import validate
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
ROOT=Path(__file__).resolve().parents[1]


def build():
    from jarvis.runtime.script_requests157 import HEADS as scripts
    old=json.loads((ROOT/'tests/fixtures/clarification_sequences156.json').read_text(encoding='utf-8'))
    samples={'name':'NEO QA','path':r'C:\NEO_QA\folder','absolute_source':r'C:\NEO_QA\fixture.txt','text':'NEO literal payload','percent156':'35','bounded_index':'2','filename':'renamed.txt','email':'neo@example.invalid','url':'https://example.invalid/','positive_integer':'2','clock':'12:30','open_kind':'2','confirm':'yes','overlay_position156':'top_right','date157':'2026-10-27','host157':'example.invalid','hotkey157':'ctrl+shift+s'}
    boolean=('да','yes','ja','oui','sí','sì','sim');button=('левая','left','links','gauche','izquierdo','sinistro','esquerdo')
    short=(('громкость','будильник','напомни','переведи','измени таймер'),('volume','alarm','remind me','translate','reschedule timer'),('lautstärke','wecker','erinnere mich','übersetze','verschiebe den timer'),('volume','réveil','rappelle-moi','traduis','reprogramme le minuteur'),('volumen','alarma','recuérdame','traduce','reprograma el temporizador'),('volume','sveglia','ricordami','traduci','riprogramma il timer'),('volume','alarme','lembra-me','traduz','reprograma o temporizador'))
    durations=('15 минут','15 minutes','15 Minuten','15 minutes','15 minutos','15 minuti','15 minutos')
    plans=('покажи запущенные приложения; покажи оборудование','show active applications; show hardware status','zeige aktive anwendungen; zeige den hardwarestatus','montre les applications actives; montre l’état du matériel','muestra las aplicaciones activas; muestra el estado del hardware','mostra le applicazioni attive; mostra lo stato dell’hardware','mostra as aplicações ativas; mostra o estado do hardware')
    sequences=[];rejected=[];coverage={};report=[]
    with external_io_blocked(),tempfile.TemporaryDirectory() as private:
        session=AtlasSession(Path(private)/'run')
        def parse(text,code):
            feature=parse_feature_command(text,code)
            if feature:return feature
            native=parse_canonical_command(text,code,lambda tool:session.agent.tools.get(tool).parameters)
            return (native.tool,dict(native.arguments or {})) if native and not native.error else None
        for i,code in enumerate(LANGUAGES):
            candidates=set(index(code))|set(_missing_index(code))|set(short[i])|set(scripts[i])|{r['request'] for r in old if r['language']==code}
            seen=set();rows=[]
            for phrase in sorted(candidates,key=lambda p:(len(p),p)):
                state=ConversationState(language=code);decision=prepare_dialogue(phrase,code,state,lambda p:parse(p,code))
                p=state.pending_intent
                if not p or not decision.reply:continue
                key=(p.tool,p.arguments.get('action',''),tuple((s.name,s.kind) for s in p.slots))
                if key in seen:continue
                seen.add(key)
                initial_tool,initial_action=p.tool,p.arguments.get('action','')
                answers=[];questions=[decision.reply];turns=[{'phrase':phrase,'expected_tool':initial_tool,'expected_action':initial_action,'expected_kind':'clarification'}]
                for step in range(8):
                    p=state.pending_intent
                    if p is None:break
                    s=p.missing()
                    if s is None:break
                    if s.kind=='duration':answer=durations[i]
                    elif s.kind=='bool157':answer=boolean[i]
                    elif s.kind=='mouse_button157':answer=button[i]
                    elif s.kind=='calls157':
                        pack=json.loads((ROOT/'jarvis/locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
                        candidates1=[x for x in pack['action_phrases']['application.list'] if '{' not in x and parse(x,code)==('application',{'action':'list'})]
                        candidates2=[x for x in pack['action_phrases']['hardware.status'] if '{' not in x and parse(x,code)==('hardware',{'action':'status'})]
                        answer=candidates1[0]+'; '+candidates2[0] if candidates1 and candidates2 else plans[i]
                    elif s.kind.startswith(('number157:','int157:')):
                        _,lo,hi=s.kind.split(':');answer=str(int(max(float(lo),min(2,float(hi))))) if s.kind.startswith('int') else str(max(float(lo),min(1,float(hi))))
                    else:answer=samples.get(s.kind)
                    if answer is None:break
                    answers.append(answer)
                    decision=prepare_dialogue(answer,code,state,lambda p:parse(p,code))
                    if decision.reply:questions.append(decision.reply)
                    if state.pending_intent:
                        turns.append({'phrase':answer,'expected_tool':state.pending_intent.tool,'expected_action':state.pending_intent.arguments.get('action',''),'expected_kind':'clarification'})
                if decision.feature is None:
                    rejected.append({'locale':code,'phrase':phrase,'tool':initial_tool,'action':initial_action,'reason':'not completed','questions':questions});continue
                tool,args=decision.feature
                errors=validate(args,session.agent.tools.get(tool).parameters)
                if errors:
                    rejected.append({'locale':code,'phrase':phrase,'reason':'schema','errors':errors});continue
                sequence={'language':code,'request':phrase,'replies':answers,'questions':questions,'expected_tool':tool,'expected_arguments':args}
                sequences.append(sequence)
                sid=f'clarification157_{code}_{len(sequences)}'
                turns.append({'phrase':answers[-1],'expected_tool':tool,'expected_action':args.get('action',''),'expected_args':args})
                for turn,entry in enumerate(turns):report.append({'locale':code,**entry,'source':'clarification157','scenario_id':sid,'keep_context':turn>0})
                rows.append({'request':phrase,'tool':tool,'action':args.get('action',''),'slot_types':[list(v) for v in key[2]],'steps':len(turns)})
            coverage[code]=rows
        session.finish()
    (ROOT/'tests/fixtures/clarification_sequences157.json').write_text(json.dumps(sequences,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'config/regression157_context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    payload={'languages':coverage,'sequences':len(sequences),'steps':len(report),'actions':len({(r['expected_tool'],r['expected_arguments'].get('action','')) for r in sequences}),'rejected':rejected}
    (ROOT/'config/clarification_coverage157.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in payload.items() if k!='languages'},ensure_ascii=False,indent=2))

if __name__=='__main__':build()
