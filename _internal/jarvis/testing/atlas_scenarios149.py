"""Independent multi-turn, negative and outcome contracts for the atlas runner.

Expected values are fixed here, not copied from the assistant's output. Tool
results used for timer ID continuity are explicitly simulated; no real account
or Windows action is authorized by these tests.
"""
from __future__ import annotations
from jarvis.runtime.natural_commands import LANGUAGES

TIMER=('таймер','timer','Timer','minuteur','temporizador','timer','temporizador')
ZERO=('0 секунд','0 seconds','0 Sekunden','0 secondes','0 segundos','0 secondi','0 segundos')
DURATION=('15 минут','15 minutes','15 Minuten','15 minutes','15 minutos','15 minuti','15 minutos')
CORRECTION=('нет, я имел в виду 20 минут','no, I meant 20 minutes','nein, ich meinte 20 Minuten','non, je voulais dire 20 minutes','no, quería decir 20 minutos','no, intendevo 20 minuti','não, queria dizer 20 minutos')
REPEAT=('повтори вопрос','repeat the question','wiederhole die Frage','répète la question','repite la pregunta','ripeti la domanda','repete a pergunta')
CANCEL=('отмена','cancel','abbrechen','annuler','cancelar','annulla','cancelar')
OPEN=('открой приложение, пожалуйста','open application, please','öffne die Anwendung, bitte','ouvre une application, s’il te plaît','abre la aplicación, por favor','apri l’applicazione, per favore','abre a aplicação, por favor')
NOTE=('создай заметку','create a note','erstelle eine Notiz','crée une note','crea una nota','crea una nota','cria uma nota')
SEARCH=('найди трек в Spotify','find the song on Spotify','finde das Lied auf Spotify','trouve la chanson sur Spotify','busca la canción en Spotify','trova la canzone su Spotify','procura a música no Spotify')
LITERAL=('Нео, не удаляй файл, пожалуйста.','NEO, do not delete the file, please.','NEO, lösche die Datei nicht, bitte.','NEO, ne supprime pas le fichier, s’il te plaît.','NEO, no borres el archivo, por favor.','NEO, non eliminare il file, per favore.','NEO, não apagues o ficheiro, por favor.')
NEGATIVE=[
 ('не открывай браузер','do not open the browser','öffne den Browser nicht','n’ouvre pas le navigateur','no abras el navegador','non aprire il browser','não abras o navegador'),
 ('не удаляй файл','do not delete the file','lösche die Datei nicht','ne supprime pas le fichier','no borres el archivo','non eliminare il file','não apagues o ficheiro'),
 ('не включай музыку','do not play music','spiele keine Musik','ne joue pas de musique','no reproduzcas música','non riprodurre musica','não reproduzas música'),
 ('почему ты закрыл браузер','why did you close the browser','warum hast du den Browser geschlossen','pourquoi as-tu fermé le navigateur','por qué cerraste el navegador','perché hai chiuso il browser','porque fechaste o navegador'),
 ('почему ты остановил музыку','why did you stop the music','warum hast du die Musik gestoppt','pourquoi as-tu arrêté la musique','por qué paraste la música','perché hai fermato la musica','porque paraste a música'),
 ('он сказал «удали файл»','he said “delete the file”','er sagte „lösche die Datei“','il a dit « supprime le fichier »','él dijo «borra el archivo»','ha detto «elimina il file»','ele disse «apaga o ficheiro»'),
 ('это не команда, а пример: выключи компьютер','this is an example, not a command: shut down the computer','dies ist ein Beispiel, kein Befehl: fahre den Computer herunter','ceci est un exemple, pas une commande : éteins l’ordinateur','esto es un ejemplo, no una orden: apaga el ordenador','questo è un esempio, non un comando: spegni il computer','isto é um exemplo, não um comando: desliga o computador'),
]

def cases(code: str):
    i=LANGUAGES.index(code)
    def row(text,tool='',action='',kind='',*,sid='',continuation=False,args=None,mock=None):
        return {'locale':code,'phrase':text,'expected_tool':tool,'expected_action':action,
                'source':'dialogue_scenario' if sid else 'negative_control','category':sid,
                'scenario_id':sid,'keep_context':continuation,'expected_kind':kind,
                'expected_args':args,'mock_result':mock}
    yield row(TIMER[i],'timer','timer','clarification',sid='timer_correction')
    yield row(ZERO[i],'timer','timer','clarification',sid='timer_correction',continuation=True)
    yield row(REPEAT[i],'timer','timer','clarification',sid='timer_correction',continuation=True)
    yield row(DURATION[i],'timer','timer',sid='timer_correction',continuation=True,args={'seconds':900},
              mock={'success':True,'text':'Timer: 900 s.','data':{'id':'NEO_QA_TIMER','seconds':900,'verified':True}})
    yield row(CORRECTION[i],'timer','reschedule',sid='timer_correction',continuation=True,args={'id':'NEO_QA_TIMER','seconds':1200},
              mock={'success':True,'text':'Timer: 1200 s.','data':{'id':'NEO_QA_TIMER','seconds':1200,'verified':True}})
    yield row(OPEN[i],'application','open','clarification',sid='application_name')
    yield row(REPEAT[i],'application','open','clarification',sid='application_name',continuation=True)
    yield row('NEO QA','application','open',sid='application_name',continuation=True,args={'name':'NEO QA'})
    yield row(SEARCH[i],'spotify','search_track','clarification',sid='search_not_play')
    yield row('Imagine','spotify','search_track',sid='search_not_play',continuation=True,args={'query':'Imagine'})
    yield row(NOTE[i],'shelf','create','clarification',sid='literal_note')
    yield row('NEO QA','shelf','create','clarification',sid='literal_note',continuation=True)
    yield row(LITERAL[i],'shelf','create',sid='literal_note',continuation=True,args={'kind':'note','title':'NEO QA','text':LITERAL[i]})
    yield row(TIMER[i],'timer','timer','clarification',sid='cancel_pending')
    yield row(CANCEL[i],kind='no_action',sid='cancel_pending',continuation=True)
    for n in NEGATIVE: yield row(n[i],kind='no_action')


def outcome_cases(code: str):
    """Formatter-only cases: never represented as Agent/desktop executions."""
    common={'content_provider':'yandex_music','provider':'yandex_music','title':'NEO QA','artist':'QA Artist','verified':True}
    rows=[('pause_done','pause',True,{},'pause'),('pause_already','pause',True,{'unchanged':True},'already'),
          ('play_done','play',True,{},'play'),('stop_done','stop',True,{},'stop'),
          ('next_done','next',True,{},'next'),('unknown','next',True,{'verified':False,'outcome_unknown':True},'unknown'),
          ('network','play',False,{'error_kind':'network'},'network'),
          ('search','search_track',True,{'search_only':True,'playback_changed':False},'search')]
    for name,action,success,data,kind in rows:
        yield {'locale':code,'phrase':'[outcome fixture: '+name+']','expected_tool':'yandex_music',
               'expected_action':action,'source':'outcome_fixture','category':name,
               'outcome_fixture':{'action':action,'success':success,'data':dict(common,**data),'kind':kind}}
