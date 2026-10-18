"""Short-lived, conversation-owned references to verified concrete objects.

The durable journal is not execution authority. Only evidence from successful
turns in this conversation can bind a pronoun. File identity is rechecked before
execution and after confirmation; a replaced/edited/missing file is never used.
"""
from __future__ import annotations
import json
from pathlib import Path
import ntpath
import time
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell

TTL = 120.0


def identity(path):
    try:
        stat = Path(path).stat()
        return [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_mode]
    except OSError:
        return None


def remember(state, tool, arguments, execution, now=None):
    now = time.monotonic() if now is None else now
    if not getattr(execution,'success',False):
        return
    data = getattr(execution,'data',None)
    payload = data if isinstance(data,dict) else {}
    if any(payload.get(k) for k in ('queued','pending','ui_request','launch_pending','requires_clarification','requires_confirmation')) or payload.get('completed') is False or payload.get('verified') is False:
        return
    action = arguments.get('action','')
    rows = [dict(r) for r in getattr(state,'objects157',[]) if now-r.get('at',0)<=TTL and r.get('language')==state.language]
    if tool == 'shelf' and action == 'delete':
        oid=str(arguments.get('id','') or payload.get('id',''))
        state.objects157=[r for r in rows if not (r['kind']=='note' and r['value']==oid)]
        return
    if tool == 'filesystem' and action in {'delete','delete_permanent'}:
        gone = str(arguments.get('source',''))
        state.objects157 = [r for r in rows if r['value'] != gone]
        return
    if tool not in {'filesystem','desktop','search','shelf','overlay'}:
        return
    candidates = data if isinstance(data,list) else payload.get('results') if isinstance(payload.get('results'),list) else [payload]
    fresh=[]
    for item in candidates[:12]:
        item = {'path':item} if isinstance(item,str) else item
        if not isinstance(item,dict):
            continue
        if tool in {'filesystem','desktop','search'}:
            path = str(item.get('path','') or (item.get('value','') if item.get('kind')=='path' else '') or '')
            if not path and len(candidates)==1 and tool=='filesystem' and action in {'read_text','info','exists','hash','reveal','list','tree'}:
                path=str(arguments.get('source',''))
            fingerprint=identity(path) if path else None
            if fingerprint is None:
                continue
            kind='folder' if Path(path).is_dir() else 'file'
            row={'kind':kind,'value':path,'label':ntpath.basename(path) or path,'identity':fingerprint}
        elif tool=='shelf':
            oid=str(item.get('id','') or arguments.get('id',''))
            title=str(item.get('title','') or arguments.get('title',''))
            if not oid or not title or action=='delete':
                continue
            row={'kind':'note','value':oid,'label':title,'version':item.get('version',arguments.get('version'))}
        else:
            continue
        fresh.append({**row,'at':now,'language':state.language})
    if fresh:
        keys={(r['kind'],r['value']) for r in fresh}
        # A listing is a candidate set, not an arbitrary "last" result.
        state.objects157=(fresh+[r for r in rows if (r['kind'],r['value']) not in keys])[:16]


def fresh_objects(state, code, now):
    return [r for r in getattr(state,'objects157',[]) if r.get('language')==code and now-r.get('at',0)<=TTL]


def changed_message(code):
    return (
      'Объект изменился или больше недоступен. Укажи его заново — действие не выполнено.',
      'The object changed or is no longer available. Please identify it again; nothing was executed.',
      'Das Objekt wurde geändert oder ist nicht mehr verfügbar. Bitte wähle es erneut; nichts wurde ausgeführt.',
      'L’objet a changé ou n’est plus disponible. Indique-le à nouveau ; rien n’a été exécuté.',
      'El objeto cambió o ya no está disponible. Indícalo de nuevo; no se ejecutó nada.',
      'L’oggetto è cambiato o non è più disponibile. Indicalo di nuovo; non ho eseguito nulla.',
      'O objeto mudou ou já não está disponível. Indica-o novamente; nada foi executado.',
    )[LANGUAGES.index(code)]


def valid_guard(guard):
    if not guard:
        return True
    return bool(time.monotonic()<=guard.get('expires_at',0) and identity(guard.get('path',''))==guard.get('identity'))


_PRONOUNS=(('его','ее','её','это','этот файл','эту папку'),('it','that file','that folder'),('sie','es','diese datei','diesen ordner'),('le','la','ça','cela','ce fichier','ce dossier'),('lo','la','ese archivo','esa carpeta'),('lo','la','quel file','quella cartella'),('isso','esse ficheiro','essa pasta'))
_VERBS={
 'open': ('открой','open','öffne','ouvre','abre','apri','abre'),
 'read_text': ('прочитай','read','lies','lis','lee','leggi','lê'),
 'info': ('покажи информацию о','show details of','zeige informationen zu','montre les détails de','muestra información de','mostra i dettagli di','mostra informações de'),
 'copy': ('скопируй','copy','kopiere','copie','copia','copia','copia'),
 'move': ('перемести','move','verschiebe','déplace','mueve','sposta','move'),
 'rename': ('переименуй','rename','benenne','renomme','renombra','rinomina','renomeia'),
 'delete': ('удали','delete','lösche','supprime','elimina','elimina','apaga'),
 'update': ('перепиши','rewrite','überarbeite','réécris','reescribe','riscrivi','reescreve'),
}
_NOTES=('эту заметку','that note','diese notiz','cette note','esa nota','quella nota','essa nota')


def resolve(text, code, state, now, ttl):
    from jarvis.runtime.dialogue_tasks import DialogueDecision, PendingIntent, Slot
    value=norm(strip_request_shell(text,code));i=LANGUAGES.index(code)
    note_action=next((a for a,verbs in _VERBS.items() if a in {'read_text','open','update','delete','rename'} and value==norm(verbs[i]+' '+_NOTES[i])),None)
    action=note_action or next((a for a,verbs in _VERBS.items() if a!='update' and value in {norm(verbs[i]+' '+p) for p in _PRONOUNS[i]}),None)
    if action is None:
        return None
    candidates=[r for r in fresh_objects(state,code,now) if (r['kind']=='note' if note_action else r['kind'] in {'file','folder'} and (action!='read_text' or r['kind']=='file'))]
    # A pronoun in a different language/session, expired context, or an ambiguous
    # result never becomes a filename literally named "it".
    if not candidates:
        if note_action:
            if action=='rename':
                from jarvis.runtime.dialogue_messages import message
                return DialogueDecision(text,message('no_context',code),purpose='clarification')
            target_action={'read_text':'read','open':'read','rename':'update'}.get(action,action)
            # PHRASE185-CLARIFY-COPY-01: reading an existing note asks WHICH note, not how to name a new one.
            slots=(Slot('title','name',{'read':'note_which_read','update':'note_which_update','delete':'note_which_delete'}.get(target_action,'title')),)
            if action in {'rename','update'}:
                slots+=(Slot('content','text','text'),)
            pending=PendingIntent('shelf',{'action':target_action},slots,code,now+ttl)
            state.pending_intent=pending
            return DialogueDecision(text,pending.question(),purpose='clarification')
        pending=PendingIntent('filesystem',{'action':action},(Slot('source','absolute_source','source'),),code,now+ttl)
    elif len(candidates)>1:
        # PHRASE185-NOTE-CHOICE-01: show the real titles (not internal IDs); the exact
        # unique title and its number choose the same object.
        labels=[str(r.get('label') or r['value']) for r in candidates[:12]]
        choices=tuple(((label if labels.count(label)==1 else f"{label} [{n}]"),json.dumps(r,ensure_ascii=False)) for n,(label,r) in enumerate(zip(labels,candidates[:12]),1))
        pending=PendingIntent('object_request157',{'action':action},(Slot('object','choice','choose',choices=choices),),code,now+ttl)
    else:
        return bind(candidates[0],action,text,code,state,now,ttl)
    if action in {'copy','move','rename'} and pending.tool=='filesystem':
        pending.slots += (Slot('new_name','filename','new_name') if action=='rename' else Slot('destination','path','destination'),)
    state.pending_intent=pending
    return DialogueDecision(text,pending.question(),purpose='clarification')


def bind(row, action, text, code, state, now, ttl):
    from jarvis.runtime.dialogue_tasks import DialogueDecision, PendingIntent, Slot
    if row['kind']=='note':
        args={'action':{'read_text':'read','open':'read','rename':'update'}.get(action,action),'id':row['value']}
        if row.get('version') is not None:
            args['version']=row['version']
        if action in {'rename','update'}:
            slot=Slot('title','name','title') if action=='rename' else Slot('content','text','text')
            pending=PendingIntent('shelf',args,(slot,),code,now+ttl)
            state.pending_intent=pending
            return DialogueDecision(text,pending.question(),purpose='clarification')
        return DialogueDecision(text,feature=('shelf',args),purpose='command',from_context=True)
    guard={'path':row['value'],'identity':row['identity'],'expires_at':row['at']+TTL}
    if not valid_guard(guard):
        return DialogueDecision(text,changed_message(code),purpose='clarification')
    args={'action':action,'source':row['value']}
    if action in {'copy','move','rename'}:
        slot=Slot('new_name','filename','new_name') if action=='rename' else Slot('destination','path','destination')
        pending=PendingIntent('filesystem',args,(slot,),code,now+ttl,guard157=guard)
        state.pending_intent=pending
        return DialogueDecision(text,pending.question(),purpose='clarification')
    return DialogueDecision(text,feature=('filesystem',args),purpose='command',from_context=True,guard157=guard)
