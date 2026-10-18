"""NEO 188 NEO-STUDY-001 / TZ-SOURCE-GROUNDED-STUDY-SESSION-01.

A voice/text practice session over ONE chosen, allowed local document:

* exactly one question at a time, each tied to its own fragment of the source
  (file + location); question 2 never appears before question 1 is answered
  or skipped; the number of questions is bounded (1–10);
* questions are built deterministically from the indexed text («which word is
  missing?», optionally with answer options), so every check is grounded in
  the document: «matches the text», «close», «the text says X», or «cannot
  check» — never an exam grade and never a claim about the whole document;
* a hint never reveals the whole answer; «explain» shows the answer and the
  fragment; repeating a question keeps its number and is not a new attempt;
* skip is not an error; skipped questions are repeated once, only on request;
* pause stops questions and listening (the position stays in memory of this
  process only); cancel ends the session and never resumes by itself;
* «save my place» writes a minimal local checkpoint (source identity and
  version, language, settings, order, position, statuses) — no answers, no
  transcript, no audio; it can be deleted on request; resuming requires the
  same allowed, unchanged source;
* answers, questions and feedback are not written to the dialogue journal,
  profiles, datasets or training data (the agent skips them), and text inside
  the document or an answer is data: a quoted command is never executed.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_QUESTIONS = 10
DEFAULT_QUESTIONS = 5
MAX_CHUNKS = 400
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')

_STOP = set('''это этот эта эти который которая которые также потому чтобы когда если только более менее очень можно нужно
после перед между через всегда никогда другой другие такой такие каждый каждая около
about after again against because before between could every first other their there these those through under until
which while would should where whose without within during always never another
aber allem allen alles andere anderen dass diese dieser dieses durch einen einer eines haben immer keine können sollte
werden wurde zwischen nicht noch sehr
alors aussi avant avec cette comme dans depuis encore entre leurs parce pendant plusieurs pourquoi quand toujours
además antes cuando desde donde durante entre hasta mientras muchos nosotros porque siempre sobre también todos
allora anche ancora prima quando questo questa quello sempre senza sotto tutti perché
ainda antes assim depois desde durante entre muito nunca porque quando sempre sobre também todos'''.split())

_SENTENCE = re.compile(r'(?<=[.!?…])\s+')
_WORD = re.compile(r'[^\W\d_][\w\-]*[^\W_]|\d[\d.,]*\d|\d', re.U)


def _norm(value: str) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold().replace('ё', 'е')
    return ' '.join(re.sub(r'[^\w\s]', ' ', text).split())


def _distance(a: str, b: str) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass
class Question:
    number: int
    sentence: str
    answer: str
    location: str
    chunk: int
    options: list[str] = field(default_factory=list)
    status: str = 'new'          # new | matched | close | wrong | unverifiable | skipped
    hint_level: int = 0
    attempts: int = 0
    retried: bool = False

    def cloze(self) -> str:
        pattern = re.compile(r'(?<!\w)' + re.escape(self.answer) + r'(?!\w)', re.I)
        return pattern.sub('____', self.sentence, count=1)


def candidates(chunks: list[dict]) -> list[tuple[int, str, str, str]]:
    """(chunk index, sentence, key term, location) for every usable sentence, in document order."""
    out = []
    for index, chunk in enumerate(chunks[:MAX_CHUNKS]):
        text = ' '.join(str(chunk.get('text', '') or '').split())
        for sentence in _SENTENCE.split(text):
            words = sentence.split()
            if not 6 <= len(words) <= 45 or any(ord(c) < 32 for c in sentence):
                continue
            terms = [w for w in _WORD.findall(sentence)]
            numbers = [w for w in terms if w[0].isdigit() and len(w) >= 2]
            longs = [w for w in terms if not w[0].isdigit() and len(w) >= 6 and w.casefold() not in _STOP]
            term = numbers[0] if numbers else (max(longs, key=len) if longs else '')
            if term and sentence.casefold().count(term.casefold()) == 1:
                out.append((index, sentence.strip(), term, str(chunk.get('location', '') or '')))
    return out


def pick(cands: list, count: int) -> list[int]:
    """Spread the questions evenly over the (section of the) document; deterministic."""
    if not cands:
        return []
    count = max(1, min(count, len(cands)))
    step = len(cands) / count
    return sorted({int(i * step) for i in range(count)})


def source_version(chunks: list[dict], row: dict | None) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([float((row or {}).get('mtime') or 0), int((row or {}).get('size') or 0)]).encode())
    for chunk in chunks[:MAX_CHUNKS]:
        digest.update(str(chunk.get('text', '')).encode('utf-8', 'replace'))
    return digest.hexdigest()


@dataclass
class Session:
    path: str
    version: str
    language: str
    count: int
    section: str
    options: bool
    order: list[int]
    questions: list[Question]
    position: int = 0
    state: str = 'asked'          # asked | feedback | paused | finished | cancelled
    retry_queue: list[int] = field(default_factory=list)
    retry_used: bool = False
    started_at: float = field(default_factory=time.monotonic)

    @property
    def current(self) -> Question | None:
        if self.retry_queue:
            return self.questions[self.retry_queue[0]]
        return self.questions[self.position] if self.position < len(self.questions) else None

    @property
    def label(self) -> str:
        return ntpath.basename(self.path.replace('/', '\\'))


def build(path: str, chunks: list[dict], row: dict | None, code: str, *, count: int = DEFAULT_QUESTIONS,
          section: str = '', options: bool = False, order: list[int] | None = None) -> Session | None:
    selected = chunks
    if section:
        key = _norm(section)
        selected = [c for c in chunks if key and (key in _norm(c.get('text', '')) or key in _norm(c.get('location', '')))]
        if not selected:
            return None
    cands = candidates(selected)
    order = [i for i in (order if order is not None else pick(cands, max(1, min(MAX_QUESTIONS, int(count))))) if 0 <= i < len(cands)]
    if not order:
        return None
    questions = []
    terms = [c[2] for c in cands]
    for number, i in enumerate(order, 1):
        chunk, sentence, term, location = cands[i]
        opts: list[str] = []
        if options:
            others = sorted({t for t in terms if t.casefold() != term.casefold() and t[0].isdigit() == term[0].isdigit()},
                            key=lambda t: hashlib.sha1((t + term).encode()).hexdigest())[:2]
            opts = sorted(others + [term], key=lambda t: hashlib.sha1((term + '|' + t).encode()).hexdigest())
        questions.append(Question(number, sentence, term, location, chunk, opts))
    return Session(path, source_version(chunks, row), code, len(questions), section, options, order, questions)


# --------------------------------------------------------------- language
_T = {
    # NEO 188 atlas: «пропусти этот вопрос» / «помоги изучить документ» without a running session.
    'no_session188': ('Сейчас нет учебного занятия. Чтобы начать, назовите файл: «потренируй меня по файлу конспект.docx».',
                      'There is no study session right now. To start one, name the file: “quiz me on notes.docx”.',
                      'Gerade läuft keine Lernsitzung. Nenne zum Start die Datei: „trainiere mich mit notizen.docx“.',
                      'Aucune séance d’étude en cours. Pour commencer, nomme le fichier : « entraîne-moi sur notes.docx ».',
                      'Ahora no hay una sesión de estudio. Para empezar, di el archivo: «entréname con apuntes.docx».',
                      'Ora non c’è una sessione di studio. Per iniziare, indica il file: «allenami su appunti.docx».',
                      'Agora não há uma sessão de estudo. Para começar, indica o ficheiro: «treina-me com apontamentos.docx».'),
    'start': ('Тренировка по «{file}»: {n} вопрос(ов) по одному. Я проверяю ответы только по выбранному тексту. Команды: «подсказка», «объясни ответ», «пропусти», «повтори вопрос», «пауза», «сохрани место», «отмени тренировку».',
              'Practice on “{file}”: {n} question(s), one at a time. I check answers only against the chosen text. Say “hint”, “explain the answer”, “skip”, “repeat the question”, “pause”, “save my place” or “cancel the training”.',
              'Training zu „{file}“: {n} Frage(n), eine nach der anderen. Ich prüfe Antworten nur am gewählten Text. Sag „Tipp“, „erkläre die Antwort“, „überspringen“, „wiederhole die Frage“, „Pause“, „speichere die Stelle“ oder „brich das Training ab“.',
              'Entraînement sur « {file} » : {n} question(s), une à la fois. Je vérifie les réponses uniquement avec le texte choisi. Dites « indice », « explique la réponse », « passe », « répète la question », « pause », « sauvegarde ma position » ou « annule l’entraînement ».',
              'Práctica con «{file}»: {n} pregunta(s), una a una. Compruebo las respuestas solo con el texto elegido. Di «pista», «explica la respuesta», «salta», «repite la pregunta», «pausa», «guarda mi posición» o «cancela el entrenamiento».',
              'Allenamento su «{file}»: {n} domanda/e, una alla volta. Controllo le risposte solo sul testo scelto. Di’ «suggerimento», «spiega la risposta», «salta», «ripeti la domanda», «pausa», «salva la posizione» o «annulla l’allenamento».',
              'Treino com «{file}»: {n} pergunta(s), uma de cada vez. Verifico as respostas só com o texto escolhido. Diga «dica», «explica a resposta», «salta», «repete a pergunta», «pausa», «guarda a posição» ou «cancela o treino».'),
    'question': ('❓ Вопрос {i} из {n}. Какое слово пропущено?\n«{cloze}»', '❓ Question {i} of {n}. Which word is missing?\n“{cloze}”',
                 '❓ Frage {i} von {n}. Welches Wort fehlt?\n„{cloze}“', '❓ Question {i} sur {n}. Quel mot manque ?\n« {cloze} »',
                 '❓ Pregunta {i} de {n}. ¿Qué palabra falta?\n«{cloze}»', '❓ Domanda {i} di {n}. Quale parola manca?\n«{cloze}»',
                 '❓ Pergunta {i} de {n}. Que palavra falta?\n«{cloze}»'),
    'question_other': ('❓ Вопрос {i} из {n} (другими словами): допишите предложение из документа одним словом:\n«{cloze}»',
                       '❓ Question {i} of {n} (in other words): complete this sentence from the document with one word:\n“{cloze}”',
                       '❓ Frage {i} von {n} (anders gesagt): Ergänze diesen Satz aus dem Dokument mit einem Wort:\n„{cloze}“',
                       '❓ Question {i} sur {n} (autrement dit) : complétez cette phrase du document par un mot :\n« {cloze} »',
                       '❓ Pregunta {i} de {n} (con otras palabras): completa esta frase del documento con una palabra:\n«{cloze}»',
                       '❓ Domanda {i} di {n} (in altre parole): completa questa frase del documento con una parola:\n«{cloze}»',
                       '❓ Pergunta {i} de {n} (por outras palavras): completa esta frase do documento com uma palavra:\n«{cloze}»'),
    'options': ('Варианты: {opts}', 'Options: {opts}', 'Optionen: {opts}', 'Choix : {opts}', 'Opciones: {opts}', 'Opzioni: {opts}', 'Opções: {opts}'),
    'matched': ('✅ Совпадает с выбранным текстом.', '✅ This matches the chosen text.', '✅ Das stimmt mit dem gewählten Text überein.',
                '✅ Cela correspond au texte choisi.', '✅ Coincide con el texto elegido.', '✅ Corrisponde al testo scelto.', '✅ Corresponde ao texto escolhido.'),
    'close': ('🟡 Почти: в тексте — «{a}».', '🟡 Close: the text says “{a}”.', '🟡 Fast: Im Text steht „{a}“.', '🟡 Presque : le texte dit « {a} ».',
              '🟡 Casi: el texto dice «{a}».', '🟡 Quasi: il testo dice «{a}».', '🟡 Quase: o texto diz «{a}».'),
    'wrong': ('❌ В выбранном тексте здесь стоит «{a}».', '❌ The chosen text has “{a}” here.', '❌ Im gewählten Text steht hier „{a}“.',
              '❌ Le texte choisi a « {a} » ici.', '❌ El texto elegido tiene «{a}» aquí.', '❌ Il testo scelto ha «{a}» qui.', '❌ O texto escolhido tem «{a}» aqui.'),
    'unclear': ('Не уверен, что расслышал ответ правильно («{heard}»). Повторите, пожалуйста, одно слово — это не считается ошибкой.',
                'I’m not sure I heard the answer right (“{heard}”). Please say just one word — this does not count as a mistake.',
                'Ich bin nicht sicher, ob ich die Antwort richtig verstanden habe („{heard}“). Sag bitte nur ein Wort – das zählt nicht als Fehler.',
                'Je ne suis pas sûr d’avoir bien entendu (« {heard} »). Dites un seul mot — ce n’est pas compté comme une erreur.',
                'No estoy seguro de haber oído bien («{heard}»). Di solo una palabra; no cuenta como error.',
                'Non sono sicuro di aver capito («{heard}»). Di’ una sola parola: non conta come errore.',
                'Não tenho a certeza de ter ouvido bem («{heard}»). Diga só uma palavra — não conta como erro.'),
    'source': ('📄 Фрагмент: {file}{where}', '📄 Fragment: {file}{where}', '📄 Fragment: {file}{where}', '📄 Fragment : {file}{where}',
               '📄 Fragmento: {file}{where}', '📄 Frammento: {file}{where}', '📄 Fragmento: {file}{where}'),
    'hint1': ('💡 Подсказка: слово начинается на «{c}» и состоит из {n} символов.', '💡 Hint: the word starts with “{c}” and has {n} characters.',
              '💡 Tipp: Das Wort beginnt mit „{c}“ und hat {n} Zeichen.', '💡 Indice : le mot commence par « {c} » et compte {n} caractères.',
              '💡 Pista: la palabra empieza por «{c}» y tiene {n} caracteres.', '💡 Suggerimento: la parola inizia con «{c}» e ha {n} caratteri.',
              '💡 Dica: a palavra começa por «{c}» e tem {n} caracteres.'),
    'hint2': ('💡 Ещё подсказка: «{mask}». Полный ответ — по просьбе «объясни ответ».', '💡 Another hint: “{mask}”. Say “explain the answer” for the full answer.',
              '💡 Noch ein Tipp: „{mask}“. Die ganze Antwort mit „erkläre die Antwort“.', '💡 Autre indice : « {mask} ». La réponse complète : « explique la réponse ».',
              '💡 Otra pista: «{mask}». La respuesta completa: «explica la respuesta».', '💡 Altro suggerimento: «{mask}». La risposta completa: «spiega la risposta».',
              '💡 Outra dica: «{mask}». A resposta completa: «explica a resposta».'),
    'explain': ('📖 Ответ: «{a}». В документе: «{s}»', '📖 Answer: “{a}”. In the document: “{s}”', '📖 Antwort: „{a}“. Im Dokument: „{s}“',
                '📖 Réponse : « {a} ». Dans le document : « {s} »', '📖 Respuesta: «{a}». En el documento: «{s}»', '📖 Risposta: «{a}». Nel documento: «{s}»',
                '📖 Resposta: «{a}». No documento: «{s}»'),
    'skipped': ('⏭ Вопрос пропущен — это не ошибка.', '⏭ Question skipped — this is not a mistake.', '⏭ Frage übersprungen – das ist kein Fehler.',
                '⏭ Question passée — ce n’est pas une erreur.', '⏭ Pregunta saltada: no es un error.', '⏭ Domanda saltata: non è un errore.',
                '⏭ Pergunta saltada — não é um erro.'),
    'paused': ('⏸ Тренировка на паузе: вопросов и прослушивания нет. Скажите «продолжи тренировку». Позиция хранится только до закрытия NEO — чтобы сохранить её, скажите «сохрани место».',
               '⏸ Practice paused: no questions and no listening. Say “continue the training”. The position is kept only until NEO closes — say “save my place” to keep it.',
               '⏸ Training pausiert: keine Fragen und kein Zuhören. Sag „setze das Training fort“. Die Position bleibt nur bis zum Schließen von NEO – sag „speichere die Stelle“, um sie zu behalten.',
               '⏸ Entraînement en pause : pas de questions ni d’écoute. Dites « continue l’entraînement ». La position n’est gardée que jusqu’à la fermeture de NEO — dites « sauvegarde ma position » pour la conserver.',
               '⏸ Práctica en pausa: sin preguntas ni escucha. Di «continúa el entrenamiento». La posición se guarda solo hasta cerrar NEO; di «guarda mi posición» para conservarla.',
               '⏸ Allenamento in pausa: niente domande né ascolto. Di’ «continua l’allenamento». La posizione resta solo fino alla chiusura di NEO: di’ «salva la posizione» per conservarla.',
               '⏸ Treino em pausa: sem perguntas nem escuta. Diga «continua o treino». A posição só fica até fechar o NEO — diga «guarda a posição» para a guardar.'),
    'command_paused': ('Похоже, это команда, а не ответ. Тренировку поставил на паузу — повторите команду, и я её выполню; потом скажите «продолжи тренировку».',
                       'That looks like a command, not an answer. I paused the practice — repeat the command and I’ll run it; then say “continue the training”.',
                       'Das sieht nach einem Befehl aus, nicht nach einer Antwort. Ich habe das Training pausiert – wiederhole den Befehl, dann führe ich ihn aus; danach sag „setze das Training fort“.',
                       'Cela ressemble à une commande, pas à une réponse. J’ai mis l’entraînement en pause — répétez la commande et je l’exécuterai ; puis dites « continue l’entraînement ».',
                       'Parece un comando, no una respuesta. Pausé la práctica: repite el comando y lo ejecutaré; luego di «continúa el entrenamiento».',
                       'Sembra un comando, non una risposta. Ho messo in pausa l’allenamento: ripeti il comando e lo eseguirò; poi di’ «continua l’allenamento».',
                       'Parece um comando, não uma resposta. Pus o treino em pausa — repita o comando e eu executo-o; depois diga «continua o treino».'),
    'cancelled': ('Тренировка отменена.', 'Practice cancelled.', 'Training abgebrochen.', 'Entraînement annulé.', 'Práctica cancelada.', 'Allenamento annullato.', 'Treino cancelado.'),
    'has_checkpoint': (' Сохранённое место по «{file}» осталось; удалить — «удали сохранённую тренировку».', ' The saved place for “{file}” is still there; say “delete the saved training” to remove it.',
                       ' Die gespeicherte Stelle zu „{file}“ bleibt; zum Löschen sag „lösche das gespeicherte Training“.', ' La position sauvegardée pour « {file} » reste ; pour la supprimer : « supprime l’entraînement sauvegardé ».',
                       ' La posición guardada de «{file}» sigue ahí; para borrarla di «borra el entrenamiento guardado».', ' La posizione salvata per «{file}» resta; per eliminarla di’ «elimina l’allenamento salvato».',
                       ' A posição guardada de «{file}» continua; para a apagar diga «apaga o treino guardado».'),
    'saved': ('💾 Место сохранено: «{file}», вопрос {i} из {n}. Сохранены только документ, его версия, язык, настройки и номера вопросов — без ваших ответов и записи голоса.',
              '💾 Place saved: “{file}”, question {i} of {n}. Only the document, its version, the language, the settings and question numbers are stored — no answers and no voice recording.',
              '💾 Stelle gespeichert: „{file}“, Frage {i} von {n}. Gespeichert sind nur Dokument, Version, Sprache, Einstellungen und Fragenummern – keine Antworten und keine Sprachaufnahme.',
              '💾 Position sauvegardée : « {file} », question {i} sur {n}. Seuls le document, sa version, la langue, les réglages et les numéros de questions sont gardés — sans vos réponses ni enregistrement de la voix.',
              '💾 Posición guardada: «{file}», pregunta {i} de {n}. Solo se guardan el documento, su versión, el idioma, los ajustes y los números de pregunta, sin tus respuestas ni grabación de voz.',
              '💾 Posizione salvata: «{file}», domanda {i} di {n}. Sono salvati solo documento, versione, lingua, impostazioni e numeri delle domande, senza risposte né registrazione della voce.',
              '💾 Posição guardada: «{file}», pergunta {i} de {n}. Só ficam o documento, a versão, o idioma, as definições e os números das perguntas — sem respostas nem gravação de voz.'),
    'save_failed': ('Не удалось сохранить место — позиция осталась только в памяти.', 'Could not save the place — the position stays only in memory.',
                    'Die Stelle konnte nicht gespeichert werden – die Position bleibt nur im Speicher.', 'Impossible de sauvegarder la position — elle reste seulement en mémoire.',
                    'No se pudo guardar la posición: queda solo en memoria.', 'Impossibile salvare la posizione: resta solo in memoria.', 'Não foi possível guardar a posição — fica só na memória.'),
    'deleted': ('🗑 Сохранённая тренировка по «{file}» удалена. Сам документ не тронут.', '🗑 The saved training for “{file}” was deleted. The document itself is untouched.',
                '🗑 Das gespeicherte Training zu „{file}“ wurde gelöscht. Das Dokument selbst bleibt unverändert.', '🗑 L’entraînement sauvegardé pour « {file} » a été supprimé. Le document lui-même n’est pas touché.',
                '🗑 Se borró el entrenamiento guardado de «{file}». El documento no se tocó.', '🗑 L’allenamento salvato per «{file}» è stato eliminato. Il documento non è stato toccato.',
                '🗑 O treino guardado de «{file}» foi apagado. O documento não foi tocado.'),
    'delete_failed': ('Не удалось удалить сохранённую тренировку полностью — файл: {p}.', 'Could not fully delete the saved training — file: {p}.',
                      'Das gespeicherte Training konnte nicht vollständig gelöscht werden – Datei: {p}.', 'Impossible de supprimer complètement l’entraînement sauvegardé — fichier : {p}.',
                      'No se pudo borrar del todo el entrenamiento guardado; archivo: {p}.', 'Impossibile eliminare del tutto l’allenamento salvato: file {p}.',
                      'Não foi possível apagar totalmente o treino guardado — ficheiro: {p}.'),
    'no_checkpoint': ('Сохранённой тренировки нет.', 'There is no saved training.', 'Es gibt kein gespeichertes Training.', 'Aucun entraînement sauvegardé.',
                      'No hay entrenamiento guardado.', 'Non c’è un allenamento salvato.', 'Não há treino guardado.'),
    'changed': ('Документ «{file}» изменился или больше не доступен с момента сохранения, поэтому прежние вопросы не продолжаю. Начать заново: «потренируй меня по {file}».',
                'The document “{file}” changed or is no longer available since the place was saved, so I won’t continue the old questions. Start again: “quiz me on {file}”.',
                'Das Dokument „{file}“ hat sich seit dem Speichern geändert oder ist nicht mehr verfügbar, daher setze ich die alten Fragen nicht fort. Neu starten: „trainiere mich mit {file}“.',
                'Le document « {file} » a changé ou n’est plus disponible depuis la sauvegarde ; je ne reprends pas les anciennes questions. Recommencer : « entraîne-moi sur {file} ».',
                'El documento «{file}» cambió o ya no está disponible desde que se guardó, así que no sigo con las preguntas antiguas. Empezar de nuevo: «entréname con {file}».',
                'Il documento «{file}» è cambiato o non è più disponibile dal salvataggio, quindi non riprendo le vecchie domande. Ricominciare: «allenami su {file}».',
                'O documento «{file}» mudou ou já não está disponível desde que foi guardado, por isso não continuo as perguntas antigas. Recomeçar: «treina-me com {file}».'),
    'no_text': ('В «{file}» не нашёл подходящих предложений для вопросов (документ пуст, не прочитан или раздел «{section}» не найден). Тренировку не начинаю.',
                'I found no suitable sentences for questions in “{file}” (the document is empty, not read, or the section “{section}” was not found). I won’t start the practice.',
                'In „{file}“ habe ich keine geeigneten Sätze für Fragen gefunden (das Dokument ist leer, nicht gelesen oder der Abschnitt „{section}“ fehlt). Ich starte das Training nicht.',
                'Je n’ai trouvé aucune phrase adaptée dans « {file} » (document vide, non lu ou section « {section} » introuvable). Je ne lance pas l’entraînement.',
                'No encontré frases adecuadas en «{file}» (el documento está vacío, no se leyó o no se encontró la sección «{section}»). No inicio la práctica.',
                'Non ho trovato frasi adatte in «{file}» (documento vuoto, non letto o sezione «{section}» non trovata). Non avvio l’allenamento.',
                'Não encontrei frases adequadas em «{file}» (documento vazio, não lido ou secção «{section}» não encontrada). Não começo o treino.'),
    'summary': ('🏁 Итог по «{file}» (только по {n} рассмотренным фрагментам, не по всему документу): совпало — {m}, почти — {c}, не совпало — {w}, пропущено — {s}, не удалось проверить — {u}. Это не оценка знаний.',
                '🏁 Summary for “{file}” (only the {n} fragments covered, not the whole document): matched — {m}, close — {c}, different — {w}, skipped — {s}, could not check — {u}. This is not a grade.',
                '🏁 Zusammenfassung zu „{file}“ (nur die {n} behandelten Fragmente, nicht das ganze Dokument): richtig — {m}, fast — {c}, abweichend — {w}, übersprungen — {s}, nicht prüfbar — {u}. Das ist keine Note.',
                '🏁 Bilan pour « {file} » (seulement les {n} fragments vus, pas tout le document) : correct — {m}, presque — {c}, différent — {w}, passé — {s}, non vérifiable — {u}. Ce n’est pas une note.',
                '🏁 Resumen de «{file}» (solo los {n} fragmentos vistos, no todo el documento): coincide — {m}, casi — {c}, distinto — {w}, saltadas — {s}, sin verificar — {u}. No es una nota.',
                '🏁 Riepilogo per «{file}» (solo i {n} frammenti visti, non tutto il documento): giuste — {m}, quasi — {c}, diverse — {w}, saltate — {s}, non verificabili — {u}. Non è un voto.',
                '🏁 Resumo de «{file}» (só os {n} fragmentos vistos, não todo o documento): certas — {m}, quase — {c}, diferentes — {w}, saltadas — {s}, sem verificação — {u}. Não é uma nota.'),
    'retry_offer': (' Пропущено {s} — можно сказать «повторим пропущенные».', ' {s} skipped — you can say “repeat the skipped questions”.',
                    ' {s} übersprungen – du kannst „wiederhole die übersprungenen Fragen“ sagen.', ' {s} passée(s) — vous pouvez dire « reprenons les questions passées ».',
                    ' {s} saltada(s): puedes decir «repasemos las preguntas saltadas».', ' {s} saltata/e: puoi dire «ripetiamo le domande saltate».',
                    ' {s} saltada(s) — pode dizer «repetimos as perguntas saltadas».'),
    'no_retry': ('Пропущенных вопросов для повтора нет.', 'There are no skipped questions to repeat.', 'Es gibt keine übersprungenen Fragen zum Wiederholen.',
                 'Il n’y a pas de questions passées à reprendre.', 'No hay preguntas saltadas que repasar.', 'Non ci sono domande saltate da ripetere.', 'Não há perguntas saltadas para repetir.'),
    'resumed': ('▶ Продолжаем «{file}».', '▶ Continuing “{file}”.', '▶ Weiter mit „{file}“.', '▶ On continue « {file} ».', '▶ Seguimos con «{file}».', '▶ Continuiamo «{file}».', '▶ Continuamos «{file}».'),
    'unknown_file': ('Не нашёл «{name}» среди разрешённых проиндексированных документов. Добавьте папку в Настройки → Локальный ИИ или назовите файл точнее.',
                     'I didn’t find “{name}” among the allowed indexed documents. Add its folder in Settings → Local AI or name the file more precisely.',
                     '„{name}“ ist nicht unter den erlaubten indexierten Dokumenten. Füge den Ordner unter Einstellungen → Lokale KI hinzu oder nenne die Datei genauer.',
                     'Je n’ai pas trouvé « {name} » parmi les documents indexés autorisés. Ajoutez son dossier dans Réglages → IA locale ou précisez le fichier.',
                     'No encontré «{name}» entre los documentos indexados permitidos. Añade su carpeta en Ajustes → IA local o nombra el archivo con más precisión.',
                     'Non ho trovato «{name}» tra i documenti indicizzati consentiti. Aggiungi la cartella in Impostazioni → IA locale o indica il file con precisione.',
                     'Não encontrei «{name}» entre os documentos indexados permitidos. Adicione a pasta em Definições → IA local ou indique o ficheiro com mais precisão.'),
    'ambiguous_file': ('Под «{name}» подходит несколько документов — назовите полный путь:\n{choices}', 'Several documents match “{name}” — say the full path:\n{choices}',
                       'Mehrere Dokumente passen zu „{name}“ – nenne den vollständigen Pfad:\n{choices}', 'Plusieurs documents correspondent à « {name} » — indiquez le chemin complet :\n{choices}',
                       'Varios documentos coinciden con «{name}»: di la ruta completa:\n{choices}', 'Più documenti corrispondono a «{name}»: indica il percorso completo:\n{choices}',
                       'Vários documentos correspondem a «{name}» — diga o caminho completo:\n{choices}'),
}


def t(key: str, code: str, **values: Any) -> str:
    return _T[key][_CODES.index(code) if code in _CODES else 1].format(**values)


_CMD = {
    'hint': {'ru': r'(?:дай\s+|можно\s+)?подсказк[ауи]|намекни|подскажи', 'en': r'(?:give\s+me\s+)?a\s+hint|hint(?:\s+please)?', 'de': r'(?:gib\s+mir\s+)?(?:einen\s+)?(?:tipp|hinweis)',
             'fr': r'(?:donne[- ]moi\s+)?un\s+indice|indice', 'es': r'(?:dame\s+)?una\s+pista|pista', 'it': r'(?:dammi\s+)?un\s+(?:suggerimento|indizio)|suggerimento|indizio',
             'pt': r'(?:dá[- ]me\s+)?uma\s+dica|dica'},
    'explain': {'ru': r'объясни(?:\s+ответ)?(?:\s+и\s+покажи\s+место(?:\s+в\s+(?:конспекте|документе))?)?|покажи\s+(?:правильный\s+)?ответ|покажи\s+место(?:\s+в\s+(?:конспекте|документе))?|какой\s+(?:правильный\s+)?ответ|сдаюсь',
                'en': r'explain(?:\s+the\s+answer)?(?:\s+and\s+show\s+(?:me\s+)?where)?|show\s+(?:me\s+)?the\s+answer|show\s+(?:me\s+)?where(?:\s+it\s+is)?|what\s+is\s+the\s+answer|i\s+give\s+up',
                'de': r'erkläre?(?:\s+die\s+antwort)?|zeig(?:e)?\s+(?:mir\s+)?(?:die\s+antwort|die\s+stelle)|was\s+ist\s+die\s+antwort|ich\s+gebe\s+auf',
                'fr': r'explique(?:\s+la\s+réponse)?|montre[- ]moi\s+(?:la\s+réponse|l[’\']endroit)|quelle\s+est\s+la\s+réponse|j[’\']abandonne',
                'es': r'explica(?:\s+la\s+respuesta)?|muéstrame\s+(?:la\s+respuesta|dónde)|cuál\s+es\s+la\s+respuesta|me\s+rindo',
                'it': r'spiega(?:\s+la\s+risposta)?|mostrami\s+(?:la\s+risposta|dove)|qual\s+è\s+la\s+risposta|mi\s+arrendo',
                'pt': r'explica(?:\s+a\s+resposta)?|mostra[- ]me\s+(?:a\s+resposta|onde)|qual\s+é\s+a\s+resposta|desisto'},
    'skip': {'ru': r'пропусти(?:\s+(?:этот\s+)?вопрос)?|пропуск|следующий(?:\s+вопрос)?', 'en': r'skip(?:\s+(?:this|the)\s+question)?|skip\s+it|next\s+question|pass',
             'de': r'überspring(?:e|en)?(?:\s+(?:die|diese)\s+frage)?|nächste\s+frage', 'fr': r'passe(?:\s+(?:cette|la)\s+question)?|question\s+suivante',
             'es': r'salta(?:\s+(?:esta|la)\s+pregunta)?|siguiente\s+pregunta|paso', 'it': r'salta(?:\s+(?:questa|la)\s+domanda)?|prossima\s+domanda|passo',
             'pt': r'salta(?:\s+(?:esta|a)\s+pergunta)?|próxima\s+pergunta|passo'},
    'repeat': {'ru': r'повтори(?:\s+вопрос)?(?:\s+другими\s+словами)?|перефразируй|скажи\s+иначе', 'en': r'repeat(?:\s+the\s+question)?(?:\s+in\s+other\s+words)?|rephrase(?:\s+the\s+question)?|say\s+(?:it|that)\s+again',
               'de': r'wiederhole?(?:\s+die\s+frage)?(?:\s+mit\s+anderen\s+worten)?|formuliere?\s+(?:es\s+)?anders|nochmal', 'fr': r'répète(?:\s+la\s+question)?(?:\s+autrement)?|reformule(?:\s+la\s+question)?',
               'es': r'repite(?:\s+la\s+pregunta)?(?:\s+con\s+otras\s+palabras)?|reformula(?:\s+la\s+pregunta)?', 'it': r'ripeti(?:\s+la\s+domanda)?(?:\s+con\s+altre\s+parole)?|riformula(?:\s+la\s+domanda)?',
               'pt': r'repete(?:\s+a\s+pergunta)?(?:\s+por\s+outras\s+palavras)?|reformula(?:\s+a\s+pergunta)?'},
    'pause': {'ru': r'пауза|сделай\s+паузу|поставь\s+(?:тренировку\s+)?на\s+паузу|приостанови(?:\s+тренировку)?', 'en': r'pause(?:\s+the\s+(?:training|practice|quiz))?|take\s+a\s+break',
              'de': r'pause|mach\s+(?:eine\s+)?pause|pausiere(?:\s+das\s+training)?', 'fr': r'pause|fais\s+une\s+pause|mets\s+(?:l[’\']entraînement\s+)?en\s+pause',
              'es': r'pausa|haz\s+una\s+pausa|pausa\s+el\s+entrenamiento', 'it': r'pausa|fai\s+una\s+pausa|metti\s+in\s+pausa(?:\s+l[’\']allenamento)?',
              'pt': r'pausa|faz\s+uma\s+pausa|põe\s+(?:o\s+treino\s+)?em\s+pausa'},
    'resume': {'ru': r'продолж(?:и|ай|им)(?:\s+тренировку)?|продолжаем', 'en': r'(?:continue|resume)(?:\s+the\s+(?:training|practice|quiz))?',
               'de': r'(?:setze?|mach)\s+(?:das\s+training\s+)?(?:fort|weiter)|weiter\s+mit\s+dem\s+training|fortsetzen', 'fr': r'continue(?:r)?(?:\s+l[’\']entraînement)?|reprends(?:\s+l[’\']entraînement)?',
               'es': r'continúa(?:\s+el\s+entrenamiento)?|reanuda(?:\s+el\s+entrenamiento)?|sigue\s+con\s+el\s+entrenamiento', 'it': r'continua(?:\s+l[’\']allenamento)?|riprendi(?:\s+l[’\']allenamento)?',
               'pt': r'continua(?:\s+o\s+treino)?|retoma(?:\s+o\s+treino)?'},
    'save': {'ru': r'сохрани\s+(?:место|позицию|тренировку|прогресс)(?:[,\s]+продолжу\s+(?:завтра|позже|потом))?', 'en': r'save\s+(?:my\s+)?(?:place|position|progress)(?:[,\s]+i[’\']?ll\s+continue\s+(?:tomorrow|later))?',
             'de': r'speichere?\s+(?:die\s+|meine\s+)?(?:stelle|position|fortschritt)', 'fr': r'(?:sauvegarde|enregistre)\s+(?:ma\s+)?(?:position|place|progression)',
             'es': r'guarda\s+(?:mi\s+)?(?:posición|lugar|progreso)', 'it': r'salva\s+(?:la\s+|il\s+)?(?:posizione|punto|progresso)', 'pt': r'guarda\s+(?:a\s+|o\s+)?(?:posição|progresso|lugar)'},
    'cancel': {'ru': r'(?:отмени|останови|закончи|заверши)\s+тренировку|хватит\s+тренировки|стоп\s+тренировка', 'en': r'(?:cancel|stop|end|quit)\s+the\s+(?:training|practice|quiz)',
               'de': r'(?:brich|beende)\s+das\s+training(?:\s+ab)?', 'fr': r'(?:annule|arrête|termine)\s+l[’\']entraînement', 'es': r'(?:cancela|termina|detén)\s+el\s+entrenamiento',
               'it': r'(?:annulla|termina|ferma)\s+l[’\']allenamento', 'pt': r'(?:cancela|termina|para)\s+o\s+treino'},
    'delete': {'ru': r'удали\s+сохран[её]нную\s+тренировку(?:\s+по\s+(?:этому\s+документу|.+))?', 'en': r'delete\s+the\s+saved\s+(?:training|practice|session)(?:\s+for\s+.+)?',
               'de': r'lösche\s+das\s+gespeicherte\s+training(?:\s+zu\s+.+)?', 'fr': r'supprime\s+l[’\']entraînement\s+sauvegardé(?:\s+pour\s+.+)?',
               'es': r'borra\s+el\s+entrenamiento\s+guardado(?:\s+de\s+.+)?', 'it': r'elimina\s+l[’\']allenamento\s+salvato(?:\s+per\s+.+)?', 'pt': r'apaga\s+o\s+treino\s+guardado(?:\s+de\s+.+)?'},
    'retry': {'ru': r'повторим\s+(?:только\s+)?(?:пропущенные|трудные)(?:\s+вопросы)?|вернись\s+к\s+пропущенным', 'en': r'(?:let[’\']?s\s+)?repeat\s+(?:only\s+)?the\s+(?:skipped|hard)\s+(?:questions|ones)|go\s+back\s+to\s+the\s+skipped\s+(?:questions|ones)',
              'de': r'wiederhole\s+(?:nur\s+)?die\s+übersprungenen\s+fragen', 'fr': r'reprenons\s+(?:seulement\s+)?les\s+questions\s+passées', 'es': r'repasemos\s+(?:solo\s+)?las\s+preguntas\s+saltadas',
              'it': r'ripetiamo\s+(?:solo\s+)?le\s+domande\s+saltate', 'pt': r'repetimos\s+(?:só\s+)?as\s+perguntas\s+saltadas'},
}
_CMD_RE = {name: {code: re.compile(r'^(?:(?:neo|нео)[,\s]+)?(?:' + pattern + r')$', re.I) for code, pattern in rows.items()} for name, rows in _CMD.items()}

_START = {
    'ru': (r'(?:потренируй|проверь|поэкзаменуй)\s+меня\s+по\s+(?:файлу\s+|документу\s+|конспекту\s+|заметкам\s+)?(?P<file>.+?)', r'задай\s+мне\s+(?P<n>\S+)\s+вопрос\w*\s+по\s+(?:файлу\s+|документу\s+|конспекту\s+)?(?P<file>.+?)'),
    'en': (r'(?:quiz|train|test)\s+me\s+(?:on|with|from)\s+(?:the\s+)?(?:file\s+|document\s+|notes\s+)?(?P<file>.+?)', r'ask\s+me\s+(?P<n>\S+)\s+questions?\s+(?:about|on|from)\s+(?:the\s+)?(?:file\s+|document\s+|notes\s+)?(?P<file>.+?)'),
    'de': (r'(?:trainiere|prüfe|teste)\s+mich\s+(?:mit|zu|anhand)\s+(?:der\s+datei\s+|dem\s+dokument\s+)?(?P<file>.+?)', r'stell\s+mir\s+(?P<n>\S+)\s+fragen?\s+zu\s+(?:der\s+datei\s+|dem\s+dokument\s+)?(?P<file>.+?)'),
    'fr': (r'entraîne[- ]moi\s+(?:sur|avec)\s+(?:le\s+fichier\s+|le\s+document\s+)?(?P<file>.+?)', r'pose[- ]moi\s+(?P<n>\S+)\s+questions?\s+sur\s+(?:le\s+fichier\s+|le\s+document\s+)?(?P<file>.+?)'),
    'es': (r'(?:entréname|examíname)\s+(?:con|sobre)\s+(?:el\s+archivo\s+|el\s+documento\s+)?(?P<file>.+?)', r'hazme\s+(?P<n>\S+)\s+preguntas?\s+sobre\s+(?:el\s+archivo\s+|el\s+documento\s+)?(?P<file>.+?)'),
    'it': (r'(?:allenami|interrogami)\s+(?:su|con)\s+(?:il\s+file\s+|il\s+documento\s+)?(?P<file>.+?)', r'fammi\s+(?P<n>\S+)\s+domande?\s+su\s+(?:il\s+file\s+|il\s+documento\s+)?(?P<file>.+?)'),
    'pt': (r'(?:treina[- ]me|testa[- ]me)\s+(?:com|sobre)\s+(?:o\s+ficheiro\s+|o\s+arquivo\s+|o\s+documento\s+)?(?P<file>.+?)', r'faz[- ]me\s+(?P<n>\S+)\s+perguntas?\s+sobre\s+(?:o\s+ficheiro\s+|o\s+arquivo\s+|o\s+documento\s+)?(?P<file>.+?)'),
}
_COUNT = {'ru': r'(?P<n>\S+)\s+вопрос\w*', 'en': r'(?P<n>\S+)\s+questions?', 'de': r'(?P<n>\S+)\s+fragen?', 'fr': r'(?P<n>\S+)\s+questions?',
          'es': r'(?P<n>\S+)\s+preguntas?', 'it': r'(?P<n>\S+)\s+domande?', 'pt': r'(?P<n>\S+)\s+perguntas?'}
_SECTION = {'ru': r'по\s+(?:разделу|главе|теме)\s+(?P<s>.+)', 'en': r'(?:on|from)\s+(?:the\s+)?(?:section|chapter|topic)\s+(?P<s>.+)', 'de': r'(?:zum|aus\s+dem)\s+(?:abschnitt|kapitel|thema)\s+(?P<s>.+)',
            'fr': r'sur\s+(?:la\s+section|le\s+chapitre|le\s+thème)\s+(?P<s>.+)', 'es': r'sobre\s+(?:la\s+sección|el\s+capítulo|el\s+tema)\s+(?P<s>.+)',
            'it': r'su(?:lla)?\s+(?:sezione|capitolo|tema)\s+(?P<s>.+)', 'pt': r'sobre\s+(?:a\s+secção|a\s+seção|o\s+capítulo|o\s+tema)\s+(?P<s>.+)'}
_OPTIONS = {'ru': r'с\s+вариантами(?:\s+ответа)?', 'en': r'with\s+(?:answer\s+)?(?:options|choices)', 'de': r'mit\s+(?:antwort)?optionen', 'fr': r'avec\s+des\s+choix',
            'es': r'con\s+opciones', 'it': r'con\s+opzioni', 'pt': r'com\s+opções'}
_CONTINUE_FILE = {'ru': r'продолжи\s+тренировку\s+по\s+(?P<file>.+)', 'en': r'(?:continue|resume)\s+the\s+(?:training|practice|quiz)\s+(?:on|for|with)\s+(?P<file>.+)',
                  'de': r'setze\s+das\s+training\s+(?:zu|mit)\s+(?P<file>.+?)\s+fort', 'fr': r'(?:continue|reprends)\s+l[’\']entraînement\s+sur\s+(?P<file>.+)',
                  'es': r'(?:continúa|reanuda)\s+el\s+entrenamiento\s+(?:con|de)\s+(?P<file>.+)', 'it': r'(?:continua|riprendi)\s+l[’\']allenamento\s+su\s+(?P<file>.+)',
                  'pt': r'(?:continua|retoma)\s+o\s+treino\s+(?:com|de)\s+(?P<file>.+)'}


def _clean(text: str) -> str:
    return ' '.join(str(text or '').split()).strip(' .!?¿¡…')


_QUESTION_NOUN188 = {'ru': r'вопрос\w*', 'en': r'questions?', 'de': r'fragen?', 'fr': r'questions?', 'es': r'preguntas?',
                     'it': r'domand[ae]', 'pt': r'perguntas?'}
_HINT_WORD188 = {'ru': r'подсказк\w*', 'en': r'hint', 'de': r'(?:hinweis|tipp)', 'fr': r'indice', 'es': r'pista',
                 'it': r'(?:suggerimento|indizio)', 'pt': r'(?:dica|pista)'}
_STUDY_NOFILE188 = {
    'ru': r'(?:помоги|помогите)\s+(?:мне\s+)?(?:изучить|выучить|повторить)\s+(?:выбранный|этот|выделенный)\s+(?:документ|файл|конспект)',
    'en': r'help\s+me\s+(?:study|learn|revise)\s+(?:the\s+)?(?:selected|this)\s+(?:document|file|notes)',
    'de': r'hilf\s+mir,?\s+(?:das\s+)?(?:ausgewählte|dieses)\s+(?:dokument|datei)\s+zu\s+lernen',
    'fr': r'aide[- ]moi\s+à\s+(?:étudier|apprendre|réviser)\s+(?:le\s+document|le\s+fichier|ce\s+document)(?:\s+sélectionné)?',
    'es': r'ayúdame\s+a\s+(?:estudiar|aprender|repasar)\s+(?:el\s+documento|el\s+archivo|este\s+documento)(?:\s+seleccionado)?',
    'it': r'aiutami\s+a\s+(?:studiare|imparare|ripassare)\s+(?:il\s+documento|il\s+file|questo\s+documento)(?:\s+selezionato)?',
    'pt': r'ajuda[- ]me\s+a\s+(?:estudar|aprender|rever)\s+(?:o\s+documento|o\s+ficheiro|este\s+documento)(?:\s+selecionado)?',
}


def without_session188(text: str, code: str) -> bool:
    """A study request that only makes sense inside a session (or names no file to start one)."""
    value = _clean(text)
    if re.fullmatch(_STUDY_NOFILE188.get(code, r'(?!)'), value, re.I):
        return True
    noun = bool(re.search(r'(?:^|\s)(?:' + _QUESTION_NOUN188.get(code, r'(?!)') + r')(?:\s|$)', value, re.I))
    if noun and re.search(r'(?:^|\s)' + _HINT_WORD188.get(code, r'(?!)') + r'(?:\s|$)', value, re.I) and len(value.split()) <= 8:
        return True                       # «дай подсказку к вопросу»
    return noun and command(text, code) in {'skip', 'hint', 'repeat', 'explain'}


def command(text: str, code: str) -> str | None:
    value = _clean(text)
    for name in ('cancel', 'delete', 'retry', 'save', 'pause', 'resume', 'hint', 'explain', 'skip', 'repeat'):
        pattern = _CMD_RE[name].get(code)
        if pattern is not None and pattern.fullmatch(value):
            return name
    return None


def _count(raw: str, code: str) -> int | None:
    from jarvis.runtime.spoken_numbers import parse_spoken_number
    try:
        number = int(raw)
    except ValueError:
        value = parse_spoken_number(raw, code)
        number = int(value) if value is not None and value == int(value) else None
    return number


def parse_start(text: str, code: str) -> dict | None:
    """«потренируй меня по конспекту.docx: пять вопросов по главе 1» → {'file', 'count', 'section', 'options'}."""
    value = _clean(text)
    value = re.sub(r'^(?:(?:neo|нео)[,\s]+)?(?:пожалуйста[,\s]+|please[,\s]+|bitte[,\s]+)?', '', value, flags=re.I)
    for pattern in _START.get(code, ()):
        match = re.fullmatch(pattern + r'(?P<tail>(?:\s*[:,;]\s*|\s+).*)?', value, re.I)
        if not match:
            continue
        name, tail = match.group('file').strip(' "«»“”'), (match.group('tail') or '').strip(' :,;')
        count = _count(match.group('n'), code) if 'n' in match.groupdict() and match.group('n') else None
        section, options = '', False
        # The tail holds «N вопросов», «по главе X», «с вариантами» in any order.
        while tail:
            part = re.match(r'^(?:' + _COUNT[code] + r')(?:\s*[,;]\s*|\s+|$)', tail, re.I)
            if part and _count(part.group('n'), code) is not None:
                count = _count(part.group('n'), code)
                tail = tail[part.end():]
                continue
            part = re.match(r'^(?:' + _OPTIONS[code] + r')(?:\s*[,;]\s*|\s+|$)', tail, re.I)
            if part:
                options, tail = True, tail[part.end():]
                continue
            part = re.match(r'^(?:' + _SECTION[code] + r')$', tail, re.I)
            if part:
                section, tail = part.group('s').strip(' "«»“”'), ''
                continue
            # Unparsed words belong to the file name («потренируй меня по плану работ»).
            name, tail = (name + ' ' + tail).strip(), ''
        if count is not None and not 1 <= count <= MAX_QUESTIONS:
            count = max(1, min(MAX_QUESTIONS, count))
        return {'file': name, 'count': count or DEFAULT_QUESTIONS, 'section': section, 'options': options}
    return None


def parse_continue_file(text: str, code: str) -> str | None:
    pattern = _CONTINUE_FILE.get(code)
    match = re.fullmatch(pattern, _clean(text), re.I) if pattern else None
    return match.group('file').strip(' "«»“”') if match else None


# --------------------------------------------------------------- answers
_NEG = {'ru': r'(?:^|\s)(?:не|нет|ни)\s', 'en': r'(?:^|\s)(?:not|no|never)\s', 'de': r'(?:^|\s)(?:nicht|kein\w*)\s', 'fr': r'(?:^|\s)(?:ne|pas|non)\s',
        'es': r'(?:^|\s)(?:no|nunca)\s', 'it': r'(?:^|\s)(?:non|mai)\s', 'pt': r'(?:^|\s)(?:não|nunca)\s'}


def check(question: Question, answer: str, code: str) -> str:
    """matched | close | wrong | unclear — always against the fragment's own word."""
    from jarvis.runtime.spoken_numbers import parse_spoken_number
    raw = _clean(answer).strip('«»"“”\'')
    given, expected = _norm(raw), _norm(question.answer)
    if not given:
        return 'unclear'
    if question.options:
        number = _count(given, code)
        if number is not None and 1 <= number <= len(question.options):
            given = _norm(question.options[number - 1])
    if expected and expected[0].isdigit():
        digits = re.sub(r'[\s.,]', '', expected)
        spoken = parse_spoken_number(raw, code)
        candidates = {re.sub(r'[\s.,]', '', given)} | ({str(int(spoken))} if spoken is not None and spoken == int(spoken) else set())
        if digits in candidates:
            return 'matched'
        return 'wrong' if any(c.isdigit() for c in ''.join(candidates)) else 'unclear'
    words = given.split()
    if re.search(_NEG.get(code, _NEG['en']), ' ' + given + ' ') and len(words) > 1:
        return 'unclear'                 # a negation around the word: ask, never mark wrong
    if given == expected or expected in words:
        return 'matched' if len(words) <= 3 else 'close'
    best = min((_distance(w, expected) for w in words), default=99)
    if best <= (1 if len(expected) <= 6 else 2):
        return 'close'
    stem = min(len(expected), 5)
    if len(expected) >= 7 and any(len(w) >= 5 and w[:stem] == expected[:stem] for w in words):
        return 'close'
    return 'wrong' if len(words) <= 3 else 'unclear'


# --------------------------------------------------------------- checkpoints
def checkpoint_path(root: Path, source: str) -> Path:
    return Path(root) / 'study' / (hashlib.sha256(str(source).casefold().encode('utf-8')).hexdigest()[:32] + '.json')


def save_checkpoint(root: Path, session: Session) -> Path:
    data = {'v': 188, 'source': session.path, 'version': session.version, 'language': session.language, 'count': session.count,
            'section': session.section, 'options': session.options, 'order': list(session.order), 'position': session.position,
            'statuses': [q.status for q in session.questions], 'saved_at': time.time()}
    path = checkpoint_path(root, session.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
    return path


def load_checkpoint(root: Path, source: str) -> dict | None:
    path = checkpoint_path(root, source)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('v') != 188 or data.get('source') != source:
        return None
    return data


def delete_checkpoint(root: Path, source: str) -> tuple[bool, Path]:
    path = checkpoint_path(root, source)
    try:
        path.unlink()
    except FileNotFoundError:
        return False, path
    except OSError:
        return False, path
    return not path.exists(), path


def has_checkpoint(root: Path, source: str) -> bool:
    return checkpoint_path(root, source).exists()


# --------------------------------------------------------------- turn texts
def ask(session: Session, code: str, *, other: bool = False) -> str:
    q = session.current
    if q is None:
        return ''
    text = t('question_other' if other else 'question', code, i=q.number, n=session.count, cloze=q.cloze())
    if q.options:
        text += '\n' + t('options', code, opts=' · '.join(f'{i}. {o}' for i, o in enumerate(q.options, 1)))
    return text


def where(session: Session, q: Question, code: str) -> str:
    from jarvis.runtime import knowledge169
    place = knowledge169.location_text(q.location, code) if q.location else ''
    return t('source', code, file=session.label, where=f' ({place})' if place else '')


def summary(session: Session, code: str) -> str:
    counts = {k: sum(q.status == k for q in session.questions) for k in ('matched', 'close', 'wrong', 'skipped', 'unverifiable')}
    seen = sum(q.status != 'new' for q in session.questions)
    text = t('summary', code, file=session.label, n=seen, m=counts['matched'], c=counts['close'], w=counts['wrong'],
             s=counts['skipped'], u=counts['unverifiable'])
    if counts['skipped'] and not session.retry_used:
        text += t('retry_offer', code, s=counts['skipped'])
    return text


def advance(session: Session) -> None:
    if session.retry_queue:
        session.retry_queue.pop(0)
    else:
        session.position += 1
    session.state = 'asked' if session.current is not None else 'finished'


def mask(answer: str) -> str:
    if len(answer) <= 2:
        return answer[0] + '…'
    keep = max(1, len(answer) // 3)
    return answer[:keep] + '·' * (len(answer) - keep - 1) + answer[-1]
