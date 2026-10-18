"""Reviewed lexical and infinitive command forms, without polite wrappers."""
from pathlib import Path
import json
import re

LANGS = ('ru','en','de','fr','es','it','pt')
# imperative=infinitive;synonymous imperative;synonymous infinitive
MORPH = {
'ru': '''включи=включить;активируй;активировать
восстанови=восстановить;верни;вернуть
вставь=вставить;добавь;добавить
выбери=выбрать;укажи;указать
выведи=вывести;отобрази;отобразить
выдели=выделить;отметь;отметить
выключи=выключить;отключи;отключить
выполни=выполнить;запусти;запустить
вычисли=вычислить;рассчитай;рассчитать
добавь=добавить;допиши;дописать
заблокируй=заблокировать;включи блокировку;включить блокировку
забудь=забыть;удали из памяти;удалить из памяти
заверши=завершить;останови;остановить
закрепи=закрепить;зафиксируй;зафиксировать
закрой=закрыть;заверши;завершить
замени=заменить;измени;изменить
запиши=записать;сохрани;сохранить
заполни=заполнить;введи в;ввести в
запомни=запомнить;сохрани в памяти;сохранить в памяти
запусти=запустить;выполни;выполнить
исправь=исправить;скорректируй;скорректировать
нажми=нажать;задействуй;задействовать
найди=найти;отыщи;отыскать
напечатай=напечатать;введи;ввести
настрой=настроить;сконфигурируй;сконфигурировать
останови=остановить;выключи;выключить
открой=открыть;покажи;показать
отмени=отменить;аннулируй;аннулировать
очисти=очистить;сотри;стереть
переведи=перевести;переключи;переключить
перезагрузи=перезагрузить;перезапусти;перезапустить
переименуй=переименовать;переназови;переназвать
перейди=перейти;переключись;переключиться
переключи=переключить;измени;изменить
переключись=переключиться;перейди;перейти
перемести=переместить;перенеси;перенести
перенеси=перенести;перемести;переместить
покажи=показать;отобрази;отобразить
приглуши=приглушить;убавь;убавить
проверь=проверить;протестируй;протестировать
прочитай=прочитать;прочти;прочесть
распакуй=распаковать;извлеки;извлечь
сгруппируй=сгруппировать;объедини;объединить
сделай=сделать;переведи;перевести
скопируй=скопировать;дублируй;дублировать
создай=создать;сформируй;сформировать
увеличь=увеличить;повысь;повысить
удали=удалить;убери;убрать
упакуй=упаковать;сожми;сжать
установи=установить;задай;задать''',
'fr': '''active=activer;mets en marche;mettre en marche
ajoute=ajouter;insère;insérer
annule=annuler;révoque;révoquer
appuie=appuyer;presse;presser
arrête=arrêter;stoppe;stopper
atténue=atténuer;réduis;réduire
augmente=augmenter;rehausse;rehausser
baisse=baisser;réduis;réduire
bascule=basculer;inverse;inverser
calcule=calculer;détermine;déterminer
cherche=chercher;recherche;rechercher
choisis=choisir;sélectionne;sélectionner
clique=cliquer;appuie;appuyer
colle=coller;insère;insérer
compresse=compresser;archive;archiver
configure=configurer;paramètre;paramétrer
copie=copier;recopie;recopier
corrige=corriger;rectifie;rectifier
coupe=couper;désactive;désactiver
crée=créer;génère;générer
démarre=démarrer;lance;lancer
déplace=déplacer;transfère;transférer
efface=effacer;supprime;supprimer
envoie=envoyer;achemine;acheminer
extrais=extraire;décompresse;décompresser
exécute=exécuter;lance;lancer
ferme=fermer;referme;refermer
force=forcer;impose;imposer
insère=insérer;ajoute;ajouter
lance=lancer;démarre;démarrer
lis=lire;affiche;afficher
mets=mettre;place;placer
montre=montrer;affiche;afficher
oublie=oublier;efface de la mémoire;effacer de la mémoire
ouvre=ouvrir;affiche;afficher
passe=passer;va;aller
place=placer;déplace;déplacer
redémarre=redémarrer;relance;relancer
regroupe=regrouper;rassemble;rassembler
remplace=remplacer;substitue;substituer
remplis=remplir;complète;compléter
renomme=renommer;change le nom de;changer le nom de
retiens=retenir;mémorise;mémoriser
rouvre=rouvrir;restaure;restaurer
règle=régler;ajuste;ajuster
rétablis=rétablir;restaure;restaurer
saisis=saisir;écris;écrire
supprime=supprimer;efface;effacer
sélectionne=sélectionner;choisis;choisir
termine=terminer;arrête;arrêter
teste=tester;vérifie;vérifier
trouve=trouver;cherche;chercher
verrouille=verrouiller;bloque;bloquer
vide=vider;efface;effacer
vérifie=vérifier;contrôle;contrôler
écris=écrire;saisis;saisir
épingle=épingler;fixe;fixer
éteins=éteindre;désactive;désactiver''',
'es': '''abre=abrir;muestra;mostrar
activa=activar;habilita;habilitar
agrupa=agrupar;reúne;reunir
ajusta=ajustar;establece;establecer
alterna=alternar;invierte;invertir
apaga=apagar;desactiva;desactivar
atenúa=atenuar;reduce;reducir
aumenta=aumentar;incrementa;incrementar
añade=añadir;agrega;agregar
baja=bajar;reduce;reducir
bloquea=bloquear;activa el bloqueo de;activar el bloqueo de
borra=borrar;elimina;eliminar
busca=buscar;localiza;localizar
calcula=calcular;determina;determinar
cambia=cambiar;modifica;modificar
cancela=cancelar;anula;anular
cierra=cerrar;clausura;clausurar
comprime=comprimir;empaqueta;empaquetar
comprueba=comprobar;verifica;verificar
configura=configurar;ajusta;ajustar
copia=copiar;duplica;duplicar
corrige=corregir;rectifica;rectificar
crea=crear;genera;generar
deshaz=deshacer;revierte;revertir
detén=detener;para;parar
ejecuta=ejecutar;inicia;iniciar
elige=elegir;selecciona;seleccionar
elimina=eliminar;borra;borrar
envía=enviar;dirige;dirigir
escribe=escribir;introduce;introducir
extrae=extraer;descomprime;descomprimir
fija=fijar;ancla;anclar
finaliza=finalizar;termina;terminar
fuerza=forzar;impón;imponer
haz=hacer;realiza;realizar
hiberna=hibernar;pon en hibernación;poner en hibernación
inicia=iniciar;arranca;arrancar
inserta=insertar;introduce;introducir
lee=leer;muestra;mostrar
muestra=mostrar;visualiza;visualizar
mueve=mover;traslada;trasladar
olvida=olvidar;borra de la memoria;borrar de la memoria
pega=pegar;inserta;insertar
pon=poner;coloca;colocar
pulsa=pulsar;presiona;presionar
recuerda=recordar;memoriza;memorizar
reinicia=reiniciar;rearranca;rearrancar
rellena=rellenar;completa;completar
renombra=renombrar;cambia el nombre de;cambiar el nombre de
restaura=restaurar;recupera;recuperar
selecciona=seleccionar;elige;elegir
silencia=silenciar;quita el sonido de;quitar el sonido de
sustituye=sustituir;reemplaza;reemplazar
vacía=vaciar;limpia;limpiar''',
'it': '''abbassa=abbassare;riduci;ridurre
aggiungi=aggiungere;inserisci;inserire
annulla=annullare;revoca;revocare
apri=aprire;visualizza;visualizzare
attiva=attivare;abilita;abilitare
aumenta=aumentare;incrementa;incrementare
avvia=avviare;lancia;lanciare
blocca=bloccare;attiva il blocco di;attivare il blocco di
calcola=calcolare;determina;determinare
cancella=cancellare;elimina;eliminare
cerca=cercare;ricerca;ricercare
chiudi=chiudere;termina;terminare
compila=compilare;riempi;riempire
comprimi=comprimere;archivia;archiviare
configura=configurare;imposta;impostare
controlla=controllare;verifica;verificare
copia=copiare;duplica;duplicare
correggi=correggere;rettifica;rettificare
crea=creare;genera;generare
digita=digitare;scrivi;scrivere
dimentica=dimenticare;rimuovi dalla memoria;rimuovere dalla memoria
disattiva=disattivare;disabilita;disabilitare
elimina=eliminare;cancella;cancellare
esegui=eseguire;avvia;avviare
estrai=estrarre;decomprimi;decomprimere
fai=fare;esegui;eseguire
ferma=fermare;arresta;arrestare
fissa=fissare;blocca;bloccare
forza=forzare;imponi;imporre
imposta=impostare;regola;regolare
incolla=incollare;inserisci;inserire
inserisci=inserire;aggiungi;aggiungere
invia=inviare;indirizza;indirizzare
leggi=leggere;mostra;mostrare
metti=mettere;porta;portare
mostra=mostrare;visualizza;visualizzare
passa=passare;vai;andare
premi=premere;aziona;azionare
raggruppa=raggruppare;riunisci;riunire
riavvia=riavviare;rilancia;rilanciare
ricorda=ricordare;memorizza;memorizzare
rinomina=rinominare;cambia il nome di;cambiare il nome di
ripristina=ripristinare;ristabilisci;ristabilire
scegli=scegliere;seleziona;selezionare
scrivi=scrivere;digita;digitare
seleziona=selezionare;marca;marcare
sostituisci=sostituire;rimpiazza;rimpiazzare
spegni=spegnere;disattiva;disattivare
sposta=spostare;trasferisci;trasferire
svuota=svuotare;pulisci;pulire
termina=terminare;arresta;arrestare
trova=trovare;cerca;cercare
verifica=verificare;controlla;controllare''',
'pt': '''abre=abrir;mostra;mostrar
acrescenta=acrescentar;adiciona;adicionar
agrupa=agrupar;reúne;reunir
alterna=alternar;inverte;inverter
apaga=apagar;elimina;eliminar
ativa=ativar;habilita;habilitar
aumenta=aumentar;incrementa;incrementar
baixa=baixar;reduz;reduzir
bloqueia=bloquear;ativa o bloqueio de;ativar o bloqueio de
calcula=calcular;determina;determinar
cancela=cancelar;anula;anular
clica=clicar;carrega;carregar
cola=colar;insere;inserir
coloca=colocar;põe;pôr
comprime=comprimir;compacta;compactar
configura=configurar;ajusta;ajustar
copia=copiar;duplica;duplicar
corrige=corrigir;retifica;retificar
cria=criar;gera;gerar
define=definir;estabelece;estabelecer
desfaz=desfazer;reverte;reverter
desliga=desligar;desativa;desativar
encerra=encerrar;termina;terminar
encontra=encontrar;localiza;localizar
envia=enviar;encaminha;encaminhar
escolhe=escolher;seleciona;selecionar
escreve=escrever;introduz;introduzir
esquece=esquecer;remove da memória;remover da memória
executa=executar;inicia;iniciar
extrai=extrair;descomprime;descomprimir
fecha=fechar;encerra;encerrar
fixa=fixar;prende;prender
força=forçar;impõe;impor
inicia=iniciar;arranca;arrancar
insere=inserir;adiciona;adicionar
limpa=limpar;esvazia;esvaziar
lê=ler;mostra;mostrar
memoriza=memorizar;guarda na memória;guardar na memória
mostra=mostrar;exibe;exibir
move=mover;desloca;deslocar
muda=mudar;altera;alterar
para=parar;interrompe;interromper
pesquisa=pesquisar;procura;procurar
preenche=preencher;completa;completar
prime=premir;pressiona;pressionar
procura=procurar;localiza;localizar
reduz=reduzir;diminui;diminuir
reinicia=reiniciar;rearranca;rearrancar
restaura=restaurar;repõe;repor
seleciona=selecionar;escolhe;escolher
silencia=silenciar;desliga o som de;desligar o som de
substitui=substituir;troca;trocar
termina=terminar;encerra;encerrar
verifica=verificar;confere;conferir''',
}
EN = '''append=add;append text;add text
calculate=compute;work out;determine
cancel=abort;call off;stop
check=verify;test;check out
clear=erase;delete;wipe
click=click the mouse;perform a click;press the mouse button
close=shut;dismiss;close down
compress=zip;pack;archive
configure=adjust;set up;configure settings for
copy=duplicate;make a copy of;copy out
correct=fix;rectify;revise
create=make;generate;create a new
extract=unpack;unzip;decompress
fill=populate;complete;fill in
find=locate;search for;look for
flush=clear;empty;purge
focus=activate;move focus to;put focus on
forget=remove from memory;erase from memory;delete from memory
group=combine;organize;group together
hibernate=put into hibernation;send into hibernation;activate hibernation on
increase=raise;turn up;boost
insert=add;place;put
list=show;display;enumerate
lock=secure;lock access to;enable the lock on
lower=reduce;decrease;turn down
move=transfer;relocate;send
mute=silence;turn off sound on;disable sound on
open=bring up;display;open up
paste=insert;place;paste in
pin=pin down;keep pinned;pin in place
press=hit;use;activate
put=place;send;switch
read=display contents of;show contents of;read out
remember=memorize;save in memory;store in memory
rename=change the name of;give a new name to;rename the
replace=substitute;swap;exchange
restart=reboot;relaunch;reboot the system on
restore=recover;bring back;reopen
reveal=show;locate;display
run=execute;start;launch
search=look;look up;search online
select=choose;use;switch to
send=route;direct;redirect
set=adjust;change;configure
show=display;present;report
start=launch;activate;run
stop=halt;deactivate;turn off
switch=move;change;switch over
terminate=kill;stop;end
toggle=flip;switch;invert
turn=switch;power;turn the
type=enter;write;type out
undo=revert;reverse;roll back
write=save;write text;output text'''
DE = '''beende=beenden;stoppe;stoppen
benenne=umbenennen;nenne;benennen
berechne=berechnen;ermittle;ermitteln
drücke=drücken;betätige;betätigen
entpacke=entpacken;extrahiere;extrahieren
erhöhe=erhöhen;steigere;steigern
ersetze=ersetzen;tausche;tauschen
erstelle=erstellen;erzeuge;erzeugen
finde=finden;suche; suchen
fülle=füllen;befülle;befüllen
gruppiere=gruppieren;bündele;bündeln
klicke=klicken;drücke;drücken
konfiguriere=konfigurieren;justiere;justieren
kopiere=kopieren;dupliziere;duplizieren
korrigiere=korrigieren;berichtige;berichtigen
leere=leeren;bereinige;bereinigen
leite=leiten;übertrage;übertragen
lies=lesen;zeige;zeigen
lösche=löschen;entferne;entfernen
markiere=markieren;selektiere;selektieren
packe=packen;komprimiere;komprimieren
prüfe=prüfen;kontrolliere;kontrollieren
schließe=schließen;beende;beenden
schreibe=schreiben;tippe;tippen
senke=senken;reduziere;reduzieren
sperre=sperren;blockiere;blockieren
starte=starten;aktiviere;aktivieren
stelle=stellen;setze;setzen
stoppe=stoppen;beende;beenden
suche=suchen;fahnde;fahnden
tippe=tippen;schreibe;schreiben
vergiss=vergessen;lösche;löschen
verschiebe=verschieben;verlege;verlegen
versetze=versetzen;schicke;schicken
wechsle=wechseln;springe;springen
wähle=wählen;selektiere;selektieren
öffne=öffnen;zeige;zeigen'''


def table(text):
    return {a: [v.strip() for v in b.split(';')] for a,b in (line.split('=',1) for line in text.splitlines() if line)}


def german(phrase):
    first, rest = phrase.split(' ',1)
    special = {
        'zeige': ('zeigen','blende','ein','einblenden'),
        'führe': ('ausführen','starte','','starten'),
        'füge': ('einfügen','setze','ein','einsetzen'),
        'hefte': ('anheften','fixiere','','fixieren'),
        'hänge': ('anhängen','füge','hinzu','hinzufügen'),
        'brich': ('abbrechen','stoppe','','stoppen'),
        'fahre': ('herunterfahren','schalte','aus','ausschalten'),
        'merke': ('merken','speichere','','speichern'),
        'mache': ('rückgängig machen','widerrufe','','widerrufen'),
        'mach': ('leiser machen','dämpfe','','dämpfen'),
    }
    if first in special:
        for suffix in (' aus',' ein',' an',' ab',' herunter',' rückgängig',' leiser'):
            if rest.endswith(suffix):
                rest = rest[:-len(suffix)]; break
        if rest.startswith('dir '):rest=rest[4:]
        inf,syn,particle,syninf=special[first]
        return [rest+' '+inf, syn+' '+rest+(' '+particle if particle else ''), rest+' '+syninf]
    if first=='schalte':
        middle, particle=rest.rsplit(' ',1)
        if particle=='stumm':return [middle+' stummschalten','stelle '+middle+' stumm',middle+' stummstellen']
        if particle=='ein':return [middle+' einschalten','aktiviere '+middle,middle+' aktivieren']
        if particle=='aus':return [middle+' ausschalten','deaktiviere '+middle,middle+' deaktivieren']
        if particle=='um':return [middle+' umschalten','wechsle den Zustand von '+middle,middle+' umstellen']
    if first=='stelle' and rest.endswith(' wieder her'):
        body=rest[:-11]
        return [body+' wiederherstellen','restauriere '+body,body+' restaurieren']
    inf,syn,syninf=table(DE)[first]
    if first=='benenne' and rest.endswith(' um'):rest=rest[:-3]
    return [rest+' '+inf,syn+' '+rest,rest+' '+syninf]


SPECIAL = {
 'application.kill': [
  ['beende die Anwendung {name} sofort','erzwinge das Beenden von {name}','Anwendung {name} zwangsweise beenden'],
 ],
}


def build():
    root=Path(__file__).resolve().parent
    source=json.loads((root/'config/canonical_phrases/core.json').read_text())
    output={}; errors=[]
    for intent,phrases in source.items():
        output[intent]={}
        for code,phrase in zip(LANGS,phrases):
            first,rest=phrase.split(' ',1)
            if first in {'где','where','wo','où','dónde','dove','onde'}:
                variants={
                 'ru':['найди сохранения игры {name}','покажи папку сохранений {name}','где лежат игровые сохранения {name}'],
                 'en':['find the saves for {name}','locate the save folder for {name}','show the saved games for {name}'],
                 'de':['finde die Spielstände von {name}','zeige den Speicherstandordner von {name}','wo liegen die Spielstände von {name}'],
                 'fr':['trouve les sauvegardes de {name}','montre le dossier des sauvegardes de {name}','où se trouvent les sauvegardes de {name}'],
                 'es':['localiza las partidas guardadas de {name}','muestra la carpeta de partidas de {name}','dónde se guardan las partidas de {name}'],
                 'it':['trova i salvataggi di {name}','mostra la cartella dei salvataggi di {name}','dove si trovano i salvataggi di {name}'],
                 'pt':['encontra os jogos guardados de {name}','mostra a pasta das gravações de {name}','onde ficam as gravações de {name}'],
                }[code]
            elif code=='de':
                try:variants=german(phrase)
                except KeyError:errors.append((intent,code,phrase));continue
            elif code=='en':
                if first not in table(EN):errors.append((intent,code,phrase));continue
                variants=[prefix+' '+rest for prefix in table(EN)[first]]
            else:
                mapping=table(MORPH[code])
                if first not in mapping:errors.append((intent,code,phrase));continue
                variants=[prefix+' '+rest for prefix in mapping[first]]
            output[intent][code]=variants
    # Action-specific repairs are applied separately, making every lexical
    # exception reviewable rather than silently accepting malformed generated text.
    for repair_name in ('core_variant_repairs.json', 'core_locale_repairs.json', 'core_ru_en_repairs.json'):
        repairs=root/'config'/repair_name
        if repairs.exists():
            for intent,langs in json.loads(repairs.read_text()).items():
                if intent in output: output[intent].update(langs)
    errors=[row for row in errors if row[1] not in output[row[0]]]
    if errors:raise ValueError(errors)
    for intent,langs in output.items():
        for i,code in enumerate(LANGS):
            base=source[intent][i];slots=set(re.findall(r'\{(\w+)\}',base));values=langs[code]
            assert len(values)==3 and len(set([base.casefold(),*(p.casefold() for p in values)]))==4,(intent,code,values)
            assert all(set(re.findall(r'\{(\w+)\}',p))==slots for p in values),(intent,code)
    target=root/'config/command_variants/core.json';target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(len(output),'actions,',sum(len(rows) for v in output.values() for rows in v.values()),'additional forms')


if __name__=='__main__':build()
