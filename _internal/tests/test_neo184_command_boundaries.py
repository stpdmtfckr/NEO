"""Independent non-execution and exact payload invariants, seven languages.
These expectations do not derive from observed router outputs.
No real handler, account, subprocess or network is permitted.
"""
import copy, json
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution


def serial_turn(session, text):
    session.calls.clear()
    session.executions.clear()
    turn = session.agent.handle(text)
    pending = session.agent.conversations.get('desktop').pending_intent
    return {'input': text, 'response': turn.text, 'outcome': turn.outcome,
            'calls': copy.deepcopy(session.calls), 'pending_confirmation': bool(turn.pending),
            'pending_intent': None if pending is None else {
                'tool': pending.tool, 'arguments': copy.deepcopy(pending.arguments),
                'missing': pending.missing().name if pending.missing() else None}}


D = {
 'ru': ('открой приложение Calculator', 'заблокируй компьютер', 'сделай скриншот', 'вставь из буфера обмена', 'очисти буфер обмена'),
 'en': ('open application Calculator', 'lock the computer', 'take a screenshot', 'paste from clipboard', 'clear clipboard'),
 'de': ('öffne die Anwendung Calculator', 'sperre den Computer', 'mache einen Screenshot', 'füge aus der Zwischenablage ein', 'leere die Zwischenablage'),
 'fr': ('ouvre l’application Calculator', 'verrouille l’ordinateur', 'fais une capture d’écran', 'colle depuis le presse-papiers', 'vide le presse-papiers'),
 'es': ('abre la aplicación Calculator', 'bloquea el ordenador', 'haz una captura de pantalla', 'pega desde el portapapeles', 'vacía el portapapeles'),
 'it': ('apri l’applicazione Calculator', 'blocca il computer', 'fai uno screenshot', 'incolla dagli appunti', 'svuota gli appunti'),
 'pt': ('abre a aplicação Calculator', 'bloqueia o computador', 'faz uma captura de ecrã', 'cola da área de transferência', 'limpa a área de transferência'),
}
T = {
 'ru': ('Не выполняй следующую команду: {}', 'В документе написано: «{}»', 'Что означает команда «{}»?', 'Пожалуйста, не выполняй следующую команду: {}'),
 'en': ('Do not execute the following command: {}', 'The document says: “{}”', 'What does the command “{}” mean?', 'Please, do not execute the following command: {}'),
 'de': ('Führe den folgenden Befehl nicht aus: {}', 'Im Dokument steht: „{}“', 'Was bedeutet der Befehl „{}“?', 'Bitte, führe den folgenden Befehl nicht aus: {}'),
 'fr': ('N’exécute pas la commande suivante : {}', 'Le document contient : « {} »', 'Que signifie la commande « {} » ?', 'S’il te plaît, n’exécute pas la commande suivante : {}'),
 'es': ('No ejecutes el siguiente comando: {}', 'El documento dice: «{}»', '¿Qué significa el comando «{}»?', 'Por favor, no ejecutes el siguiente comando: {}'),
 'it': ('Non eseguire il seguente comando: {}', 'Nel documento è scritto: «{}»', 'Cosa significa il comando «{}»?', 'Per favore, non eseguire il seguente comando: {}'),
 'pt': ('Não executes o seguinte comando: {}', 'O documento diz: «{}»', 'O que significa o comando «{}»?', 'Por favor, não executes o seguinte comando: {}'),
}
ROWS=[]
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('independent184')/'session')
  for d in s.agent.tools._tools.values(): d.handler=lambda a:ToolExecution(True,'QA inert',{'qa_simulated':True})
  def record(name,args,risk):s.calls.append({'tool':name,'arguments':copy.deepcopy(args),'risk':risk.label});return None
  s.agent.tools.set_execution_guard(record)
  yield s
  s.finish()

@pytest.mark.parametrize('lang,template,payload',[(l,i,p) for l in D for i in range(4) for p in D[l]])
def test_unequivocal_nonexecution(session,lang,template,payload):
 session.reset_case(lang)
 r={'locale':lang,'template':template,**serial_turn(session,T[lang][template].format(payload))};ROWS.append(r)
 assert not r['calls'] and not r['pending_confirmation'], r
 assert r['pending_intent'] is None, r

@pytest.mark.parametrize('lang,prefix', [('ru','напечатай'),('en','type'),('de','tippe'),('fr','écris'),('es','escribe'),('it','scrivi'),('pt','escreve')])
@pytest.mark.parametrize('payload',['Do not execute: open application Calculator','The document says: “clear clipboard”','No, do not open it!','Hello\u200bworld!'])
def test_literal_payload_is_not_reinterpreted(session,lang,prefix,payload):
 session.reset_case(lang)
 r={'locale':lang,'kind':'literal-payload',**serial_turn(session,prefix+' '+payload)};ROWS.append(r)
 assert len(r['calls'])==1 and not r['pending_confirmation'],r
 c=r['calls'][0]
 assert c['tool'] in {'ui','text_edit','input_control'} and c['arguments'].get('text')==payload,r

@pytest.mark.parametrize('lang',list(D))
@pytest.mark.parametrize('delimiter',[',',';',':','—',''])
@pytest.mark.parametrize('uppercase',[False,True])
def test_polite_negation_delimiters(session,lang,delimiter,uppercase):
 session.reset_case(lang)
 phrase=T[lang][3].format(D[lang][1]).replace(',',delimiter,1)
 if uppercase: phrase=phrase.upper()
 r={'locale':lang,'kind':'polite-negation-delimiter',**serial_turn(session,phrase)};ROWS.append(r)
 assert not r['calls'] and not r['pending_confirmation'] and r['pending_intent'] is None,r

MIXED={
 'ru':'Не открывай Steam, открой приложение Calculator',
 'en':"Don't open Steam, open application Calculator",
 'de':'Öffne Steam nicht, öffne die Anwendung Calculator',
 'fr':'N’ouvre pas Steam, ouvre l’application Calculator',
 'es':'No abras Steam, abre la aplicación Calculator',
 'it':'Non aprire Steam, apri l’applicazione Calculator',
 'pt':'Não abras Steam, abre a aplicação Calculator',
}
@pytest.mark.parametrize('lang',list(D))
@pytest.mark.parametrize('polite',[False,True])
def test_mixed_negation_executes_only_explicit_allowed_clause(session,lang,polite):
 session.reset_case(lang)
 phrase=MIXED[lang]
 if polite: phrase=T[lang][3].split(',',1)[0]+', '+phrase
 r={'locale':lang,'kind':'mixed-negation',**serial_turn(session,phrase)};ROWS.append(r)
 assert [(c['tool'],c['arguments']) for c in r['calls']]==[('application',{'action':'open','name':'Calculator'})],r

@pytest.mark.parametrize('lang',list(D))
def test_negative_outer_scope_of_dictation_never_runs(session,lang):
 session.reset_case(lang)
 verbs={'ru':'напечатай','en':'type','de':'tippe','fr':'écris','es':'escribe','it':'scrivi','pt':'escreve'}
 phrase=T[lang][3].format(verbs[lang]+' Hello, World!')
 r={'locale':lang,'kind':'negative-outer-dictation',**serial_turn(session,phrase)};ROWS.append(r)
 assert not r['calls'] and not r['pending_confirmation'] and r['pending_intent'] is None,r

@pytest.mark.parametrize('lang,opening',[
 ('ru','создай заметку'),('en','create a note'),('de','erstelle eine Notiz'),
 ('fr','crée une note'),('es','crea una nota'),('it','crea una nota'),('pt','cria uma nota')])
def test_requested_note_content_is_literal_even_when_it_quotes_a_command(session,lang,opening):
 session.reset_case(lang)
 a=serial_turn(session,opening);ROWS.append({'locale':lang,'kind':'note-start',**a})
 assert a['pending_intent'] and a['pending_intent']['missing']=='title',a
 b=serial_turn(session,'QA_Literal_Note_731');ROWS.append({'locale':lang,'kind':'note-title',**b})
 assert b['pending_intent'] and b['pending_intent']['missing']=='text',b
 payload=T[lang][0].format(D[lang][0])+'\u200b!'
 c=serial_turn(session,payload);ROWS.append({'locale':lang,'kind':'note-payload',**c})
 assert len(c['calls'])==1 and c['calls'][0]['tool']=='shelf',c
 assert c['calls'][0]['arguments'].get('text')==payload,c
