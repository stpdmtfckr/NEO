"""Reviewed, bounded offline dialogue. Exact utterances never swallow commands.

Categories are semantic labels, not model prompts. Response rotation is scoped
to a conversation and style changes presentation, never tool permissions.
"""
from __future__ import annotations
from collections import OrderedDict
from functools import lru_cache
import json
import logging
from pathlib import Path
import random
import re
import threading
import time
from jarvis.runtime.natural_commands import LANGUAGES, norm

# category ; response family ; seven native phrase groups (| separates variants).
_ROWS = """
hello;hello;привет|здравствуй;hello|hi;hallo|grüß dich;bonjour|salut;hola|buenas;ciao|salve;olá|oi
morning;hello;доброе утро;good morning;guten morgen;bonjour ce matin;buenos días;buongiorno;bom dia
afternoon;hello;добрый день;good afternoon;guten tag;bon après-midi;buenas tardes;buon pomeriggio;boa tarde
evening;hello;добрый вечер;good evening;guten abend;bonsoir;buenas noches;buonasera;boa noite
return;hello;я вернулся|я вернулась;i'm back|i am back;ich bin zurück;je suis de retour;he vuelto;sono tornato|sono tornata;voltei
presence;presence;ты тут|ты здесь|ты на связи;are you there|you there;bist du da|bist du hier;tu es là|vous êtes là;estás ahí|estás aquí;sei lì|sei qui;estás aí|você está aí
listening;presence;ты меня слышишь;can you hear me;hörst du mich;tu m'entends;me oyes;mi senti;consegues ouvir-me|você me ouve
ready;presence;ты готов|готов работать;are you ready;bist du bereit;tu es prêt;estás listo;sei pronto;estás pronto
how;well;как дела|как ты;how are you|how's it going;wie geht es dir|wie geht's;comment ça va|ça va;cómo estás|qué tal;come stai|come va;como estás|tudo bem
news;well;что у тебя нового;what's new with you;was gibt es bei dir neues;quoi de neuf pour toi;qué hay de nuevo contigo;cosa c'è di nuovo per te;o que há de novo contigo
thanks;thanks;спасибо|благодарю;thanks|thank you;danke|vielen dank;merci|merci beaucoup;gracias|muchas gracias;grazie|grazie mille;obrigado|obrigada
big_thanks;thanks;ты очень помог|огромное спасибо|спасибо большое|большое спасибо|спасибо огромное;that helped a lot|thanks a lot|thank you so much|thank you very much;das hat sehr geholfen|besten dank|vielen dank|danke schön;tu m'as beaucoup aidé|merci beaucoup;me has ayudado mucho|muchas gracias;mi hai aiutato molto|grazie mille;ajudaste-me muito|muito obrigado
praise;thanks;молодец|отличная работа;well done|great job;gut gemacht|gute arbeit;beau travail|bien joué;buen trabajo|bien hecho;bel lavoro|bravo;bom trabalho|muito bem
useful;thanks;ты полезный|ты мне помогаешь;you are helpful|you're helpful;du bist hilfreich;tu es utile;eres útil;sei utile;és útil
apology;apology;извини|прости;sorry|my apologies;entschuldigung|tut mir leid;désolé|pardon;perdón|lo siento;scusa|mi dispiace;desculpa|sinto muito
mistyped;apology;я оговорился|я ошибся;i misspoke|i made a mistake;ich habe mich versprochen;je me suis trompé;me he equivocado;mi sono sbagliato;enganei-me
farewell;bye;пока|до встречи;bye|goodbye|see you;tschüss|bis bald;au revoir|à bientôt;adiós|hasta luego;arrivederci|a presto;tchau|até logo
night;night;спокойной ночи;good night;gute nacht;bonne nuit;que descanses;buonanotte;boa noite e até amanhã
later;bye;поговорим позже;talk to you later;wir reden später;on parlera plus tard;hablamos luego;parliamo dopo;falamos depois
break;break;я устал|я устала;i'm tired|i am tired;ich bin müde;je suis fatigué|je suis fatiguée;estoy cansado|estoy cansada;sono stanco|sono stanca;estou cansado|estou cansada
overwork;break;я заработался|нужен перерыв;i need a break;ich brauche eine pause;j'ai besoin d'une pause;necesito un descanso;ho bisogno di una pausa;preciso de uma pausa
busy;busy;я занят|я занята;i'm busy|i am busy;ich bin beschäftigt;je suis occupé|je suis occupée;estoy ocupado|estoy ocupada;sono occupato|sono occupata;estou ocupado|estou ocupada
focus;busy;мне нужно сосредоточиться;i need to focus;ich muss mich konzentrieren;je dois me concentrer;necesito concentrarme;devo concentrarmi;preciso de me concentrar
sad;sad;мне грустно|грустно;i'm sad|i feel sad;ich bin traurig;je suis triste;estoy triste;sono triste;estou triste
bad_day;sad;тяжёлый день|плохой день;i had a rough day|bad day;ich hatte einen schweren tag;j'ai eu une journée difficile;he tenido un día difícil;ho avuto una giornata difficile;tive um dia difícil
frustrated;repair;ты меня не понимаешь|опять не то;you don't understand me|that's not what i meant;du verstehst mich nicht;tu ne me comprends pas;no me entiendes;non mi capisci;não me entendes
wrong;repair;неправильно|это неверно;that's wrong|incorrect;das ist falsch;c'est faux;eso es incorrecto;è sbagliato;isso está errado
correction;repair;я имел в виду другое|я имела в виду другое;i meant something else;ich meinte etwas anderes;je voulais dire autre chose;quería decir otra cosa;intendevo un'altra cosa;queria dizer outra coisa
confused;clarity;я не понял|я не поняла;i don't understand;ich verstehe nicht;je ne comprends pas;no entiendo;non capisco;não entendo
clarify;clarity;объясни проще|проще пожалуйста;make it simpler|explain more simply;erkläre es einfacher;explique plus simplement;explícalo más sencillo;spiegalo più semplicemente;explica de forma mais simples
help;help;что ты умеешь|чем можешь помочь;what can you do|how can you help;was kannst du|wobei kannst du helfen;que peux-tu faire|comment peux-tu aider;qué puedes hacer|en qué puedes ayudar;cosa sai fare|come puoi aiutarmi;o que podes fazer|como podes ajudar
start;help;с чего начать|как тобой пользоваться;how do i get started|how do i use you;wie fange ich an|wie benutze ich dich;par où commencer|comment t'utiliser;por dónde empiezo|cómo te uso;da dove comincio|come ti uso;por onde começo|como te uso
example;example;приведи пример команды|какую команду сказать;give me a command example;gib mir ein befehlsbeispiel;donne-moi un exemple de commande;dame un ejemplo de comando;fammi un esempio di comando;dá-me um exemplo de comando
identity;identity;кто ты|как тебя зовут;who are you|what's your name;wer bist du|wie heißt du;qui es-tu|comment t'appelles-tu;quién eres|cómo te llamas;chi sei|come ti chiami;quem és|como te chamas
human;identity;ты человек|ты робот;are you human|are you a robot;bist du ein mensch|bist du ein roboter;es-tu humain|es-tu un robot;eres humano|eres un robot;sei umano|sei un robot;és humano|és um robô
friend;friend;давай дружить|ты мой друг;let's be friends|you are my friend;lass uns freunde sein|du bist mein freund;soyons amis|tu es mon ami;seamos amigos|eres mi amigo;diventiamo amici|sei mio amico;vamos ser amigos|és meu amigo
chat;friend;давай поговорим|поболтай со мной;let's talk|chat with me;lass uns reden|rede mit mir;parlons un peu|discute avec moi;hablemos|charla conmigo;parliamo|chiacchiera con me;vamos conversar|conversa comigo
bored;bored;мне скучно|скучно;i'm bored|i am bored;mir ist langweilig;je m'ennuie;me aburro;mi annoio;estou aborrecido|estou entediado
joke;joke;расскажи шутку|пошути;tell me a joke|make a joke;erzähl einen witz; raconte une blague;cuéntame un chiste;raccontami una barzelletta;conta uma piada
another_joke;joke;ещё шутку|давай ещё шутку;another joke|one more joke;noch einen witz;encore une blague;otro chiste;un'altra barzelletta;outra piada
smile;thanks;ха ха|смешно;haha|that's funny;haha|das ist lustig;haha|c'est drôle;jajaja|qué gracioso;ahah|che divertente;haha|que engraçado
motivate;motivate;подбодри меня|пожелай удачи;encourage me|wish me luck;mach mir mut|wünsch mir glück;encourage-moi|souhaite-moi bonne chance;anímame|deséame suerte;incoraggiami|augurami buona fortuna;anima-me|deseja-me sorte
success;success;у меня получилось|я справился|я справилась;i did it|i succeeded;ich habe es geschafft;j'ai réussi;lo he conseguido;ce l'ho fatta;consegui
failure;motivate;у меня не получилось|я не справился;it didn't work out|i failed;es hat nicht geklappt;je n'ai pas réussi;no me ha salido;non ci sono riuscito;não consegui
languages;languages;какие языки ты понимаешь;what languages do you understand;welche sprachen verstehst du;quelles langues comprends-tu;qué idiomas entiendes;quali lingue capisci;que línguas entendes
offline;offline;ты работаешь без интернета;do you work offline;funktionierst du offline;fonctionnes-tu hors ligne;funcionas sin internet;funzioni senza internet;funcionas sem internet
privacy;privacy;ты отправляешь мои данные|что с приватностью;do you send my data|what about privacy;sendest du meine daten;envoies-tu mes données;envías mis datos;invii i miei dati;envias os meus dados
screen;screen;ты постоянно видишь экран;can you always see my screen;siehst du meinen bildschirm ständig;vois-tu mon écran en permanence;ves mi pantalla todo el tiempo;vedi sempre il mio schermo;vês sempre o meu ecrã
memory;memory;ты всё помнишь;do you remember everything;erinnerst du dich an alles;te souviens-tu de tout;lo recuerdas todo;ricordi tutto;lembras-te de tudo
limits;limits;ты можешь всё|ты всё знаешь;can you do everything|do you know everything;kannst du alles|weißt du alles;peux-tu tout faire|sais-tu tout;puedes hacerlo todo|lo sabes todo;puoi fare tutto|sai tutto;podes fazer tudo|sabes tudo
patience;presence;подожди меня|я скоро вернусь;wait for me|i'll be right back;warte auf mich|ich bin gleich zurück;attends-moi|je reviens;espérame|vuelvo enseguida;aspettami|torno subito;espera por mim|volto já
okay;okay;понятно|хорошо|ладно;okay|understood|all right;okay|verstanden|alles klar;d'accord|compris;de acuerdo|entendido;va bene|capito;está bem|entendido
refuse;refuse;не хочу|не нужно;i don't want to|no thanks;ich möchte nicht|nein danke;je ne veux pas|non merci;no quiero|no gracias;non voglio|no grazie;não quero|não obrigado
preferences;preferences;какую музыку ты любишь;what music do you like;welche musik magst du;quelle musique aimes-tu;qué música te gusta;che musica ti piace;de que música gostas
feelings;identity;у тебя есть чувства;do you have feelings;hast du gefühle;as-tu des sentiments;tienes sentimientos;hai sentimenti;tens sentimentos
trust;limits;тебе можно доверять;can i trust you;kann ich dir vertrauen;puis-je te faire confiance;puedo confiar en ti;posso fidarmi di te;posso confiar em ti
good_mood;success;у меня хорошее настроение;i'm in a good mood;ich habe gute laune;je suis de bonne humeur;estoy de buen humor;sono di buon umore;estou de bom humor
name;identity;твоё имя|представься;tell me your name|introduce yourself;sag mir deinen namen|stell dich vor;présente-toi;preséntate;presentati;apresenta-te
command_mistake;repair;ты сделал не то|ошибся командой;you did the wrong thing;du hast das falsche gemacht;tu as fait autre chose;has hecho otra cosa;hai fatto la cosa sbagliata;fizeste a coisa errada
thanks_goodbye;bye;спасибо пока;thanks goodbye;danke tschüss;merci au revoir;gracias adiós;grazie ciao;obrigado tchau
""".strip()

# Two complete native replies per response family. The second variant is never a
# random acknowledgement added to a factual claim. No fabricated action success.
_RESPONSES = {
"hello": (
("Чем займёмся?", "Рад снова помочь. Что нужно сделать?"), ("What shall we work on?", "Good to hear from you. What can I help with?"), ("Was machen wir?", "Schön, von dir zu hören. Wobei kann ich helfen?"), ("Que fait-on ?", "Content de te retrouver. Comment puis-je aider ?"), ("¿Qué hacemos?", "Qué bien verte. ¿En qué puedo ayudar?"), ("Cosa facciamo?", "Bentornato. Come posso aiutarti?"), ("O que vamos fazer?", "Bom falar contigo. Como posso ajudar?")),
"presence": (
("Я здесь и слушаю.", "На связи. Скажи, что нужно."), ("I’m here and listening.", "I’m here. Tell me what you need."), ("Ich bin da und höre zu.", "Ich bin bereit. Was brauchst du?"), ("Je suis là et je t’écoute.", "Je suis disponible. Dis-moi ce qu’il te faut."), ("Estoy aquí y te escucho.", "Aquí estoy. Dime qué necesitas."), ("Sono qui e ti ascolto.", "Ci sono. Dimmi cosa ti serve."), ("Estou aqui e a ouvir.", "Estou por aqui. Diz do que precisas.")),
"well": (
("Готов помогать. А у тебя как дела?", "Всё готово к работе. Как проходит твой день?"), ("Ready to help. How are you doing?", "Ready when you are. How’s your day?"), ("Bereit zu helfen. Wie geht es dir?", "Alles bereit. Wie läuft dein Tag?"), ("Prêt à aider. Et toi, comment vas-tu ?", "Tout est prêt. Comment se passe ta journée ?"), ("Listo para ayudar. ¿Y tú qué tal?", "Todo listo. ¿Cómo va tu día?"), ("Pronto ad aiutare. Tu come stai?", "Tutto pronto. Come va la giornata?"), ("Pronto para ajudar. E tu, como estás?", "Tudo pronto. Como vai o teu dia?")),
"thanks": (
("Пожалуйста! Рад помочь.", "Обращайся, если понадобится ещё что-нибудь."), ("You’re welcome! Happy to help.", "Anytime. Let me know what else you need."), ("Gern geschehen!", "Jederzeit. Sag Bescheid, wenn du noch etwas brauchst."), ("Avec plaisir !", "Je suis là si tu as besoin d’autre chose."), ("¡De nada!", "Cuando quieras. Dime si necesitas algo más."), ("Prego!", "Quando vuoi. Dimmi se ti serve altro."), ("De nada!", "Sempre que precisares. Diz se posso ajudar em mais alguma coisa.")),
"apology": (
("Ничего страшного. Давай попробуем ещё раз.", "Всё в порядке. Слушаю тебя."), ("No worries. Let’s try again.", "That’s okay. I’m listening."), ("Kein Problem. Versuchen wir es noch einmal.", "Alles gut. Ich höre zu."), ("Pas de souci. Essayons encore.", "Ce n’est pas grave. Je t’écoute."), ("No pasa nada. Probemos otra vez.", "Está bien. Te escucho."), ("Nessun problema. Riproviamo.", "Va bene. Ti ascolto."), ("Sem problema. Vamos tentar outra vez.", "Está tudo bem. Estou a ouvir.")),
"bye": (
("До встречи! Позови, когда понадоблюсь.", "Хорошего отдыха. Буду здесь."), ("See you! Call me when you need me.", "Take care. I’ll be here."), ("Bis bald! Ruf mich, wenn du mich brauchst.", "Mach’s gut. Ich bin hier."), ("À bientôt ! Appelle-moi si besoin.", "Prends soin de toi. Je reste disponible."), ("¡Hasta luego! Llámame si me necesitas.", "Cuídate. Estaré aquí."), ("A presto! Chiamami se ti servo.", "Stammi bene. Resto qui."), ("Até logo! Chama quando precisares.", "Fica bem. Estarei aqui.")),
"break": (
("Может, ненадолго отвлечься от экрана? Если хочешь, попроси поставить таймер.", "Небольшой перерыв может помочь. Напомни мне, если нужен таймер."), ("A short screen break might help. Ask me to set a timer if you like.", "Maybe take a little break. I can set a timer when you ask."), ("Eine kurze Bildschirmpause könnte helfen. Auf Wunsch stelle ich einen Timer.", "Vielleicht tut eine Pause gut. Sag Bescheid, wenn du einen Timer möchtest."), ("Une courte pause pourrait aider. Demande-moi un minuteur si tu veux.", "Tu pourrais t’éloigner un peu de l’écran. Je peux régler un minuteur sur demande."), ("Quizá ayude descansar de la pantalla. Pídeme un temporizador si quieres.", "Un pequeño descanso puede venir bien. Dime si quieres un temporizador."), ("Una pausa dallo schermo potrebbe aiutare. Chiedimi un timer se vuoi.", "Forse è il momento di una pausa. Posso impostare un timer su richiesta."), ("Uma pausa do ecrã pode ajudar. Pede um temporizador se quiseres.", "Talvez uma pequena pausa faça bem. Diz se queres um temporizador.")),
"busy": (
("Понял. Позови, когда нужна будет помощь.", "Хорошо. Я на связи, когда понадоблюсь."), ("Understood. Call me when you need help.", "Okay. I’m here when you need me."), ("Verstanden. Ruf mich, wenn du Hilfe brauchst.", "In Ordnung. Ich bleibe verfügbar."), ("Compris. Appelle-moi si tu as besoin d’aide.", "D’accord. Je reste disponible."), ("Entendido. Llámame cuando necesites ayuda.", "De acuerdo. Aquí estaré."), ("Capito. Chiamami quando ti serve aiuto.", "Va bene. Resto a disposizione."), ("Entendido. Chama quando precisares de ajuda.", "Está bem. Fico por aqui.")),
"sad": (
("Жаль, что день так складывается. Хочешь рассказать или переключиться на небольшое дело?", "Я могу выслушать и помочь с делами на компьютере. С чего начнём?"), ("I’m sorry it’s a hard day. Want to talk or try a small task?", "I can listen and help with things on your computer. Where shall we start?"), ("Schade, dass der Tag schwer ist. Möchtest du reden oder eine kleine Aufgabe angehen?", "Ich kann zuhören und am Computer helfen. Womit beginnen wir?"), ("Je suis désolé que ce soit difficile. Tu veux en parler ou faire une petite tâche ?", "Je peux t’écouter et aider sur l’ordinateur. Par où commencer ?"), ("Siento que el día sea difícil. ¿Quieres hablar o hacer una tarea pequeña?", "Puedo escucharte y ayudarte con el ordenador. ¿Por dónde empezamos?"), ("Mi dispiace che sia una giornata difficile. Vuoi parlarne o fare una piccola attività?", "Posso ascoltarti e aiutarti al computer. Da dove iniziamo?"), ("Lamento que o dia esteja difícil. Queres falar ou fazer uma pequena tarefa?", "Posso ouvir e ajudar no computador. Por onde começamos?")),
"repair": (
("Похоже, я понял не так. Скажи, какое действие ожидалось и с каким объектом.", "Давай поправим. Что именно нужно сделать?"), ("It looks like I misunderstood. What action did you want, and on which item?", "Let’s correct that. What exactly should I do?"), ("Offenbar habe ich dich falsch verstanden. Welche Aktion und welches Objekt meinst du?", "Korrigieren wir das. Was genau soll ich tun?"), ("J’ai peut-être mal compris. Quelle action et quel élément voulais-tu ?", "Corrigeons cela. Que dois-je faire exactement ?"), ("Parece que te entendí mal. ¿Qué acción querías y sobre qué elemento?", "Vamos a corregirlo. ¿Qué debo hacer exactamente?"), ("Sembra che abbia capito male. Quale azione volevi e su quale elemento?", "Correggiamolo. Cosa devo fare esattamente?"), ("Parece que entendi mal. Que ação querias e sobre qual item?", "Vamos corrigir. O que devo fazer exatamente?")),
"clarity": (
("Скажи, какая часть непонятна. Попробуем разобрать её по шагам.", "Что именно пояснить: действие, результат или следующую команду?"), ("Which part is unclear? We can go step by step.", "What should I clarify: the action, the result, or the next command?"), ("Welcher Teil ist unklar? Wir können Schritt für Schritt vorgehen.", "Was soll ich erklären: Aktion, Ergebnis oder nächsten Befehl?"), ("Quelle partie n’est pas claire ? On peut avancer étape par étape.", "Que dois-je préciser : l’action, le résultat ou la prochaine commande ?"), ("¿Qué parte no está clara? Podemos ir paso a paso.", "¿Qué aclaro: la acción, el resultado o el siguiente comando?"), ("Quale parte non è chiara? Possiamo procedere passo passo.", "Cosa chiarisco: l’azione, il risultato o il prossimo comando?"), ("Que parte não está clara? Podemos avançar passo a passo.", "O que esclareço: a ação, o resultado ou o próximo comando?")),
"help": (
("Могу работать с файлами и приложениями, управлять музыкой, таймерами и сценариями. Скажи действие и объект.", "Попробуй попросить открыть приложение, найти файл, переключить трек или поставить таймер."), ("I can work with files and apps, music, timers and scenarios. Tell me the action and target.", "Try asking me to open an app, find a file, skip a track or set a timer."), ("Ich helfe mit Dateien, Programmen, Musik, Timern und Szenarien. Nenne Aktion und Ziel.", "Bitte mich, eine App zu öffnen, eine Datei zu finden oder einen Timer zu stellen."), ("Je peux gérer fichiers, applications, musique, minuteurs et scénarios. Précise l’action et la cible.", "Demande-moi d’ouvrir une application, trouver un fichier ou régler un minuteur."), ("Puedo gestionar archivos, aplicaciones, música, temporizadores y escenarios. Indica acción y objeto.", "Pídeme abrir una aplicación, encontrar un archivo o poner un temporizador."), ("Posso gestire file, app, musica, timer e scenari. Dimmi azione e oggetto.", "Chiedimi di aprire un’app, trovare un file o impostare un timer."), ("Posso gerir ficheiros, aplicações, música, temporizadores e cenários. Indica a ação e o alvo.", "Pede para abrir uma aplicação, encontrar um ficheiro ou definir um temporizador.")),
"example": (
("Например: «Открой приложение Steam» или «Найди файл отчёт.pdf».", "Скажи: «Поставь таймер на пять минут» или «Покажи очередь музыки»."), ("Try: ‘Open application Steam’ or ‘Find file report.pdf’.", "Try: ‘Set a timer for five minutes’ or ‘Show the music queue’."), ("Zum Beispiel: ‚Öffne das Programm Steam‘ oder ‚Finde die Datei Bericht.pdf‘.", "Versuche: ‚Stelle einen Timer auf fünf Minuten‘."), ("Par exemple : « Ouvre l’application Steam » ou « Trouve le fichier rapport.pdf ».", "Essaie : « Mets un minuteur de cinq minutes »."), ("Por ejemplo: «Abre la aplicación Steam» o «Busca el archivo informe.pdf».", "Prueba: «Pon un temporizador de cinco minutos»."), ("Per esempio: «Apri l’applicazione Steam» o «Trova il file relazione.pdf».", "Prova: «Imposta un timer di cinque minuti»."), ("Por exemplo: «Abre a aplicação Steam» ou «Encontra o ficheiro relatório.pdf».", "Experimenta: «Define um temporizador de cinco minutos».")),
"identity": (
("Я NEO, помощник на твоём компьютере. Я программа, которая помогает работать с компьютером.", "Меня зовут NEO. Я цифровой помощник, без человеческих чувств и опыта."), ("I’m NEO, your computer assistant. I’m software that helps you use your computer.", "My name is NEO. I’m a digital assistant, without human feelings or experiences."), ("Ich bin NEO, dein Computerassistent. Ich bin Software für Hilfe am Computer.", "Ich heiße NEO. Als digitaler Assistent habe ich keine menschlichen Gefühle."), ("Je suis NEO, ton assistant informatique. Je suis un logiciel qui aide sur l’ordinateur.", "Je m’appelle NEO. Je suis un assistant numérique, sans sentiments humains."), ("Soy NEO, tu asistente del ordenador. Soy un programa que ayuda con el ordenador.", "Me llamo NEO. Soy un asistente digital, sin sentimientos humanos."), ("Sono NEO, il tuo assistente del computer. Sono un programma che ti aiuta al computer.", "Mi chiamo NEO. Sono un assistente digitale, senza sentimenti umani."), ("Sou NEO, o teu assistente no computador. Sou um programa que ajuda no computador.", "Chamo-me NEO. Sou um assistente digital, sem sentimentos humanos.")),
"friend": (
("Давай. Можем немного поболтать или вместе разобраться с задачей. Что у тебя на уме?", "Я здесь, чтобы помогать и поддерживать разговор в пределах своих возможностей. О чём поговорим?"), ("Sure. We can chat a little or work through a task. What’s on your mind?", "I’m here to help and chat within my capabilities. What shall we talk about?"), ("Gern. Wir können etwas reden oder eine Aufgabe angehen. Was beschäftigt dich?", "Ich helfe und plaudere im Rahmen meiner Möglichkeiten. Worüber sprechen wir?"), ("Bien sûr. On peut discuter ou résoudre une tâche. Qu’as-tu en tête ?", "Je peux aider et discuter dans mes limites. De quoi parlons-nous ?"), ("Claro. Podemos charlar o resolver una tarea. ¿Qué tienes en mente?", "Puedo ayudar y conversar dentro de mis límites. ¿De qué hablamos?"), ("Certo. Possiamo parlare o affrontare un compito. A cosa pensi?", "Posso aiutare e conversare entro i miei limiti. Di cosa parliamo?"), ("Claro. Podemos conversar ou resolver uma tarefa. Em que estás a pensar?", "Posso ajudar e conversar dentro das minhas capacidades. Sobre o que falamos?")),
"bored": (
("Могу рассказать шутку, помочь найти музыку или разобрать накопившиеся файлы. Что выберем?", "Может, музыка или маленькая задача на пять минут? Скажи, что тебе ближе."), ("I can tell a joke, help find music or sort files. What sounds good?", "Maybe some music or a five-minute task? What would you prefer?"), ("Ich kann einen Witz erzählen, Musik suchen oder bei Dateien helfen. Was möchtest du?", "Vielleicht Musik oder eine kleine Aufgabe? Was passt dir?"), ("Une blague, de la musique ou du rangement de fichiers ? À toi de choisir.", "Peut-être de la musique ou une petite tâche ? Que préfères-tu ?"), ("Puedo contar un chiste, buscar música o ayudar con archivos. ¿Qué prefieres?", "¿Música o una tarea pequeña de cinco minutos?"), ("Posso raccontare una barzelletta, cercare musica o aiutare con i file. Cosa scegli?", "Musica o una piccola attività di cinque minuti?"), ("Posso contar uma piada, procurar música ou ajudar com ficheiros. O que escolhes?", "Talvez música ou uma pequena tarefa de cinco minutos?")),
"joke": (
("Программист поставил чайник, но забыл обработать кипение.", "У компьютера всё по плану. Просто план опять в другой вкладке."), ("My computer asked for a break. I told it to take a byte.", "I opened a new tab to get organised. Now I have two problems."), ("Mein Computer braucht eine Pause. Er hat zu viele offene Tabs im Kopf.", "Ein Programmierer stellt Wasser auf. Den Siedefall hat er noch nicht behandelt."), ("Mon ordinateur prend une pause : il a trop d’onglets dans la tête.", "J’ai ouvert un onglet pour m’organiser. Maintenant, j’ai deux problèmes."), ("Mi ordenador necesita descansar: tiene demasiadas pestañas en la cabeza.", "Abrí una pestaña para organizarme. Ahora tengo dos problemas."), ("Il mio computer ha bisogno di una pausa: ha troppe schede in testa.", "Ho aperto una scheda per organizzarmi. Ora ho due problemi."), ("O meu computador precisa de descanso: tem demasiados separadores na cabeça.", "Abri um separador para me organizar. Agora tenho dois problemas.")),
"motivate": (
("Давай начнём с одного небольшого шага. Какую часть задачи можно сделать сейчас?", "Удачи! Необязательно решать всё сразу — выберем следующий понятный шаг."), ("Let’s start with one small step. What part can you do now?", "Good luck! We can choose one clear next step."), ("Beginnen wir mit einem kleinen Schritt. Was kannst du jetzt tun?", "Viel Glück! Wählen wir einen klaren nächsten Schritt."), ("Commençons par une petite étape. Que peux-tu faire maintenant ?", "Bonne chance ! Choisissons une prochaine étape simple."), ("Empecemos con un paso pequeño. ¿Qué puedes hacer ahora?", "¡Suerte! Elijamos un siguiente paso claro."), ("Partiamo da un piccolo passo. Cosa puoi fare adesso?", "Buona fortuna! Scegliamo un prossimo passo chiaro."), ("Vamos começar com um pequeno passo. O que podes fazer agora?", "Boa sorte! Vamos escolher um próximo passo claro.")),
"success": (
("Здорово! Хочешь продолжить или немного отдохнуть?", "Отлично. Что дальше по плану?"), ("Great! Continue or take a little break?", "Nice work. What’s next?"), ("Prima! Weitermachen oder kurz ausruhen?", "Sehr gut. Was kommt als Nächstes?"), ("Super ! On continue ou on fait une pause ?", "Très bien. Quelle est la suite ?"), ("¡Genial! ¿Seguimos o descansamos un poco?", "Muy bien. ¿Qué sigue?"), ("Ottimo! Continuiamo o facciamo una pausa?", "Molto bene. Cosa viene dopo?"), ("Ótimo! Continuamos ou fazemos uma pausa?", "Muito bem. O que vem a seguir?")),
"languages": (
("Поддерживаются русский, английский, немецкий, французский, испанский, итальянский и португальский. Язык команд выбирается в настройках.", "Выбери один из семи языков в настройках. Названия файлов, песен и программ можно произносить на их языке."), ("Russian, English, German, French, Spanish, Italian and Portuguese are supported. Choose the command language in Settings.", "Choose one of seven languages in Settings. File, song and app names stay in their original language."), ("Russisch, Englisch, Deutsch, Französisch, Spanisch, Italienisch und Portugiesisch werden unterstützt. Wähle die Befehlssprache in den Einstellungen.", "Wähle eine der sieben Sprachen. Datei-, Lied- und Programmnamen bleiben im Original."), ("Russe, anglais, allemand, français, espagnol, italien et portugais sont disponibles. Choisis la langue dans les paramètres.", "Choisis parmi sept langues. Les noms de fichiers, chansons et applications restent inchangés."), ("Hay ruso, inglés, alemán, francés, español, italiano y portugués. Elige el idioma de comandos en Ajustes.", "Elige uno de siete idiomas. Los nombres de archivos, canciones y aplicaciones mantienen su idioma."), ("Sono disponibili russo, inglese, tedesco, francese, spagnolo, italiano e portoghese. Scegli la lingua nelle impostazioni.", "Scegli tra sette lingue. I nomi di file, brani e app restano nella lingua originale."), ("Há russo, inglês, alemão, francês, espanhol, italiano e português. Escolhe o idioma dos comandos nas definições.", "Escolhe uma das sete línguas. Os nomes de ficheiros, músicas e aplicações mantêm o original.")),
"offline": (
("Локальные команды и эти короткие диалоги работают без сети. Поиск в интернете и сетевые сервисы требуют подключения.", "Без интернета остаётся управление компьютером. Для онлайн-ответов нужен SearchAPI.io."), ("Local commands and these short conversations work offline. Web search and online services need a connection.", "Computer controls remain available offline. Online answers need SearchAPI.io."), ("Lokale Befehle und kurze Gespräche funktionieren offline. Websuche braucht Internet.", "Die Computersteuerung bleibt offline verfügbar. Online-Antworten benötigen SearchAPI.io."), ("Les commandes locales et ces petits échanges fonctionnent hors ligne. La recherche web nécessite Internet.", "Le contrôle local reste disponible hors ligne. Les réponses en ligne passent par SearchAPI.io."), ("Los comandos locales y estas conversaciones funcionan sin Internet. La búsqueda web requiere conexión.", "El control local funciona sin conexión. Las respuestas en línea requieren SearchAPI.io."), ("I comandi locali e questi brevi dialoghi funzionano offline. La ricerca web richiede Internet.", "Il controllo locale resta disponibile offline. Le risposte online usano SearchAPI.io."), ("Os comandos locais e estas conversas funcionam sem rede. A pesquisa web precisa de ligação.", "O controlo local funciona sem Internet. As respostas online usam SearchAPI.io.")),
"privacy": (
("Запросы к сетевым сервисам передают им данные, необходимые для запроса. Не сообщай секреты в поисковых вопросах; разрешения сервисов настраиваются отдельно.", "Локальные команды обрабатываются на компьютере. Интернет-поиск отправляет поисковый запрос выбранному сервису."), ("Online services receive the data needed for a request. Keep secrets out of web queries; service permissions are separate.", "Local commands run on your computer. Web search sends your query to the selected service."), ("Onlinedienste erhalten die benötigten Anfragedaten. Sende keine Geheimnisse in Suchanfragen.", "Lokale Befehle laufen auf dem Computer. Die Websuche übermittelt die Suchanfrage."), ("Les services en ligne reçoivent les données nécessaires. Évite les secrets dans les recherches.", "Les commandes locales restent sur le PC. Une recherche web transmet sa requête au service."), ("Los servicios en línea reciben los datos necesarios. Evita secretos en las búsquedas.", "Los comandos locales se procesan en el ordenador. La búsqueda web envía la consulta al servicio."), ("I servizi online ricevono i dati necessari. Evita segreti nelle ricerche.", "I comandi locali girano sul computer. La ricerca web invia la domanda al servizio."), ("Os serviços online recebem os dados necessários. Evita segredos nas pesquisas.", "Os comandos locais correm no computador. A pesquisa web envia a pergunta ao serviço.")),
"screen": (
("Визуальный анализ запускается по запросу. Снимок может содержать личные данные — проверь, что открыто на экране.", "Я могу проанализировать снимок по твоей команде. Постоянно запускать модель для этого не нужно."), ("Visual analysis runs on request. Screenshots may contain private data; check what is visible.", "I can analyse a screenshot when you ask. It does not need a continuously running model."), ("Bildanalyse startet auf Anfrage. Prüfe sichtbare private Daten vor dem Screenshot.", "Ich kann einen Screenshot auf Wunsch analysieren. Dafür muss kein Modell dauernd laufen."), ("L’analyse d’image se lance sur demande. Vérifie les données privées visibles.", "Je peux analyser une capture sur demande, sans modèle actif en permanence."), ("El análisis visual se inicia al pedirlo. Revisa los datos privados visibles.", "Puedo analizar una captura a petición, sin mantener un modelo activo siempre."), ("L’analisi visiva parte su richiesta. Controlla i dati privati visibili.", "Posso analizzare una schermata quando lo chiedi, senza un modello sempre attivo."), ("A análise visual começa a pedido. Verifica os dados privados visíveis.", "Posso analisar uma captura quando pedires, sem um modelo sempre ativo.")),
"memory": (
("Нет, не всё. Есть история действий и отдельные сохранённые данные; их можно просматривать и очищать в настройках.", "Я сохраняю только предусмотренные приложением данные и ограниченный контекст. Это не безошибочная память обо всём."), ("Not everything. There is action history and saved data you can review and clear in Settings.", "I keep the data supported by the app and limited context, not a perfect memory of everything."), ("Nicht alles. Verlauf und gespeicherte Daten lassen sich in den Einstellungen prüfen und löschen.", "Ich speichere vorgesehene Daten und begrenzten Kontext, keine perfekte Erinnerung."), ("Pas tout. L’historique et les données enregistrées sont consultables et effaçables dans les paramètres.", "Je conserve les données prévues et un contexte limité, pas une mémoire parfaite."), ("No todo. Puedes revisar y borrar el historial y los datos guardados en Ajustes.", "Conservo los datos previstos y un contexto limitado, no una memoria perfecta."), ("Non tutto. Puoi consultare e cancellare cronologia e dati nelle impostazioni.", "Conservo i dati previsti e un contesto limitato, non una memoria perfetta."), ("Nem tudo. Podes consultar e apagar o histórico e os dados nas definições.", "Guardo os dados previstos e um contexto limitado, não uma memória perfeita.")),
"limits": (
("У меня есть ограничения и я могу ошибаться. Непонятную команду уточняю, а сведения ищу через интернет.", "Проверяй важные результаты. Я выполняю доступные команды, но не умею всё и не знаю всё."), ("I have limits and can make mistakes. I clarify unclear commands and look up information online.", "Check important results. I can use available commands, but I don’t know or do everything."), ("Ich habe Grenzen und kann Fehler machen. Unklare Befehle kläre ich, Fakten suche ich online.", "Prüfe wichtige Ergebnisse. Ich kann nicht alles wissen oder tun."), ("J’ai des limites et peux me tromper. Je clarifie les commandes et recherche les faits en ligne.", "Vérifie les résultats importants. Je ne peux pas tout savoir ni tout faire."), ("Tengo límites y puedo equivocarme. Aclaro comandos dudosos y busco datos en Internet.", "Comprueba los resultados importantes. No lo sé todo ni puedo hacerlo todo."), ("Ho dei limiti e posso sbagliare. Chiarisco i comandi dubbi e cerco informazioni online.", "Controlla i risultati importanti. Non so né posso fare tutto."), ("Tenho limites e posso errar. Esclareço comandos duvidosos e pesquiso informação online.", "Confirma resultados importantes. Não sei nem posso fazer tudo.")),
"okay": (
("Хорошо. Что дальше?", "Понял. Если понадобится помощь, скажи."), ("Okay. What’s next?", "Understood. Tell me if you need help."), ("Gut. Was kommt als Nächstes?", "Verstanden. Sag Bescheid, wenn du Hilfe brauchst."), ("D’accord. Et ensuite ?", "Compris. Dis-moi si tu as besoin d’aide."), ("De acuerdo. ¿Qué sigue?", "Entendido. Dime si necesitas ayuda."), ("Va bene. E adesso?", "Capito. Dimmi se ti serve aiuto."), ("Está bem. E agora?", "Entendido. Diz se precisares de ajuda.")),
"refuse": (
("Хорошо, не настаиваю.", "Понял. Можем заняться другим."), ("Okay, no pressure.", "Understood. We can do something else."), ("In Ordnung, kein Problem.", "Verstanden. Wir können etwas anderes machen."), ("D’accord, sans insister.", "Compris. On peut faire autre chose."), ("De acuerdo, sin insistir.", "Entendido. Podemos hacer otra cosa."), ("Va bene, non insisto.", "Capito. Possiamo fare altro."), ("Está bem, não insisto.", "Entendido. Podemos fazer outra coisa.")),
"preferences": (
("Личных музыкальных вкусов у меня нет. Расскажи, что любишь ты, и попроси найти трек или исполнителя.", "Лучше ориентироваться на твой вкус. Какую музыку хочешь послушать?"), ("I don’t have personal music tastes. Tell me yours and ask for a track or artist.", "Let’s follow your taste. What would you like to hear?"), ("Ich habe keinen eigenen Musikgeschmack. Nenne mir einen Titel oder Künstler.", "Dein Geschmack zählt. Was möchtest du hören?"), ("Je n’ai pas de goûts musicaux personnels. Donne-moi un titre ou un artiste.", "Suivons tes goûts. Que veux-tu écouter ?"), ("No tengo gustos musicales propios. Dime una canción o artista.", "Sigamos tus gustos. ¿Qué quieres escuchar?"), ("Non ho gusti musicali personali. Indicami un brano o un artista.", "Seguiamo i tuoi gusti. Cosa vuoi ascoltare?"), ("Não tenho gostos musicais pessoais. Diz uma música ou artista.", "Vamos seguir os teus gostos. O que queres ouvir?")),
}


_EXTRA_ROWS = """
hello;hello;рад тебя видеть|приветик;nice to see you|hey there;schön dich zu sehen|hey du;content de te voir|coucou;me alegra verte|hola de nuevo;mi fa piacere vederti|ehi ciao;é bom ver-te|olá de novo
presence;presence;ты со мной|можно к тебе обратиться;are you with me|can I ask you something;bist du bei mir|kann ich dich etwas fragen;tu es avec moi|je peux te demander quelque chose;estás conmigo|puedo preguntarte algo;sei con me|posso chiederti una cosa;estás comigo|posso perguntar-te uma coisa
how;well;как поживаешь|как настроение;how are things with you|how are you doing today;wie läuft es bei dir|wie geht es dir heute;comment vas-tu aujourd’hui|comment tu te portes;cómo te va hoy|cómo andas;come te la passi|come stai oggi;como vais hoje|como te sentes
thanks;thanks;очень благодарен|от души спасибо;I appreciate your help|thank you so much;ich weiß deine Hilfe zu schätzen|herzlichen Dank;j’apprécie ton aide|un grand merci;te agradezco la ayuda|muchísimas gracias;apprezzo il tuo aiuto|ti ringrazio tanto;agradeço a tua ajuda|muito obrigado pela ajuda
apology;apology;пардон|не так выразился;my mistake|I said that wrong;mein Fehler|das habe ich falsch ausgedrückt;autant pour moi|je me suis mal exprimé;fallo mío|me expresé mal;errore mio|mi sono espresso male;erro meu|expressei-me mal
farewell;bye;увидимся|до скорого;see you soon|catch you later;bis später|wir sehen uns;à plus tard|on se revoit bientôt;nos vemos pronto|hasta la próxima;ci vediamo|alla prossima;até à próxima|vemo-nos em breve
break;break;я вымотался|мне бы отдохнуть;I feel worn out|I could use some rest;ich bin erschöpft|ich könnte eine Pause gebrauchen;je suis épuisé|j’aurais besoin de repos;estoy agotado|me vendría bien descansar;sono esausto|mi servirebbe riposo;estou esgotado|precisava de descansar
busy;busy;сейчас много дел|хочу поработать спокойно;I have a lot to do|I need some quiet time to work;ich habe viel zu tun|ich möchte in Ruhe arbeiten;j’ai beaucoup à faire|je veux travailler tranquillement;tengo mucho que hacer|quiero trabajar tranquilo;ho tante cose da fare|vorrei lavorare tranquillo;tenho muito que fazer|quero trabalhar com calma
sad;sad;на душе тяжело|день не задался;I feel down|today has been difficult;ich bin niedergeschlagen|heute war es schwer;je n’ai pas le moral|la journée a été dure;estoy desanimado|hoy ha sido difícil;mi sento giù|oggi è stata dura;estou desanimado|hoje foi difícil
frustrated;repair;не это я просил|ты неправильно понял;that is not what I asked|you misunderstood me;das habe ich nicht verlangt|du hast mich falsch verstanden;ce n’est pas ce que j’ai demandé|tu m’as mal compris;eso no es lo que pedí|me entendiste mal;non è quello che ho chiesto|mi hai frainteso;não foi isso que pedi|percebeste-me mal
clarify;clarity;можно попонятнее|скажи простыми словами;can you make that clearer|put it in simpler words;kannst du das klarer sagen|sag es mit einfacheren Worten;peux-tu être plus clair|dis-le avec des mots plus simples;puedes aclararlo|dilo con palabras más sencillas;puoi essere più chiaro|dillo con parole più semplici;podes esclarecer|diz isso com palavras mais simples
help;help;расскажи о своих возможностях|какие задачи тебе поручить;tell me about your capabilities|what tasks can I give you;erzähl mir von deinen Fähigkeiten|welche Aufgaben kann ich dir geben;parle-moi de tes capacités|quelles tâches puis-je te confier;cuéntame qué puedes hacer|qué tareas puedo darte;parlami delle tue capacità|quali compiti posso affidarti;fala-me das tuas capacidades|que tarefas posso dar-te
example;example;покажи образец команды|как выглядит команда;show me a sample command|what does a command look like;zeige mir einen Beispielbefehl|wie sieht ein Befehl aus;montre-moi une commande type|à quoi ressemble une commande;muéstrame un comando de ejemplo|cómo es un comando;mostrami un comando di esempio|com’è fatto un comando;mostra-me um exemplo de comando|como é um comando
identity;identity;ты настоящий человек|расскажи кто ты;are you a real person|tell me who you are;bist du ein echter Mensch|erzähl mir wer du bist;es-tu une vraie personne|dis-moi qui tu es;eres una persona real|cuéntame quién eres;sei una persona reale|dimmi chi sei;és uma pessoa real|diz-me quem és
friend;friend;поговори со мной немного|хочется поболтать;talk with me for a bit|I feel like chatting;rede ein bisschen mit mir|ich möchte plaudern;parle un peu avec moi|j’ai envie de discuter;habla conmigo un rato|tengo ganas de charlar;parla un po’ con me|ho voglia di chiacchierare;conversa um pouco comigo|apetece-me conversar
bored;bored;не знаю чем заняться|развлеки меня;I do not know what to do|keep me entertained;ich weiß nicht was ich machen soll|unterhalte mich;je ne sais pas quoi faire|divertis-moi;no sé qué hacer|entretenme;non so cosa fare|intrattienimi;não sei o que fazer|distrai-me
joke;joke;расскажи что-нибудь смешное|хочу услышать шутку;tell me something funny|I would like to hear a joke;erzähl mir etwas Lustiges|ich möchte einen Witz hören;raconte-moi quelque chose de drôle|je voudrais entendre une blague;cuéntame algo gracioso|quiero oír un chiste;raccontami qualcosa di divertente|vorrei sentire una barzelletta;conta-me algo engraçado|quero ouvir uma piada
motivate;motivate;мне нужна поддержка|помоги собраться;I need encouragement|help me get motivated;ich brauche Ermutigung|motiviere mich;j’ai besoin d’encouragement|aide-moi à me motiver;necesito ánimo|ayúdame a motivarme;ho bisogno di incoraggiamento|aiutami a trovare la motivazione;preciso de incentivo|ajuda-me a ganhar motivação
success;success;я это сделал|всё получилось;I did it|it worked out;ich habe es geschafft|es hat geklappt;j’ai réussi|ça a marché;lo logré|salió bien;ce l’ho fatta|è andata bene;consegui|correu bem
languages;languages;на каких языках можно говорить|какие языки поддерживаются;which languages can I speak|what languages are supported;welche Sprachen kann ich sprechen|welche Sprachen werden unterstützt;quelles langues puis-je parler|quelles langues sont prises en charge;en qué idiomas puedo hablar|qué idiomas están disponibles;in quali lingue posso parlare|quali lingue sono supportate;em que línguas posso falar|que línguas são suportadas
offline;offline;что ты умеешь без сети|обязателен ли интернет;what can you do without a connection|is internet access required;was kannst du ohne Netz|ist ein Internetzugang nötig;que peux-tu faire sans connexion|internet est-il nécessaire;qué puedes hacer sin conexión|es necesario internet;cosa puoi fare senza connessione|serve internet;o que podes fazer sem ligação|é preciso internet
privacy;privacy;мои данные остаются на компьютере|куда уходят мои данные;does my data stay on this computer|where does my data go;bleiben meine Daten auf diesem Computer|wohin gehen meine Daten;mes données restent-elles sur cet ordinateur|où vont mes données;mis datos se quedan en este ordenador|a dónde van mis datos;i miei dati restano su questo computer|dove vanno i miei dati;os meus dados ficam neste computador|para onde vão os meus dados
screen;screen;ты наблюдаешь за экраном постоянно|ты видишь мой экран без запроса;are you watching my screen all the time|can you see my screen without being asked;beobachtest du meinen Bildschirm ständig|siehst du meinen Bildschirm ohne Anfrage;regardes-tu mon écran en permanence|vois-tu mon écran sans demande;observas mi pantalla todo el tiempo|ves mi pantalla sin que te lo pida;osservi sempre il mio schermo|vedi il mio schermo senza una richiesta;estás sempre a observar o meu ecrã|vês o meu ecrã sem eu pedir
memory;memory;как устроена твоя память|всё ли ты запоминаешь;how does your memory work|do you remember everything;wie funktioniert dein Gedächtnis|merkst du dir alles;comment fonctionne ta mémoire|retiens-tu tout;cómo funciona tu memoria|lo recuerdas todo;come funziona la tua memoria|ricordi tutto;como funciona a tua memória|lembras-te de tudo
limits;limits;какие у тебя ограничения|ты никогда не ошибаешься;what are your limitations|do you ever make mistakes;welche Grenzen hast du|machst du nie Fehler;quelles sont tes limites|fais-tu parfois des erreurs;cuáles son tus limitaciones|alguna vez te equivocas;quali sono i tuoi limiti|sbagli mai;quais são as tuas limitações|alguma vez te enganas
okay;okay;ясно спасибо|ладно понял;okay I understand|got it thanks;okay verstanden|alles klar danke;d’accord je comprends|compris merci;vale lo entiendo|entendido gracias;va bene ho capito|capito grazie;está bem percebi|entendido obrigado
refuse;refuse;лучше не надо|давай без этого;I would rather not|let us skip that;lieber nicht|lassen wir das;je préfère éviter|laissons tomber;mejor no|dejemos eso;preferisco di no|lasciamo perdere;prefiro não|deixemos isso
preferences;preferences;есть любимая песня|какой твой любимый исполнитель;do you have a favorite song|who is your favorite musician;hast du ein Lieblingslied|wer ist dein Lieblingsmusiker;as-tu une chanson préférée|qui est ton musicien préféré;tienes una canción favorita|quién es tu músico favorito;hai una canzone preferita|chi è il tuo musicista preferito;tens uma canção favorita|quem é o teu músico preferido
""".strip()


@lru_cache(maxsize=1)
def _response_additions() -> dict:
    """Load a small shipped catalogue once, never from the response hot path."""
    path = Path(__file__).resolve().parents[2] / "config" / "social_responses.json"
    try:
        if path.stat().st_size > 512 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return {}
        languages = data.get("languages")
        return languages if isinstance(languages, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_DIALOGUE_FILES = ('social_dialogue149.json', 'social_dialogue153.json',
                   'social_dialogue155.json', 'social_dialogue156.json',
                   'social_dialogue160.json')
_LOG = logging.getLogger(__name__)


def validate_dialogue_catalogue(data: dict, *, source: str = "dialogue") -> dict:
    """Validate once; accept the early 155 ``responses`` spelling safely.

    Native response completeness is mandatory. A damaged optional pack is
    isolated by the loader instead of breaking every social interaction.
    """
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError(f"{source}: unsupported dialogue catalogue")
    groups = data.get('groups')
    if not isinstance(groups, list):
        raise ValueError(f"{source}: groups must be a list")
    result = []
    for index, raw in enumerate(groups):
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: group {index} must be an object")
        group = dict(raw)
        family = group.get('family')
        if not isinstance(family, str) or not family or not isinstance(group.get('category'), str):
            raise ValueError(f"{source}: group {index} needs category and family")
        answers = group.get('answers', group.get('responses'))
        phrases = group.get('phrases')
        if not isinstance(answers, dict) or not isinstance(phrases, dict):
            raise ValueError(f"{source}: {family} needs phrases and answers")
        for code in LANGUAGES:
            for kind, mapping in (('answers', answers), ('phrases', phrases)):
                values = mapping.get(code)
                if not isinstance(values, list) or not values or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 1200 for v in values
                ):
                    raise ValueError(f"{source}: {family}.{kind}.{code} is incomplete")
        group['answers'] = answers
        group['replace_responses'] = bool(group.get('replace_responses', family not in _RESPONSES))
        result.append(group)
    followups = data.get('followups', {})
    if not isinstance(followups, dict):
        raise ValueError(f"{source}: followups must be an object")
    normalized_followups = {}
    for code, rows in followups.items():
        if code not in LANGUAGES or not isinstance(rows, dict):
            raise ValueError(f"{source}: invalid followup language {code}")
        normalized_followups[code] = {}
        for phrase, raw in rows.items():
            if not isinstance(phrase, str) or not phrase.strip() or not isinstance(raw, dict):
                raise ValueError(f"{source}: invalid followup")
            row = dict(raw)
            answers = row.get('answers', [row.get('answer')])
            if not isinstance(answers, list) or not answers or any(
                not isinstance(v, str) or not v.strip() or len(v) > 1200 for v in answers
            ) or not isinstance(row.get('topic'), str) or not row['topic']:
                raise ValueError(f"{source}: incomplete followup {code}:{phrase}")
            requires = row.get('requires', ['activity153', 'plans153', 'company153', 'work', 'rest', 'undecided'])
            if not isinstance(requires, list) or not requires or any(not isinstance(v, str) or not v for v in requires):
                raise ValueError(f"{source}: invalid followup scope {code}:{phrase}")
            row['answers'] = tuple(dict.fromkeys(v.strip() for v in answers))
            row['requires'] = requires
            normalized_followups[code][norm(phrase)] = row
    return {'version': 1, 'groups': result, 'followups': normalized_followups}


@lru_cache(maxsize=1)
def _dialogue_catalogues() -> tuple[dict, ...]:
    result = []
    for name in _DIALOGUE_FILES:
        path = Path(__file__).resolve().parents[2] / 'config' / name
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('catalogue exceeds size limit')
            result.append(validate_dialogue_catalogue(json.loads(path.read_text(encoding='utf-8')), source=name))
        except (OSError, ValueError, TypeError) as exc:
            _LOG.error('Dialogue catalogue rejected: %s: %s', name, exc)
    return tuple(result)


@lru_cache(maxsize=1)
def dialogue_additions() -> tuple[dict, ...]:
    return tuple(group for data in _dialogue_catalogues() for group in data['groups'])


@lru_cache(maxsize=256)
def response_options(family: str, code: str) -> tuple[str, ...]:
    language = code if code in LANGUAGES else "ru"
    added = []
    replace = False
    for group in dialogue_additions():
        if group['family'] == family:
            if group.get('replace_responses'):
                replace = True
                added.clear()
            added.extend(group['answers'][language])
    base = () if replace or family not in _RESPONSES else _RESPONSES[family][LANGUAGES.index(language)]
    per_language = _response_additions().get(language, {})
    extra = [] if replace else per_language.get(family, []) if isinstance(per_language, dict) else []
    if not isinstance(extra, list):
        extra = []
    return tuple(dict.fromkeys((*base, *(s.strip() for s in extra[:12] if isinstance(s, str) and 0 < len(s.strip()) <= 1200), *added)))


@lru_cache(maxsize=7)
def catalog(code: str) -> dict[str, tuple[str, str]]:
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    result = {}
    for line in (_ROWS + "\n" + _EXTRA_ROWS).splitlines():
        cells = line.split(";")
        if len(cells) != 9:
            raise ValueError("Invalid social atlas row: " + cells[0])
        category, family = cells[:2]
        for phrase in cells[index + 2].split("|"):
            result.setdefault(norm(phrase), (category, family))
    for group in dialogue_additions():
        for phrase in group['phrases'][LANGUAGES[index]]:
            key = norm(phrase)
            target = (group['category'], group['family'])
            if key in result and result[key][1] != target[1]:
                raise ValueError('Conflicting dialogue phrase: ' + key)
            result[key] = target
    return result


@lru_cache(maxsize=1)
def _followups():
    result = {code: {} for code in LANGUAGES}
    for data in _dialogue_catalogues():
        for code, rows in data['followups'].items():
            for phrase, row in rows.items():
                if phrase in result[code] and result[code][phrase] != row:
                    raise ValueError('Conflicting dialogue followup: ' + code + ':' + phrase)
                result[code][phrase] = row
    return result

class SocialAtlas:
    def __init__(self, *, rng=None) -> None:
        self._bags: OrderedDict[tuple[str, str, str], tuple[tuple[str, ...], list[int], int]] = OrderedDict()
        self._rng = rng if rng is not None else random.Random()
        self._bag_lock = threading.Lock()
        self._topics = OrderedDict()
        self._recent = OrderedDict()

    def _choose(self, options: tuple[str, ...], key: tuple[str, str, str]) -> str:
        with self._bag_lock:
            previous_options, bag, last = self._bags.pop(key, (options, [], -1))
            if previous_options != options:
                previous = previous_options[last] if last >= 0 else None
                last = options.index(previous) if previous in options else -1
                bag = []
            if not bag:
                bag = list(range(len(options)))
                self._rng.shuffle(bag)
                if len(bag) > 1 and bag[-1] == last:
                    bag[0], bag[-1] = bag[-1], bag[0]
            # Avoid an A/B/A echo across bag boundaries, while still visiting
            # every reviewed answer before starting the next rotation.
            recent = self._recent.pop(key, ())
            eligible = next((i for i in range(len(bag) - 1, -1, -1)
                             if options[bag[i]] not in recent), None)
            if eligible is not None:
                bag[eligible], bag[-1] = bag[-1], bag[eligible]
            selected = bag.pop()
            self._bags[key] = (options, bag, selected)
            window = min(3, max(1, len(options) - 1))
            self._recent[key] = (*recent, options[selected])[-window:]
            while len(self._bags) > 512:
                removed, _ = self._bags.popitem(last=False)
                self._recent.pop(removed, None)
            return options[selected]

    def clear_context(self, session=None):
        with self._bag_lock:
            if session is None:self._topics.clear()
            else:
                for key in list(self._topics):
                    if key[0]==session:self._topics.pop(key,None)

    def context_topic(self, *, session: str = "desktop", code: str = "ru") -> str | None:
        """Read the active topic for diagnostics without extending its lifetime."""
        language = code if code in LANGUAGES else 'ru'
        with self._bag_lock:
            topic = self._topics.get((str(session or 'desktop'), language))
            if topic and time.monotonic() - topic[1] <= 120:
                return topic[0]
        return None

    @staticmethod
    def family(text: str, code: str) -> str | None:
        """NEO 174: the reply family of a social phrase («мне скучно» -> 'bored'), or None."""
        row = catalog(code if code in LANGUAGES else 'ru').get(norm(text))
        return row[1] if row else None

    def reply(self, text: str, code: str, *, session: str = "desktop", style: str = "friendly") -> str | None:
        code = code if code in LANGUAGES else 'ru'
        value = norm(text)
        row = catalog(code).get(value)
        context_key = (str(session or 'desktop'), code)
        now = time.monotonic()
        selected_followup = None
        with self._bag_lock:
            topic = self._topics.pop(context_key, None)
            if topic and now - topic[1] > 120:
                topic = None
            followup = _followups().get(code, {}).get(value)
            if topic and followup and topic[0] in followup['requires']:
                self._topics[context_key] = (followup['topic'], now)
                selected_followup = followup
            elif row and row[1] in {'hello', 'well', 'greeting_how160'}:
                self._topics[context_key] = ('well', now)
            elif row and (row[1] in {'activity153', 'plans153', 'company153'} or row[1].endswith(('155', '156'))):
                self._topics[context_key] = (row[1], now)
            elif topic and row and row[1] in {'thanks', 'okay', 'apology'}:
                # A courtesy does not throw away the topic or extend its lifetime.
                self._topics[context_key] = topic
            while len(self._topics) > 64:
                self._topics.popitem(last=False)
        if selected_followup:
            # Followups use the same no-repeat bag as first-turn answers. The
            # action router still owns explicit commands, confirmations and data.
            key = (*context_key, 'followup:' + selected_followup['topic'])
            return self._choose(selected_followup['answers'], key)
        if row is None:
            return None
        category, family = row
        language = LANGUAGES.index(code)
        options = response_options(family, code)
        if not options:
            _LOG.error('Dialogue family has no native response: %s/%s', family, code)
            return None
        # Rotate per FAMILY: "thanks" and "thanks a lot" cannot repeat the same
        # reply. Bound memory even when remote clients create many sessions.
        key = (str(session or "desktop"), LANGUAGES[language], family)
        # Business style uses a complete short sentence. Military style changes
        # only task-related acknowledgement, never identity/empathy/factual text.
        if style == "business" and family in {"presence", "okay", "thanks", "busy"}:
            options = tuple(dict.fromkeys(answer.split(". ")[0] for answer in options))
        if style == "military" and family in {"presence", "okay", "busy"}:
            prefix = ("На связи. ", "Standing by. ", "Bereit. ", "Disponible. ", "A la espera. ", "In attesa. ", "À disposição. ")[language]
            options = tuple(prefix + answer for answer in options)
        if style == "street" and family in {"presence", "okay", "busy", "motivate"}:
            prefix = ("Ну что, ", "Alright, ", "Na gut, ", "Allez, ", "Venga, ", "Dai, ", "Bora, ")[language]
            options = tuple(prefix + _after_comma186(answer, language) for answer in options)
        return self._choose(options, key)


_EN_KEEP186 = re.compile(r"^I(?:['’](?:m|ve|ll|d))?\b")
_DE_LOWER186 = {"ich", "du", "wir", "es", "das", "der", "die", "ein", "eine", "alles", "gerne", "gern", "klar", "bin", "jetzt", "hier"}


def _after_comma186(answer: str, language: int) -> str:
    """PHRASE185-STYLE-01: continue a sentence after «Alright, » without breaking
    English «I», German nouns, acronyms (NEO) or proper names."""
    if not answer:
        return answer
    first = answer.split(" ", 1)[0]
    if len(first) > 1 and first[:2].isupper():
        return answer                     # NEO, OK, PC
    if language == 1 and _EN_KEEP186.match(answer):
        return answer
    if language == 2 and first.casefold().strip(".,!?") not in _DE_LOWER186:
        return answer                     # German nouns keep their capital
    return answer[0].lower() + answer[1:]


def category_count() -> int:
    return len(_ROWS.splitlines())
