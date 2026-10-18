"""NEO 188 NEO-DOC-003: conversation side of the aligned .docx translation (see doc_translate188).

``respond`` returns ``(text, waiting)`` for inputs that belong to the document
translation job, or None for ordinary routing. Control words are in the
selected interface language; the translation language is a separate parameter.
"""
from __future__ import annotations

import ntpath
import os
import re
from pathlib import Path
from typing import Any

from jarvis.runtime import doc_translate188 as dt

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_DOC = {'ru': r'(?:документ|файл)\s+', 'en': r'(?:the\s+)?(?:document|file)\s+', 'de': r'(?:das\s+dokument|die\s+datei)\s+',
        'fr': r'(?:le\s+document|le\s+fichier)\s+', 'es': r'(?:el\s+documento|el\s+archivo)\s+', 'it': r'(?:il\s+documento|il\s+file)\s+',
        'pt': r'(?:o\s+documento|o\s+ficheiro|o\s+arquivo)\s+'}
_START = {'ru': r'переведи\s+(?P<doc>{d})?(?P<file>.+?)\s+на\s+(?P<lang>\S+)(?:\s+язык)?',
          'en': r'translate\s+(?P<doc>{d})?(?P<file>.+?)\s+(?:in)?to\s+(?P<lang>\S+)',
          'de': r'übersetze\s+(?P<doc>{d})?(?P<file>.+?)\s+(?:ins|auf|in\s+die)\s+(?P<lang>\S+)',
          'fr': r'traduis\s+(?P<doc>{d})?(?P<file>.+?)\s+en\s+(?P<lang>\S+)',
          'es': r'traduce\s+(?P<doc>{d})?(?P<file>.+?)\s+(?:al|a)\s+(?P<lang>\S+)',
          'it': r'traduci\s+(?P<doc>{d})?(?P<file>.+?)\s+in\s+(?P<lang>\S+)',
          'pt': r'traduz\s+(?P<doc>{d})?(?P<file>.+?)\s+para\s+(?:o\s+)?(?P<lang>\S+)'}
_TAIL = r'(?:[,;\s]+(?:и\s+|and\s+|und\s+|et\s+|y\s+|e\s+)?(?:сохрани|save|speichere|sauvegarde|enregistre|guarda|salva)\s+(?:\S+\s+)?(?:отдельную\s+)?(?:копию|copy|kopie|copie|copia|cópia)(?:\s+\S+)?)?'
_N = r'(?P<n>\d{1,4})'
_FRAG = {'ru': r'(?:фрагмент[аеу]?|абзац[аеу]?|часть|части)', 'en': r'(?:fragment|paragraph|part)', 'de': r'(?:fragment|absatz|teil)',
         'fr': r'(?:fragment|paragraphe|partie)', 'es': r'(?:fragmento|párrafo|parte)', 'it': r'(?:frammento|paragrafo|parte)', 'pt': r'(?:fragmento|parágrafo|parte)'}
_CMD = {
    'show': {'ru': r'покажи\s+(?:оригинал\s+)?{f}\s+' + _N, 'en': r'show\s+(?:me\s+)?(?:the\s+original\s+of\s+)?{f}\s+' + _N,
             'de': r'zeig(?:e)?\s+(?:mir\s+)?(?:das\s+original\s+von\s+)?{f}\s+' + _N, 'fr': r'montre(?:[- ]moi)?\s+(?:l[’\']original\s+du\s+)?(?:le\s+)?{f}\s+' + _N,
             'es': r'muéstrame\s+(?:el\s+original\s+del\s+)?(?:el\s+)?{f}\s+' + _N, 'it': r'mostrami\s+(?:l[’\']originale\s+del\s+)?(?:il\s+)?{f}\s+' + _N,
             'pt': r'mostra(?:-me)?\s+(?:o\s+original\s+do\s+)?(?:o\s+)?{f}\s+' + _N},
    'fix': {'ru': r'исправь\s+(?:перевод\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)', 'en': r'(?:fix|correct)\s+(?:the\s+translation\s+of\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)',
            'de': r'korrigiere\s+(?:die\s+übersetzung\s+von\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)', 'fr': r'corrige\s+(?:la\s+traduction\s+du\s+)?(?:le\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)',
            'es': r'corrige\s+(?:la\s+traducción\s+del\s+)?(?:el\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)', 'it': r'correggi\s+(?:la\s+traduzione\s+del\s+)?(?:il\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)',
            'pt': r'corrige\s+(?:a\s+tradução\s+do\s+)?(?:o\s+)?{f}\s+' + _N + r'\s*[:—-]\s*(?P<text>.+)'},
    'retry': {'ru': r'повтори\s+(?:перевод\s+)?{f}\s+' + _N + r'|переведи\s+(?:заново|ещё\s+раз)\s+{f}\s+' + _N.replace('n>', 'm>'),
              'en': r'(?:retranslate|retry)\s+{f}\s+' + _N, 'de': r'übersetze\s+{f}\s+' + _N + r'\s+neu', 'fr': r'retraduis\s+(?:le\s+)?{f}\s+' + _N,
              'es': r'vuelve\s+a\s+traducir\s+(?:el\s+)?{f}\s+' + _N, 'it': r'ritraduci\s+(?:il\s+)?{f}\s+' + _N, 'pt': r'volta\s+a\s+traduzir\s+(?:o\s+)?{f}\s+' + _N},
    'skip': {'ru': r'пропусти\s+{f}\s+' + _N + r'(?:\s+и\s+оставь\s+исходный\s+текст)?', 'en': r'skip\s+{f}\s+' + _N + r'(?:\s+and\s+keep\s+the\s+original(?:\s+text)?)?',
             'de': r'überspringe?\s+{f}\s+' + _N, 'fr': r'passe\s+(?:le\s+)?{f}\s+' + _N, 'es': r'salta\s+(?:el\s+)?{f}\s+' + _N, 'it': r'salta\s+(?:il\s+)?{f}\s+' + _N,
             'pt': r'salta\s+(?:o\s+)?{f}\s+' + _N},
    'save': {'ru': r'сохрани\s+перевод', 'en': r'save\s+the\s+translation', 'de': r'speichere\s+die\s+übersetzung', 'fr': r'(?:sauvegarde|enregistre)\s+la\s+traduction',
             'es': r'guarda\s+la\s+traducción', 'it': r'salva\s+la\s+traduzione', 'pt': r'guarda\s+a\s+tradução'},
    'pause': {'ru': r'приостанови\s+перевод|пауза\s+перевода', 'en': r'pause\s+the\s+translation', 'de': r'pausiere\s+die\s+übersetzung',
              'fr': r'mets\s+la\s+traduction\s+en\s+pause|suspends\s+la\s+traduction', 'es': r'pausa\s+la\s+traducción', 'it': r'metti\s+in\s+pausa\s+la\s+traduzione|sospendi\s+la\s+traduzione',
              'pt': r'pausa\s+a\s+tradução'},
    'resume': {'ru': r'продолжи\s+перевод', 'en': r'(?:continue|resume)\s+the\s+translation', 'de': r'setze\s+die\s+übersetzung\s+fort|übersetze\s+weiter',
               'fr': r'(?:continue|reprends)\s+la\s+traduction', 'es': r'(?:continúa|reanuda)\s+la\s+traducción', 'it': r'(?:continua|riprendi)\s+la\s+traduzione',
               'pt': r'(?:continua|retoma)\s+a\s+tradução'},
    'cancel': {'ru': r'отмени\s+перевод', 'en': r'cancel\s+the\s+translation', 'de': r'brich\s+die\s+übersetzung\s+ab', 'fr': r'annule\s+la\s+traduction',
               'es': r'cancela\s+la\s+traducción', 'it': r'annulla\s+la\s+traduzione', 'pt': r'cancela\s+a\s+tradução'},
    'local': {'ru': r'переводи\s+только\s+на\s+этом\s+компьютере', 'en': r'translate\s+only\s+on\s+this\s+computer', 'de': r'übersetze\s+nur\s+auf\s+diesem\s+computer',
              'fr': r'traduis\s+seulement\s+sur\s+cet\s+ordinateur', 'es': r'traduce\s+solo\s+en\s+este\s+ordenador', 'it': r'traduci\s+solo\s+su\s+questo\s+computer',
              'pt': r'traduz\s+só\s+neste\s+computador'},
    'review': {'ru': r'покажи\s+перевод(?:\s+документа)?', 'en': r'show\s+(?:me\s+)?the\s+translation', 'de': r'zeig(?:e)?\s+(?:mir\s+)?die\s+übersetzung',
               'fr': r'montre(?:[- ]moi)?\s+la\s+traduction', 'es': r'muéstrame\s+la\s+traducción', 'it': r'mostrami\s+la\s+traduzione', 'pt': r'mostra(?:-me)?\s+a\s+tradução'},
}

_T = {
    'plan': ('📄 Перевод «{file}» → {lang}\nПуть: {path}\nМаршрут: только локальный движок на этом компьютере (в сеть ничего не отправляется)\nОбъём: {n} фрагментов, {chars} символов\nКопия будет сохранена рядом: «{copy}» — только по команде «сохрани перевод»; оригинал не меняется.',
             '📄 Translating “{file}” → {lang}\nPath: {path}\nRoute: local engine on this computer only (nothing is sent to the network)\nSize: {n} fragments, {chars} characters\nThe copy will be saved next to it as “{copy}” — only when you say “save the translation”; the original is not changed.',
             '📄 Übersetzung von „{file}“ → {lang}\nPfad: {path}\nWeg: nur die lokale Engine auf diesem Computer (nichts geht ins Netz)\nUmfang: {n} Fragmente, {chars} Zeichen\nDie Kopie wird daneben als „{copy}“ gespeichert – nur auf „speichere die Übersetzung“; das Original bleibt unverändert.',
             '📄 Traduction de « {file} » → {lang}\nChemin : {path}\nItinéraire : moteur local de cet ordinateur uniquement (rien n’est envoyé sur le réseau)\nVolume : {n} fragments, {chars} caractères\nLa copie sera enregistrée à côté sous « {copy} » — seulement sur « sauvegarde la traduction » ; l’original ne change pas.',
             '📄 Traducción de «{file}» → {lang}\nRuta: {path}\nVía: solo el motor local de este equipo (no se envía nada a la red)\nVolumen: {n} fragmentos, {chars} caracteres\nLa copia se guardará al lado como «{copy}», solo con «guarda la traducción»; el original no cambia.',
             '📄 Traduzione di «{file}» → {lang}\nPercorso: {path}\nVia: solo il motore locale di questo computer (nulla va in rete)\nVolume: {n} frammenti, {chars} caratteri\nLa copia sarà salvata accanto come «{copy}», solo con «salva la traduzione»; l’originale non cambia.',
             '📄 Tradução de «{file}» → {lang}\nCaminho: {path}\nVia: só o motor local deste computador (nada é enviado para a rede)\nVolume: {n} fragmentos, {chars} caracteres\nA cópia será guardada ao lado como «{copy}», só com «guarda a tradução»; o original não muda.'),
    'unsupported': ('⚠️ Не переводится и остаётся как в оригинале: {items}', '⚠️ Not translated, kept as in the original: {items}',
                    '⚠️ Nicht übersetzt, bleibt wie im Original: {items}', '⚠️ Non traduit, conservé tel quel : {items}', '⚠️ No se traduce y queda como en el original: {items}',
                    '⚠️ Non tradotto, resta come nell’originale: {items}', '⚠️ Não traduzido, fica como no original: {items}'),
    'external': ('🔗 Внешних ссылок: {n} — они не открываются и не загружаются.', '🔗 External links: {n} — they are not opened or downloaded.',
                 '🔗 Externe Verknüpfungen: {n} – sie werden weder geöffnet noch geladen.', '🔗 Liens externes : {n} — ils ne sont ni ouverts ni téléchargés.',
                 '🔗 Enlaces externos: {n}; no se abren ni se descargan.', '🔗 Collegamenti esterni: {n}: non vengono aperti né scaricati.', '🔗 Ligações externas: {n} — não são abertas nem descarregadas.'),
    'progress': ('⏳ Переведено {d} из {n} фрагментов. Скажите «продолжи перевод», «приостанови перевод» или «отмени перевод».',
                 '⏳ Translated {d} of {n} fragments. Say “continue the translation”, “pause the translation” or “cancel the translation”.',
                 '⏳ {d} von {n} Fragmenten übersetzt. Sag „setze die Übersetzung fort“, „pausiere die Übersetzung“ oder „brich die Übersetzung ab“.',
                 '⏳ {d} fragments traduits sur {n}. Dites « continue la traduction », « mets la traduction en pause » ou « annule la traduction ».',
                 '⏳ Traducidos {d} de {n} fragmentos. Di «continúa la traducción», «pausa la traducción» o «cancela la traducción».',
                 '⏳ Tradotti {d} di {n} frammenti. Di’ «continua la traduzione», «metti in pausa la traduzione» o «annulla la traduzione».',
                 '⏳ Traduzidos {d} de {n} fragmentos. Diga «continua a tradução», «pausa a tradução» ou «cancela a tradução».'),
    'review': ('✅ Перевод готов к проверке: готово — {done}, исправлено вами — {edited}, пропущено — {skipped}, ошибок — {error}, не поддерживается — {unsup}, проверить числа/ссылки — {flag}. Это машинный перевод, не проверенный специалистом.\nКоманды: «покажи фрагмент 3», «исправь фрагмент 3: текст», «повтори фрагмент 3», «пропусти фрагмент 3», «сохрани перевод».',
               '✅ Translation ready for review: done — {done}, edited by you — {edited}, skipped — {skipped}, errors — {error}, unsupported — {unsup}, check numbers/links — {flag}. This is a machine translation, not reviewed by a professional.\nSay “show fragment 3”, “fix fragment 3: text”, “retranslate fragment 3”, “skip fragment 3”, “save the translation”.',
               '✅ Übersetzung zur Prüfung bereit: fertig — {done}, von dir korrigiert — {edited}, übersprungen — {skipped}, Fehler — {error}, nicht unterstützt — {unsup}, Zahlen/Links prüfen — {flag}. Maschinelle Übersetzung, nicht fachlich geprüft.\nBefehle: „zeig Fragment 3“, „korrigiere Fragment 3: Text“, „übersetze Fragment 3 neu“, „überspringe Fragment 3“, „speichere die Übersetzung“.',
               '✅ Traduction prête à relire : terminé — {done}, corrigé par vous — {edited}, passé — {skipped}, erreurs — {error}, non pris en charge — {unsup}, nombres/liens à vérifier — {flag}. Traduction automatique, non relue par un professionnel.\nDites « montre le fragment 3 », « corrige le fragment 3 : texte », « retraduis le fragment 3 », « passe le fragment 3 », « sauvegarde la traduction ».',
               '✅ Traducción lista para revisar: hecho — {done}, corregido por ti — {edited}, saltado — {skipped}, errores — {error}, no compatible — {unsup}, revisar números/enlaces — {flag}. Es traducción automática, no revisada por un profesional.\nDi «muéstrame el fragmento 3», «corrige el fragmento 3: texto», «vuelve a traducir el fragmento 3», «salta el fragmento 3», «guarda la traducción».',
               '✅ Traduzione pronta da rivedere: fatto — {done}, corretto da te — {edited}, saltato — {skipped}, errori — {error}, non supportato — {unsup}, controllare numeri/link — {flag}. Traduzione automatica, non rivista da un professionista.\nDi’ «mostrami il frammento 3», «correggi il frammento 3: testo», «ritraduci il frammento 3», «salta il frammento 3», «salva la traduzione».',
               '✅ Tradução pronta para rever: feito — {done}, corrigido por si — {edited}, saltado — {skipped}, erros — {error}, não suportado — {unsup}, verificar números/ligações — {flag}. É tradução automática, não revista por um profissional.\nDiga «mostra o fragmento 3», «corrige o fragmento 3: texto», «volta a traduzir o fragmento 3», «salta o fragmento 3», «guarda a tradução».'),
    'fragment': ('[{i}] {where} · {status}{flag}\nОригинал: {src}\nПеревод: {dst}', '[{i}] {where} · {status}{flag}\nOriginal: {src}\nTranslation: {dst}',
                 '[{i}] {where} · {status}{flag}\nOriginal: {src}\nÜbersetzung: {dst}', '[{i}] {where} · {status}{flag}\nOriginal : {src}\nTraduction : {dst}',
                 '[{i}] {where} · {status}{flag}\nOriginal: {src}\nTraducción: {dst}', '[{i}] {where} · {status}{flag}\nOriginale: {src}\nTraduzione: {dst}',
                 '[{i}] {where} · {status}{flag}\nOriginal: {src}\nTradução: {dst}'),
    'status': ({'new': 'ещё не переведено', 'done': 'готово', 'skipped': 'пропущено (оставлен оригинал)', 'error': 'ошибка', 'edited': 'исправлено вами', 'unsupported': 'не поддерживается (оставлен оригинал)'},
               {'new': 'not translated yet', 'done': 'done', 'skipped': 'skipped (original kept)', 'error': 'error', 'edited': 'edited by you', 'unsupported': 'unsupported (original kept)'},
               {'new': 'noch nicht übersetzt', 'done': 'fertig', 'skipped': 'übersprungen (Original bleibt)', 'error': 'Fehler', 'edited': 'von dir korrigiert', 'unsupported': 'nicht unterstützt (Original bleibt)'},
               {'new': 'pas encore traduit', 'done': 'terminé', 'skipped': 'passé (original conservé)', 'error': 'erreur', 'edited': 'corrigé par vous', 'unsupported': 'non pris en charge (original conservé)'},
               {'new': 'aún sin traducir', 'done': 'hecho', 'skipped': 'saltado (queda el original)', 'error': 'error', 'edited': 'corregido por ti', 'unsupported': 'no compatible (queda el original)'},
               {'new': 'non ancora tradotto', 'done': 'fatto', 'skipped': 'saltato (resta l’originale)', 'error': 'errore', 'edited': 'corretto da te', 'unsupported': 'non supportato (resta l’originale)'},
               {'new': 'ainda não traduzido', 'done': 'feito', 'skipped': 'saltado (fica o original)', 'error': 'erro', 'edited': 'corrigido por si', 'unsupported': 'não suportado (fica o original)'}),
    'flag': (' · ⚠ проверьте числа/ссылки', ' · ⚠ check numbers/links', ' · ⚠ Zahlen/Links prüfen', ' · ⚠ vérifiez nombres/liens', ' · ⚠ revisa números/enlaces',
             ' · ⚠ controlla numeri/link', ' · ⚠ verifique números/ligações'),
    'no_fragment': ('Фрагмента {i} нет: их {n}.', 'There is no fragment {i}: there are {n}.', 'Es gibt kein Fragment {i}: es sind {n}.', 'Il n’y a pas de fragment {i} : il y en a {n}.',
                    'No hay fragmento {i}: hay {n}.', 'Non c’è il frammento {i}: sono {n}.', 'Não há fragmento {i}: são {n}.'),
    'not_editable': ('Фрагмент {i} не поддерживается для перевода и останется как в оригинале.', 'Fragment {i} is not supported for translation and stays as in the original.',
                     'Fragment {i} wird für die Übersetzung nicht unterstützt und bleibt wie im Original.', 'Le fragment {i} n’est pas pris en charge et reste tel quel.',
                     'El fragmento {i} no es compatible y queda como en el original.', 'Il frammento {i} non è supportato e resta come nell’originale.', 'O fragmento {i} não é suportado e fica como no original.'),
    'saved': ('💾 Сохранил отдельную копию: {path}{partial}\nОригинал не изменён. Вёрстка (переносы, число страниц) могла измениться — проверьте в Word.',
              '💾 Saved a separate copy: {path}{partial}\nThe original is unchanged. Layout (line breaks, page count) may have changed — check it in Word.',
              '💾 Eine separate Kopie gespeichert: {path}{partial}\nDas Original ist unverändert. Das Layout (Umbrüche, Seitenzahl) kann sich geändert haben – prüfe es in Word.',
              '💾 Copie séparée enregistrée : {path}{partial}\nL’original n’a pas changé. La mise en page (retours à la ligne, nombre de pages) a pu changer — vérifiez dans Word.',
              '💾 Guardé una copia aparte: {path}{partial}\nEl original no cambió. El diseño (saltos, número de páginas) puede haber cambiado: revísalo en Word.',
              '💾 Salvata una copia separata: {path}{partial}\nL’originale non è cambiato. L’impaginazione (a capo, numero di pagine) può essere cambiata: controlla in Word.',
              '💾 Guardei uma cópia separada: {path}{partial}\nO original não mudou. O layout (quebras, número de páginas) pode ter mudado — verifique no Word.'),
    'partial': ('\nЭто частичный перевод: {n} фрагмент(ов) остались на исходном языке.', '\nThis is a partial translation: {n} fragment(s) stayed in the source language.',
                '\nDas ist eine Teilübersetzung: {n} Fragment(e) blieben in der Ausgangssprache.', '\nC’est une traduction partielle : {n} fragment(s) restent dans la langue source.',
                '\nEs una traducción parcial: {n} fragmento(s) quedaron en el idioma original.', '\nÈ una traduzione parziale: {n} frammento/i sono rimasti nella lingua di origine.',
                '\nÉ uma tradução parcial: {n} fragmento(s) ficaram na língua de origem.'),
    'not_ready': ('Перевод ещё не закончен ({d} из {n}). Скажите «продолжи перевод» или пропустите оставшиеся фрагменты.', 'The translation is not finished yet ({d} of {n}). Say “continue the translation” or skip the remaining fragments.',
                  'Die Übersetzung ist noch nicht fertig ({d} von {n}). Sag „setze die Übersetzung fort“ oder überspringe die übrigen Fragmente.', 'La traduction n’est pas terminée ({d} sur {n}). Dites « continue la traduction » ou passez les fragments restants.',
                  'La traducción aún no termina ({d} de {n}). Di «continúa la traducción» o salta los fragmentos restantes.', 'La traduzione non è finita ({d} di {n}). Di’ «continua la traduzione» o salta i frammenti rimanenti.',
                  'A tradução ainda não terminou ({d} de {n}). Diga «continua a tradução» ou salte os fragmentos restantes.'),
    'paused': ('⏸ Перевод приостановлен; новые фрагменты не переводятся. «Продолжи перевод» — продолжить.', '⏸ Translation paused; no new fragments are translated. Say “continue the translation” to go on.',
               '⏸ Übersetzung pausiert; es werden keine neuen Fragmente übersetzt. „Setze die Übersetzung fort“ zum Weitermachen.', '⏸ Traduction en pause ; aucun nouveau fragment n’est traduit. « Continue la traduction » pour reprendre.',
               '⏸ Traducción en pausa; no se traducen fragmentos nuevos. «Continúa la traducción» para seguir.', '⏸ Traduzione in pausa; nessun nuovo frammento viene tradotto. «Continua la traduzione» per proseguire.',
               '⏸ Tradução em pausa; não se traduzem novos fragmentos. «Continua a tradução» para continuar.'),
    'cancelled': ('Перевод отменён; файл не создавался, оригинал не тронут.', 'Translation cancelled; no file was created and the original is untouched.',
                  'Übersetzung abgebrochen; keine Datei erstellt, Original unberührt.', 'Traduction annulée ; aucun fichier créé, l’original n’est pas touché.',
                  'Traducción cancelada; no se creó ningún archivo y el original no se tocó.', 'Traduzione annullata; nessun file creato, originale intatto.',
                  'Tradução cancelada; nenhum ficheiro criado e o original não foi tocado.'),
    'local': ('Да: документы перевожу только локальным движком на этом компьютере. Внешний перевод документов не используется.', 'Yes: I translate documents only with the local engine on this computer. External document translation is not used.',
              'Ja: Dokumente übersetze ich nur mit der lokalen Engine auf diesem Computer. Externe Dokumentübersetzung wird nicht verwendet.', 'Oui : je traduis les documents uniquement avec le moteur local de cet ordinateur. Aucune traduction externe des documents.',
              'Sí: traduzco documentos solo con el motor local de este equipo. No se usa traducción externa de documentos.', 'Sì: traduco i documenti solo con il motore locale di questo computer. Nessuna traduzione esterna dei documenti.',
              'Sim: traduzo documentos só com o motor local deste computador. Não se usa tradução externa de documentos.'),
    'fixed': ('✏️ Фрагмент {i} исправлен вами; соседние фрагменты не изменены.', '✏️ Fragment {i} edited by you; neighbouring fragments are unchanged.',
              '✏️ Fragment {i} von dir korrigiert; benachbarte Fragmente bleiben unverändert.', '✏️ Fragment {i} corrigé par vous ; les fragments voisins ne changent pas.',
              '✏️ Fragmento {i} corregido por ti; los fragmentos vecinos no cambian.', '✏️ Frammento {i} corretto da te; i frammenti vicini non cambiano.',
              '✏️ Fragmento {i} corrigido por si; os fragmentos vizinhos não mudam.'),
    'skipped': ('⏭ Фрагмент {i} пропущен — в копии останется исходный текст.', '⏭ Fragment {i} skipped — the copy keeps the original text.',
                '⏭ Fragment {i} übersprungen – die Kopie behält den Originaltext.', '⏭ Fragment {i} passé — la copie garde le texte original.',
                '⏭ Fragmento {i} saltado: la copia conserva el texto original.', '⏭ Frammento {i} saltato: la copia mantiene il testo originale.',
                '⏭ Fragmento {i} saltado — a cópia mantém o texto original.'),
    'errors': {'damaged': ('Файл повреждён, зашифрован или это не обычный .docx — ничего не делаю.', 'The file is damaged, encrypted or not a plain .docx — I won’t do anything.',
                           'Die Datei ist beschädigt, verschlüsselt oder kein normales .docx – ich tue nichts.', 'Le fichier est endommagé, chiffré ou n’est pas un .docx simple — je ne fais rien.',
                           'El archivo está dañado, cifrado o no es un .docx normal: no hago nada.', 'Il file è danneggiato, cifrato o non è un .docx semplice: non faccio nulla.',
                           'O ficheiro está danificado, encriptado ou não é um .docx simples — não faço nada.'),
               'macros': ('В документе есть макросы — такие файлы не перевожу и не открываю.', 'The document contains macros — I don’t translate or open such files.',
                          'Das Dokument enthält Makros – solche Dateien übersetze und öffne ich nicht.', 'Le document contient des macros — je ne traduis ni n’ouvre ces fichiers.',
                          'El documento tiene macros: no traduzco ni abro esos archivos.', 'Il documento contiene macro: non traduco né apro questi file.', 'O documento tem macros — não traduzo nem abro esses ficheiros.'),
               'not_docx': ('Пока перевожу только обычные .docx (абзацы, заголовки, простые таблицы).', 'For now I translate only plain .docx files (paragraphs, headings, simple tables).',
                            'Vorerst übersetze ich nur einfache .docx-Dateien (Absätze, Überschriften, einfache Tabellen).', 'Pour l’instant je ne traduis que des .docx simples (paragraphes, titres, tableaux simples).',
                            'Por ahora solo traduzco .docx normales (párrafos, títulos, tablas simples).', 'Per ora traduco solo .docx semplici (paragrafi, titoli, tabelle semplici).',
                            'Por agora só traduzo .docx simples (parágrafos, títulos, tabelas simples).'),
               'too_large': ('Файл слишком большой или подозрительно сжат — не распаковываю.', 'The file is too large or suspiciously compressed — I won’t unpack it.',
                             'Die Datei ist zu groß oder verdächtig komprimiert – ich entpacke sie nicht.', 'Le fichier est trop gros ou compressé de façon suspecte — je ne le décompresse pas.',
                             'El archivo es demasiado grande o está comprimido de forma sospechosa: no lo descomprimo.', 'Il file è troppo grande o compresso in modo sospetto: non lo estraggo.',
                             'O ficheiro é demasiado grande ou está comprimido de forma suspeita — não o descompacto.'),
               'limit': ('В документе {fragments} фрагментов и {chars} символов — больше лимита (1000 фрагментов, 100 000 символов). Ничего не обрезаю: выберите документ поменьше.',
                         'The document has {fragments} fragments and {chars} characters — over the limit (1000 fragments, 100,000 characters). I don’t cut anything: choose a smaller document.',
                         'Das Dokument hat {fragments} Fragmente und {chars} Zeichen – über dem Limit (1000 Fragmente, 100.000 Zeichen). Ich schneide nichts ab: wähle ein kleineres Dokument.',
                         'Le document a {fragments} fragments et {chars} caractères — au-delà de la limite (1000 fragments, 100 000 caractères). Je ne coupe rien : choisissez un document plus petit.',
                         'El documento tiene {fragments} fragmentos y {chars} caracteres: supera el límite (1000 fragmentos, 100 000 caracteres). No recorto nada: elige un documento más pequeño.',
                         'Il documento ha {fragments} frammenti e {chars} caratteri: oltre il limite (1000 frammenti, 100.000 caratteri). Non taglio nulla: scegli un documento più piccolo.',
                         'O documento tem {fragments} fragmentos e {chars} caracteres — acima do limite (1000 fragmentos, 100 000 caracteres). Não corto nada: escolha um documento mais pequeno.'),
               'no_model': ('Локальная модель перевода не установлена или недоступна — документ никуда не отправляю. Установите её в Настройки → Перевод.',
                            'The local translation model is not installed or not available — I don’t send the document anywhere. Install it in Settings → Translation.',
                            'Das lokale Übersetzungsmodell ist nicht installiert oder nicht erreichbar – ich sende das Dokument nirgendwohin. Installiere es unter Einstellungen → Übersetzung.',
                            'Le modèle de traduction local n’est pas installé ou indisponible — je n’envoie le document nulle part. Installez-le dans Réglages → Traduction.',
                            'El modelo de traducción local no está instalado o no está disponible: no envío el documento a ninguna parte. Instálalo en Ajustes → Traducción.',
                            'Il modello di traduzione locale non è installato o non è disponibile: non invio il documento da nessuna parte. Installalo in Impostazioni → Traduzione.',
                            'O modelo de tradução local não está instalado ou disponível — não envio o documento para lado nenhum. Instale-o em Definições → Tradução.'),
               'not_local': ('Адрес движка перевода больше не локальный — останавливаюсь и ничего не отправляю.', 'The translation engine address is no longer local — I stop and send nothing.',
                             'Die Adresse der Übersetzungs-Engine ist nicht mehr lokal – ich halte an und sende nichts.', 'L’adresse du moteur n’est plus locale — je m’arrête et n’envoie rien.',
                             'La dirección del motor ya no es local: me detengo y no envío nada.', 'L’indirizzo del motore non è più locale: mi fermo e non invio nulla.',
                             'O endereço do motor já não é local — paro e não envio nada.'),
               'missing': ('Исходный файл больше недоступен — копию не сохраняю.', 'The source file is no longer available — I won’t save the copy.',
                           'Die Quelldatei ist nicht mehr verfügbar – ich speichere keine Kopie.', 'Le fichier source n’est plus disponible — je n’enregistre pas la copie.',
                           'El archivo de origen ya no está disponible: no guardo la copia.', 'Il file di origine non è più disponibile: non salvo la copia.', 'O ficheiro de origem já não está disponível — não guardo a cópia.'),
               'changed': ('Исходный документ изменился после чтения — перевод относится к прежней версии, копию не сохраняю. Начните перевод заново.',
                           'The source document changed after it was read — the translation belongs to the old version, so I won’t save the copy. Start the translation again.',
                           'Das Quelldokument hat sich nach dem Lesen geändert – die Übersetzung gehört zur alten Version, daher speichere ich keine Kopie. Starte die Übersetzung neu.',
                           'Le document source a changé après lecture — la traduction concerne l’ancienne version, je n’enregistre pas la copie. Relancez la traduction.',
                           'El documento de origen cambió tras leerlo: la traducción es de la versión anterior y no guardo la copia. Empieza de nuevo.',
                           'Il documento di origine è cambiato dopo la lettura: la traduzione riguarda la versione precedente, non salvo la copia. Ricomincia.',
                           'O documento de origem mudou depois de lido — a tradução é da versão anterior, não guardo a cópia. Recomece.'),
               'write_unsafe': ('Не могу безопасно записать перевод в эту структуру документа — копию не создаю, оригинал не тронут.', 'I can’t safely write the translation into this document structure — no copy created, the original is untouched.',
                                'Ich kann die Übersetzung nicht sicher in diese Dokumentstruktur schreiben – keine Kopie, Original unberührt.', 'Je ne peux pas écrire la traduction en toute sécurité dans cette structure — aucune copie, original intact.',
                                'No puedo escribir la traducción de forma segura en esta estructura: no creo copia y el original no se tocó.', 'Non posso scrivere la traduzione in modo sicuro in questa struttura: nessuna copia, originale intatto.',
                                'Não consigo escrever a tradução com segurança nesta estrutura — sem cópia, original intacto.'),
               'write_failed': ('Не удалось создать новый файл — ничего не перезаписано.', 'Could not create the new file — nothing was overwritten.', 'Die neue Datei konnte nicht erstellt werden – nichts überschrieben.',
                                'Impossible de créer le nouveau fichier — rien n’a été écrasé.', 'No se pudo crear el archivo nuevo: no se sobrescribió nada.', 'Impossibile creare il nuovo file: nulla è stato sovrascritto.',
                                'Não foi possível criar o novo ficheiro — nada foi substituído.')},
    'name_file188': ('Выбранный документ NEO не видит. Назовите файл: «переведи документ отчёт.docx на английский» — перевод сохранится отдельной копией, оригинал фрагмента можно посмотреть рядом.',
                     'NEO cannot see a selected document. Name the file: “translate the document report.docx into English” — the translation is saved as a separate copy and each fragment can be compared with its original.',
                     'NEO sieht kein ausgewähltes Dokument. Nenne die Datei: „übersetze das Dokument bericht.docx ins Englische“ — die Übersetzung wird als Kopie gespeichert, jedes Fragment lässt sich mit dem Original vergleichen.',
                     'NEO ne voit pas de document sélectionné. Nomme le fichier : « traduis le document rapport.docx en anglais » — la traduction est enregistrée dans une copie et chaque fragment peut être comparé à l’original.',
                     'NEO no ve ningún documento seleccionado. Di el archivo: «traduce el documento informe.docx al inglés»; la traducción se guarda como copia y cada fragmento se puede comparar con el original.',
                     'NEO non vede un documento selezionato. Indica il file: «traduci il documento rapporto.docx in inglese» — la traduzione viene salvata in una copia e ogni frammento si può confrontare con l’originale.',
                     'O NEO não vê um documento selecionado. Diz o ficheiro: «traduz o documento relatorio.docx para inglês» — a tradução fica numa cópia e cada fragmento pode ser comparado com o original.'),
    'not_found': ('Не нашёл документ «{name}». Назовите его точнее или укажите полный путь.', 'I didn’t find the document “{name}”. Name it more precisely or give the full path.',
                  'Ich habe das Dokument „{name}“ nicht gefunden. Nenne es genauer oder gib den vollständigen Pfad an.', 'Je n’ai pas trouvé le document « {name} ». Précisez son nom ou le chemin complet.',
                  'No encontré el documento «{name}». Nómbralo con más precisión o da la ruta completa.', 'Non ho trovato il documento «{name}». Indica il nome esatto o il percorso completo.',
                  'Não encontrei o documento «{name}». Diga o nome exato ou o caminho completo.'),
    'ambiguous': ('Под «{name}» подходит несколько документов — назовите полный путь:\n{choices}', 'Several documents match “{name}” — say the full path:\n{choices}',
                  'Mehrere Dokumente passen zu „{name}“ – nenne den vollständigen Pfad:\n{choices}', 'Plusieurs documents correspondent à « {name} » — indiquez le chemin complet :\n{choices}',
                  'Varios documentos coinciden con «{name}»: di la ruta completa:\n{choices}', 'Più documenti corrispondono a «{name}»: indica il percorso completo:\n{choices}',
                  'Vários documentos correspondem a «{name}» — diga o caminho completo:\n{choices}'),
    'no_job': ('Сейчас нет перевода документа.', 'There is no document translation right now.', 'Gerade läuft keine Dokumentübersetzung.', 'Aucune traduction de document en cours.',
               'Ahora no hay traducción de documento.', 'Non c’è alcuna traduzione di documento in corso.', 'Não há tradução de documento agora.'),
}


def t(key: str, code: str, **values: Any) -> str:
    row = _T[key]
    return row[_CODES.index(code) if code in _CODES else 1].format(**values)


def error_text(error: dt.DocError, code: str) -> str:
    row = _T['errors'].get(error.key) or _T['errors']['damaged']
    return row[_CODES.index(code) if code in _CODES else 1].format(**error.values)


def _clean(text: str) -> str:
    return ' '.join(str(text or '').split()).strip(' .!?¿¡…')


def command(text: str, code: str) -> tuple[str, dict] | None:
    value = _clean(text)
    for name, rows in _CMD.items():
        pattern = rows.get(code)
        if not pattern:
            continue
        match = re.fullmatch(pattern.replace('{f}', _FRAG[code]), value, re.I)
        if match:
            data = {k: v for k, v in match.groupdict().items() if v is not None}
            if 'm' in data:
                data['n'] = data.pop('m')
            return name, data
    return None


# The copy is already reviewed fragment by fragment next to its original («покажи оригинал фрагмента N»).
_ALIGN188 = (r'(?:[,;\s]+(?:и\s+|and\s+|und\s+|et\s+|y\s+|e\s+)?(?:покажи\s+рядом\s+оригинал|покажи\s+оригинал\s+рядом|alongside\s+the\s+original|'
             r'zeige\s+das\s+original\s+daneben|affiche\s+l[’\']original\s+à\s+côté|muestra\s+el\s+original\s+al\s+lado|'
             r'mostra\s+accanto\s+l[’\']originale|mostra\s+o\s+original\s+ao\s+lado))?')
_SELECTED188 = (r'(?:выбранный|выделенный|этот|текущий)\s+(?:документ|файл)|(?:the\s+)?(?:selected|current)\s+(?:document|file)|this\s+(?:document|file)|'
                r'das\s+ausgewählte\s+dokument|ausgewählte\s+dokument|le\s+document\s+sélectionné|document\s+sélectionné|'
                r'el\s+documento\s+seleccionado|documento\s+seleccionado|il\s+documento\s+selezionato|documento\s+selezionato|'
                r'o\s+documento\s+selecionado|documento\s+selecionado|'
                r'sélectionné|selectionne|seleccionado|selezionato|selecionado|ausgewählte|selected')


def parse_start(text: str, code: str) -> dict | None:
    """«переведи документ отчёт.docx на английский, сохрани отдельную копию»."""
    from jarvis.integrations.local_translation import _language_code
    value = _clean(text)
    pattern = _START.get(code)
    if pattern is None:
        return None
    match = re.fullmatch(pattern.replace('{d}', _DOC[code]) + _TAIL + _ALIGN188, value, re.I)
    if not match:
        return None
    name = match.group('file').strip(' "«»“”')
    if re.fullmatch(_SELECTED188, name, re.I):
        # NEO 188 atlas (proposed.document.translate_aligned): there is no «selected document»
        # NEO can see; ask for its name instead of translating the words of the command.
        target = _language_code(match.group('lang').strip(' ,.'))
        return {'file': '', 'target': target, 'selected': True} if target else None
    if not match.group('doc') and not name.casefold().endswith('.docx'):
        return None                       # «переведи привет на английский» stays a phrase translation
    target = _language_code(match.group('lang').strip(' ,.'))
    if not target:
        return None
    return {'file': name, 'target': target}


def find_documents(name: str) -> list[str]:
    from jarvis.runtime import office178
    direct = Path(os.path.expandvars(os.path.expanduser(name)))
    if direct.is_absolute():
        return [str(direct)] if direct.is_file() else []
    wanted = name.casefold().replace('ё', 'е')
    found: list[str] = []
    for folder in office178.user_folders():
        try:
            items = list(folder.glob('*')) + list(folder.glob('*/*'))
        except OSError:
            continue
        for path in items[:6000]:
            low = path.name.casefold().replace('ё', 'е')
            if path.suffix.casefold() == '.docx' and (low == wanted or path.stem.casefold().replace('ё', 'е') == wanted):
                found.append(str(path))
    return sorted(set(found))


def _jobs(agent: Any) -> dict:
    store = getattr(agent, '_doc_jobs188', None)
    if store is None:
        store = agent._doc_jobs188 = {}
    return store


def _translator(agent: Any, job: dt.Job):
    factory = getattr(agent, '_doc_translator188', None)       # tests inject an inert translator
    if callable(factory):
        return factory(job.source, job.target)
    from jarvis.ollama_runtime import is_local_ollama_url
    from jarvis.runtime import translation184
    if not is_local_ollama_url(translation184._base_url(agent.config)) or not translation184.installed(agent.config, fresh=True):
        raise dt.DocError('no_model')
    return dt.local_translator(agent.config, job.source, job.target)


def _language_name(target: str, code: str) -> str:
    names = {'ru': ('русский', 'Russian', 'Russisch', 'russe', 'ruso', 'russo', 'russo'), 'en': ('английский', 'English', 'Englisch', 'anglais', 'inglés', 'inglese', 'inglês'),
             'de': ('немецкий', 'German', 'Deutsch', 'allemand', 'alemán', 'tedesco', 'alemão'), 'fr': ('французский', 'French', 'Französisch', 'français', 'francés', 'francese', 'francês'),
             'es': ('испанский', 'Spanish', 'Spanisch', 'espagnol', 'español', 'spagnolo', 'espanhol'), 'it': ('итальянский', 'Italian', 'Italienisch', 'italien', 'italiano', 'italiano', 'italiano'),
             'pt': ('португальский', 'Portuguese', 'Portugiesisch', 'portugais', 'portugués', 'portoghese', 'português')}
    row = names.get(target)
    return row[_CODES.index(code) if code in _CODES else 1] if row else target


def _progress(job: dt.Job, code: str) -> tuple[str, bool]:
    if job.state == 'review':
        return review(job, code), False
    done, total = _counts(job)
    return t('progress', code, d=done, n=total), False


def _counts(job: dt.Job) -> tuple[int, int]:
    """Processed / translatable fragments (unsupported ones are listed separately, never counted as translated)."""
    translatable = [f for f in job.inventory.fragments if f.status != 'unsupported']
    return sum(f.status != 'new' for f in translatable), len(translatable)


def review(job: dt.Job, code: str) -> str:
    frags = job.inventory.fragments
    count = lambda s: sum(f.status == s for f in frags)
    return t('review', code, done=count('done'), edited=count('edited'), skipped=count('skipped'), error=count('error'),
             unsup=count('unsupported'), flag=sum(bool(f.flag) for f in frags))


def show(job: dt.Job, number: int, code: str) -> str:
    frags = job.inventory.fragments
    if not 1 <= number <= len(frags):
        return t('no_fragment', code, i=number, n=len(frags))
    f = frags[number - 1]
    status = _T['status'][_CODES.index(code) if code in _CODES else 1][f.status]
    return t('fragment', code, i=number, where=f.position, status=status, flag=t('flag', code) if f.flag else '',
             src=f.text, dst=f.translation or '—')


def respond(agent: Any, text: str, code: str) -> tuple[str, bool] | None:
    scope = str(getattr(agent, '_conversation_key', 'desktop') or 'desktop')
    jobs = _jobs(agent)
    request = parse_start(text, code)
    if request is not None and request.get('selected'):
        return t('name_file188', code), False
    if request is not None:
        found = find_documents(request['file'])
        if not found:
            return t('not_found', code, name=request['file']), False
        if len(found) > 1:
            return t('ambiguous', code, name=request['file'], choices='\n'.join(f'{i}. {p}' for i, p in enumerate(found[:8], 1))), False
        path = found[0]
        try:
            inventory = dt.inspect(path, Path(path).read_bytes())
        except dt.DocError as error:
            return error_text(error, code), False
        except OSError:
            return error_text(dt.DocError('missing'), code), False
        from jarvis.runtime import translation184
        sample = ' '.join(f.text for f in inventory.fragments[:5])
        job = dt.Job(inventory, translation184.detect_source(sample, code), request['target'])
        lines = [t('plan', code, file=job.label, lang=_language_name(job.target, code), path=path, n=len(inventory.fragments),
                   chars=inventory.chars, copy=dt.copy_name(Path(path), job.target).name)]
        if inventory.unsupported:
            items = ', '.join(sorted({f'{k} ({w})' for k, w in inventory.unsupported}))[:600]
            lines.append(t('unsupported', code, items=items))
        if inventory.external_links:
            lines.append(t('external', code, n=inventory.external_links))
        try:
            translate = _translator(agent, job)
        except dt.DocError as error:
            return '\n'.join(lines + [error_text(error, code)]), False
        jobs[scope] = job
        try:
            dt.run_batch(job, translate)
        except dt.DocError as error:
            job.state = 'paused'
            return '\n'.join(lines + [error_text(error, code)]), False
        return '\n'.join(lines) + '\n\n' + _progress(job, code)[0], False
    job = jobs.get(scope)
    parsed = command(text, code)
    if parsed is None:
        return None
    name, data = parsed
    if name == 'local':
        return t('local', code), False
    if job is None or job.state in {'cancelled', 'saved'}:
        return (t('no_job', code), False) if name in {'save', 'pause', 'resume', 'cancel', 'review'} else None
    if name == 'cancel':
        job.state = 'cancelled'
        jobs.pop(scope, None)
        return t('cancelled', code), False
    if name == 'pause':
        if job.state == 'translating':
            job.state = 'paused'
        return t('paused', code), False
    if name == 'resume':
        if job.state == 'paused':
            job.state = 'translating'
        if job.state == 'translating':
            try:
                dt.run_batch(job, _translator(agent, job))
            except dt.DocError as error:
                job.state = 'paused'
                return error_text(error, code), False
        return _progress(job, code)
    if name == 'review':
        lines = [review(job, code) if job.state == 'review' else _progress(job, code)[0]]
        lines += [show(job, i, code) for i in range(1, min(len(job.inventory.fragments), 5) + 1)]
        return '\n\n'.join(lines), False
    number = int(data.get('n', 0) or 0)
    frags = job.inventory.fragments
    if name in {'show', 'fix', 'retry', 'skip'} and not 1 <= number <= len(frags):
        return t('no_fragment', code, i=number, n=len(frags)), False
    if name == 'show':
        return show(job, number, code), False
    fragment = frags[number - 1] if number else None
    if name in {'fix', 'retry'} and fragment is not None and fragment.status == 'unsupported':
        return t('not_editable', code, i=number), False
    if name == 'fix':
        fragment.translation, fragment.status = str(data.get('text', '')).strip(), 'edited'
        fragment.flag = dt.check_flags(fragment.text, fragment.translation)
        return t('fixed', code, i=number) + '\n' + show(job, number, code), False
    if name == 'retry':
        try:
            dt.translate_fragment(fragment, _translator(agent, job))
        except dt.DocError as error:
            return error_text(error, code), False
        if not job.pending() and job.state == 'translating':
            job.state = 'review'
        return show(job, number, code), False
    if name == 'skip':
        if fragment.status != 'unsupported':
            fragment.status, fragment.translation = 'skipped', ''
        if not job.pending() and job.state == 'translating':
            job.state = 'review'
        return t('skipped', code, i=number), False
    if name == 'save':
        if job.pending():
            done, total = _counts(job)
            return t('not_ready', code, d=done, n=total), False
        try:
            path = dt.save(job)
        except dt.DocError as error:
            return error_text(error, code), False
        untranslated = sum(f.status in {'skipped', 'error', 'unsupported'} for f in frags)
        partial = t('partial', code, n=untranslated) if untranslated else ''
        jobs.pop(scope, None)
        return t('saved', code, path=str(path), partial=partial), False
    return None
