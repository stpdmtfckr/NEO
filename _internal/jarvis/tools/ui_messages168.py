"""Replies of the NEO 168 tools (ui, browser_nav, copy) in the seven NEO languages."""
from __future__ import annotations

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')

MESSAGES: dict[str, tuple[str, ...]] = {}


def add(key: str, *values: str) -> None:
    assert len(values) == 7, key
    MESSAGES[key] = values


def msg(name: str, code: str, /, **values) -> str:
    row = MESSAGES[name]
    return row[_CODES.index(code) if code in _CODES else 1].format(**values)


# ---- typing and keys
add('typed', 'Напечатал: «{text}».', 'Typed: “{text}”.', 'Getippt: „{text}“.', 'Saisi : « {text} ».',
    'Escrito: «{text}».', 'Digitato: «{text}».', 'Escrito: «{text}».')
add('pressed', 'Нажал {key}.', 'Pressed {key}.', '{key} gedrückt.', '{key} appuyée.', 'Pulsado {key}.', 'Premuto {key}.', 'Premido {key}.')
add('pressed_times', 'Нажал {key} ({count} раз).', 'Pressed {key} ({count} times).', '{key} gedrückt ({count}-mal).',
    '{key} appuyée ({count} fois).', 'Pulsado {key} ({count} veces).', 'Premuto {key} ({count} volte).', 'Premido {key} ({count} vezes).')
add('empty_text', 'Не услышал, что напечатать.', 'I did not catch what to type.', 'Ich habe nicht verstanden, was ich tippen soll.',
    'Je n’ai pas compris quoi saisir.', 'No entendí qué escribir.', 'Non ho capito cosa digitare.', 'Não percebi o que escrever.')


# ---- numbered elements, clicks
add('hints_shown', 'Пронумеровал элементы: {count}. Скажите «нажми» и номер.', 'Numbered {count} elements. Say “click” and a number.',
    '{count} Elemente nummeriert. Sagen Sie „klick“ und eine Nummer.', '{count} éléments numérotés. Dites « clique » et un numéro.',
    'He numerado {count} elementos. Di «pulsa» y un número.', 'Ho numerato {count} elementi. Di’ «clicca» e un numero.',
    'Numerei {count} elementos. Diga «clica» e um número.')
add('hints_none', 'В активном окне нет элементов, которые можно пронумеровать.', 'The active window has no elements to number.',
    'Das aktive Fenster hat keine Elemente zum Nummerieren.', 'La fenêtre active n’a aucun élément à numéroter.',
    'La ventana activa no tiene elementos para numerar.', 'La finestra attiva non ha elementi da numerare.',
    'A janela ativa não tem elementos para numerar.')
add('hints_off', 'Номера убраны.', 'Numbers removed.', 'Nummern entfernt.', 'Numéros retirés.', 'Números quitados.', 'Numeri rimossi.', 'Números removidos.')
add('hints_off_refused', 'Не удалось передать окну NEO команду убрать номера. Попробуйте ещё раз.', 'Could not pass the “remove numbers” command to the NEO window. Please try again.', 'Der Befehl zum Entfernen der Nummern konnte nicht an das NEO-Fenster übergeben werden. Versuchen Sie es erneut.', 'Impossible de transmettre à la fenêtre NEO la commande de retirer les numéros. Réessayez.', 'No se pudo pasar a la ventana de NEO la orden de quitar los números. Inténtalo de nuevo.', 'Impossibile passare alla finestra NEO il comando di rimuovere i numeri. Riprova.', 'Não foi possível passar à janela do NEO o comando de remover os números. Tente novamente.')
add('clicked', 'Нажал «{name}».', 'Clicked “{name}”.', '„{name}“ angeklickt.', 'Cliqué sur « {name} ».', 'Pulsado «{name}».', 'Cliccato «{name}».', 'Clicado «{name}».')
add('no_hints', 'Сначала скажите «покажи номера».', 'Say “show numbers” first.', 'Sagen Sie zuerst „zeige Nummern“.',
    'Dites d’abord « affiche les numéros ».', 'Primero di «muestra los números».', 'Prima di’ «mostra i numeri».', 'Primeiro diga «mostra os números».')
add('hint_missing', 'Элемента с номером {number} нет, всего их {count}.', 'There is no element {number}; there are {count}.',
    'Element {number} gibt es nicht; es gibt {count}.', 'Il n’y a pas d’élément {number} ; il y en a {count}.',
    'No hay elemento {number}; hay {count}.', 'Non c’è l’elemento {number}; ce ne sono {count}.', 'Não há elemento {number}; há {count}.')
add('window_changed', 'Окно сменилось. Скажите «покажи номера» ещё раз.', 'The window changed. Say “show numbers” again.',
    'Das Fenster hat sich geändert. Sagen Sie erneut „zeige Nummern“.', 'La fenêtre a changé. Redites « affiche les numéros ».',
    'La ventana cambió. Di otra vez «muestra los números».', 'La finestra è cambiata. Di’ di nuovo «mostra i numeri».',
    'A janela mudou. Diga outra vez «mostra os números».')
add('element_moved', 'Элемент «{name}» уже не на месте. Ничего не нажато.', 'The element “{name}” has moved. Nothing was clicked.',
    'Das Element „{name}“ hat sich bewegt. Nichts angeklickt.', 'L’élément « {name} » a bougé. Rien n’a été cliqué.',
    'El elemento «{name}» se ha movido. No se pulsó nada.', 'L’elemento «{name}» si è spostato. Nessun clic.', 'O elemento «{name}» mudou de lugar. Nada foi clicado.')
add('no_link', 'Не нашёл ссылку номер {index}: на странице их {count}.', 'Link {index} not found: the page has {count}.',
    'Link {index} nicht gefunden: die Seite hat {count}.', 'Lien {index} introuvable : la page en a {count}.',
    'No encontré el enlace {index}: la página tiene {count}.', 'Link {index} non trovato: la pagina ne ha {count}.',
    'Não encontrei o link {index}: a página tem {count}.')
add('no_button', 'Не нашёл кнопку номер {index}: в окне их {count}.', 'Button {index} not found: the window has {count}.',
    'Schaltfläche {index} nicht gefunden: das Fenster hat {count}.', 'Bouton {index} introuvable : la fenêtre en a {count}.',
    'No encontré el botón {index}: la ventana tiene {count}.', 'Pulsante {index} non trovato: la finestra ne ha {count}.',
    'Não encontrei o botão {index}: a janela tem {count}.')
add('no_item', 'Не нашёл пункт номер {index}: в списке их {count}.', 'Item {index} not found: the list has {count}.',
    'Eintrag {index} nicht gefunden: die Liste hat {count}.', 'Élément {index} introuvable : la liste en a {count}.',
    'No encontré el elemento {index}: la lista tiene {count}.', 'Voce {index} non trovata: l’elenco ne ha {count}.',
    'Não encontrei o item {index}: a lista tem {count}.')
add('bad_index', 'Не понял номер: «{value}».', 'I did not understand the number “{value}”.', 'Die Nummer „{value}“ habe ich nicht verstanden.',
    'Je n’ai pas compris le numéro « {value} ».', 'No entendí el número «{value}».', 'Non ho capito il numero «{value}».', 'Não percebi o número «{value}».')

# ---- scrolling
add('scroll_down', 'Прокрутил вниз.', 'Scrolled down.', 'Nach unten gescrollt.', 'Défilé vers le bas.', 'Desplazado hacia abajo.', 'Scorrimento in basso.', 'Deslizei para baixo.')
add('scroll_up', 'Прокрутил вверх.', 'Scrolled up.', 'Nach oben gescrollt.', 'Défilé vers le haut.', 'Desplazado hacia arriba.', 'Scorrimento in alto.', 'Deslizei para cima.')
add('scroll_left', 'Прокрутил влево.', 'Scrolled left.', 'Nach links gescrollt.', 'Défilé vers la gauche.', 'Desplazado a la izquierda.', 'Scorrimento a sinistra.', 'Deslizei para a esquerda.')
add('scroll_right', 'Прокрутил вправо.', 'Scrolled right.', 'Nach rechts gescrollt.', 'Défilé vers la droite.', 'Desplazado a la derecha.', 'Scorrimento a destra.', 'Deslizei para a direita.')
add('scroll_top', 'Перешёл в самое начало.', 'Went to the very top.', 'Ganz nach oben gesprungen.', 'Retour tout en haut.', 'Ido al principio.', 'Tornato all’inizio.', 'Fui para o início.')
add('scroll_bottom', 'Перешёл в самый конец.', 'Went to the very bottom.', 'Ganz nach unten gesprungen.', 'Allé tout en bas.', 'Ido al final.', 'Andato alla fine.', 'Fui para o fim.')

# ---- browser navigation
add('no_browser', 'Не вижу открытого браузера.', 'I do not see an open browser.', 'Ich sehe keinen geöffneten Browser.',
    'Je ne vois aucun navigateur ouvert.', 'No veo ningún navegador abierto.', 'Non vedo un browser aperto.', 'Não vejo nenhum navegador aberto.')
add('nav_back', 'Назад.', 'Back.', 'Zurück.', 'Retour.', 'Atrás.', 'Indietro.', 'Voltar.')
add('nav_forward', 'Вперёд.', 'Forward.', 'Vorwärts.', 'Suivant.', 'Adelante.', 'Avanti.', 'Avançar.')
add('nav_reload', 'Обновил страницу.', 'Page reloaded.', 'Seite neu geladen.', 'Page actualisée.', 'Página recargada.', 'Pagina ricaricata.', 'Página atualizada.')
add('nav_stop', 'Остановил загрузку.', 'Loading stopped.', 'Laden gestoppt.', 'Chargement arrêté.', 'Carga detenida.', 'Caricamento interrotto.', 'Carregamento parado.')
add('nav_next_tab', 'Следующая вкладка.', 'Next tab.', 'Nächster Tab.', 'Onglet suivant.', 'Pestaña siguiente.', 'Scheda successiva.', 'Separador seguinte.')
add('nav_previous_tab', 'Предыдущая вкладка.', 'Previous tab.', 'Vorheriger Tab.', 'Onglet précédent.', 'Pestaña anterior.', 'Scheda precedente.', 'Separador anterior.')
add('nav_find', 'Ищу на странице: «{text}».', 'Finding on the page: “{text}”.', 'Suche auf der Seite: „{text}“.', 'Recherche dans la page : « {text} ».',
    'Buscando en la página: «{text}».', 'Cerco nella pagina: «{text}».', 'A procurar na página: «{text}».')
add('nav_find_next', 'Следующее совпадение.', 'Next match.', 'Nächster Treffer.', 'Occurrence suivante.', 'Siguiente coincidencia.', 'Occorrenza successiva.', 'Ocorrência seguinte.')
add('nav_zoom_in', 'Увеличил масштаб.', 'Zoomed in.', 'Vergrößert.', 'Zoom avant.', 'Zoom aumentado.', 'Zoom aumentato.', 'Zoom aumentado.')
add('nav_zoom_out', 'Уменьшил масштаб.', 'Zoomed out.', 'Verkleinert.', 'Zoom arrière.', 'Zoom reducido.', 'Zoom ridotto.', 'Zoom reduzido.')
add('nav_zoom_reset', 'Масштаб 100%.', 'Zoom reset to 100%.', 'Zoom auf 100 %.', 'Zoom à 100 %.', 'Zoom al 100 %.', 'Zoom al 100%.', 'Zoom a 100%.')
add('nav_address', 'Адресная строка выбрана.', 'Address bar selected.', 'Adressleiste ausgewählt.', 'Barre d’adresse sélectionnée.',
    'Barra de direcciones seleccionada.', 'Barra degli indirizzi selezionata.', 'Barra de endereço selecionada.')
add('nav_open_url', 'Открываю {url}.', 'Opening {url}.', 'Öffne {url}.', 'J’ouvre {url}.', 'Abriendo {url}.', 'Apro {url}.', 'A abrir {url}.')
add('nav_next_field', 'Следующее поле.', 'Next field.', 'Nächstes Feld.', 'Champ suivant.', 'Campo siguiente.', 'Campo successivo.', 'Campo seguinte.')
add('nav_previous_field', 'Предыдущее поле.', 'Previous field.', 'Vorheriges Feld.', 'Champ précédent.', 'Campo anterior.', 'Campo precedente.', 'Campo anterior.')

# ---- copy
add('copied', 'Скопировал {what}.', 'Copied {what}.', '{what} kopiert.', 'Copié : {what}.', 'Copiado: {what}.', 'Copiato: {what}.', 'Copiado: {what}.')
add('nothing_selected', 'Ничего не выделено. Выделите текст или скажите, что скопировать.', 'Nothing is selected. Select text or say what to copy.',
    'Nichts markiert. Markieren Sie Text oder sagen Sie, was kopiert werden soll.', 'Rien n’est sélectionné. Sélectionnez du texte ou dites quoi copier.',
    'No hay nada seleccionado. Selecciona texto o di qué copiar.', 'Niente di selezionato. Seleziona del testo o di’ cosa copiare.',
    'Nada selecionado. Selecione texto ou diga o que copiar.')
add('no_reply', 'В этом разговоре я ещё ничего не писал.', 'I have not written anything in this conversation yet.',
    'In diesem Gespräch habe ich noch nichts geschrieben.', 'Je n’ai encore rien écrit dans cette conversation.',
    'Aún no he escrito nada en esta conversación.', 'Non ho ancora scritto nulla in questa conversazione.', 'Ainda não escrevi nada nesta conversa.')
add('no_file', 'Не нашёл файл «{name}».', 'File “{name}” not found.', 'Datei „{name}“ nicht gefunden.', 'Fichier « {name} » introuvable.',
    'No encontré el archivo «{name}».', 'File «{name}» non trovato.', 'Não encontrei o ficheiro «{name}».')
add('no_selection_file', 'Выделите файл или папку в Проводнике или назовите их.', 'Select a file or folder in File Explorer or say its name.',
    'Markieren Sie eine Datei oder einen Ordner im Explorer oder nennen Sie den Namen.', 'Sélectionnez un fichier ou dossier dans l’Explorateur ou dites son nom.',
    'Selecciona un archivo o carpeta en el Explorador o di su nombre.', 'Seleziona un file o una cartella in Esplora file o di’ il nome.',
    'Selecione um ficheiro ou pasta no Explorador ou diga o nome.')
add('not_text', 'Файл «{name}» не текстовый, поэтому скопировал сам файл.', '“{name}” is not a text file, so I copied the file itself.',
    '„{name}“ ist keine Textdatei, daher habe ich die Datei selbst kopiert.', '« {name} » n’est pas un fichier texte : j’ai copié le fichier lui-même.',
    '«{name}» no es un archivo de texto; he copiado el archivo.', '«{name}» non è un file di testo: ho copiato il file.',
    '«{name}» não é um ficheiro de texto; copiei o próprio ficheiro.')
add('no_screenshot', 'Скриншотов ещё нет.', 'There are no screenshots yet.', 'Es gibt noch keine Screenshots.', 'Aucune capture pour l’instant.',
    'Aún no hay capturas.', 'Non ci sono ancora screenshot.', 'Ainda não há capturas.')
add('no_url', 'Не вижу адреса страницы.', 'I do not see the page address.', 'Ich sehe die Seitenadresse nicht.', 'Je ne vois pas l’adresse de la page.',
    'No veo la dirección de la página.', 'Non vedo l’indirizzo della pagina.', 'Não vejo o endereço da página.')

# ---- paste
add('pasted', 'Вставил {what}{where}.', 'Pasted {what}{where}.', '{what}{where} eingefügt.', 'Collé : {what}{where}.',
    'Pegado: {what}{where}.', 'Incollato: {what}{where}.', 'Colado: {what}{where}.')
add('where_window', ' в «{name}»', ' into “{name}”', ' in „{name}“', ' dans « {name} »', ' en «{name}»', ' in «{name}»', ' em «{name}»')
add('where_field', ' в поле «{name}»', ' into the field “{name}”', ' in das Feld „{name}“', ' dans le champ « {name} »', ' en el campo «{name}»',
    ' nel campo «{name}»', ' no campo «{name}»')
add('where_line', ' в строку {line}', ' into line {line}', ' in Zeile {line}', ' à la ligne {line}', ' en la línea {line}', ' alla riga {line}', ' na linha {line}')
add('where_end', ' в конец', ' at the end', ' am Ende', ' à la fin', ' al final', ' alla fine', ' no fim')
add('clip_empty', 'Буфер обмена пуст — сначала скажите «скопируй …».', 'The clipboard is empty — say “copy …” first.',
    'Die Zwischenablage ist leer — sagen Sie zuerst „kopiere …“.', 'Le presse-papiers est vide — dites d’abord « copie … ».',
    'El portapapeles está vacío: primero di «copia …».', 'Gli appunti sono vuoti: prima di’ «copia …».', 'A área de transferência está vazia — diga primeiro «copia …».')
add('where_paste', 'Сейчас активно окно NEO. Куда вставить? Например: «вставь в {behind}», «вставь в поле Поиск», «вставь в строку 3», «вставь в папку Документы».',
    'The NEO window is active. Where should I paste? For example: “paste into {behind}”, “paste into the Search field”, “paste into line 3”, “paste into the Documents folder”.',
    'Das NEO-Fenster ist aktiv. Wohin einfügen? Zum Beispiel: „füge in {behind} ein“, „füge in das Feld Suche ein“, „füge in Zeile 3 ein“, „füge in den Ordner Dokumente ein“.',
    'La fenêtre de NEO est active. Où coller ? Par exemple : « colle dans {behind} », « colle dans le champ Recherche », « colle à la ligne 3 », « colle dans le dossier Documents ».',
    'La ventana de NEO está activa. ¿Dónde pego? Por ejemplo: «pega en {behind}», «pega en el campo Buscar», «pega en la línea 3», «pega en la carpeta Documentos».',
    'La finestra di NEO è attiva. Dove incollo? Per esempio: «incolla in {behind}», «incolla nel campo Cerca», «incolla alla riga 3», «incolla nella cartella Documenti».',
    'A janela do NEO está ativa. Onde colo? Por exemplo: «cola em {behind}», «cola no campo Pesquisar», «cola na linha 3», «cola na pasta Documentos».')
add('no_window', 'Не нашёл окно «{name}».', 'Window “{name}” not found.', 'Fenster „{name}“ nicht gefunden.', 'Fenêtre « {name} » introuvable.',
    'No encontré la ventana «{name}».', 'Finestra «{name}» non trovata.', 'Não encontrei a janela «{name}».')
add('no_field', 'Не нашёл поле «{name}».', 'Field “{name}” not found.', 'Feld „{name}“ nicht gefunden.', 'Champ « {name} » introuvable.',
    'No encontré el campo «{name}».', 'Campo «{name}» non trovato.', 'Não encontrei o campo «{name}».')
add('saved', 'Сохранил {what} в папку {folder}: {files}.', 'Saved {what} to the folder {folder}: {files}.', '{what} im Ordner {folder} gespeichert: {files}.',
    '{what} enregistré dans le dossier {folder} : {files}.', '{what} guardado en la carpeta {folder}: {files}.', '{what} salvato nella cartella {folder}: {files}.',
    '{what} guardado na pasta {folder}: {files}.')
add('appended', 'Дописал {what} в файл {name}.', 'Appended {what} to the file {name}.', '{what} an die Datei {name} angehängt.',
    '{what} ajouté au fichier {name}.', '{what} añadido al archivo {name}.', '{what} aggiunto al file {name}.', '{what} acrescentado ao ficheiro {name}.')
add('clip_is', 'В буфере обмена {what}.', 'The clipboard holds {what}.', 'In der Zwischenablage: {what}.', 'Le presse-papiers contient {what}.',
    'En el portapapeles hay {what}.', 'Negli appunti c’è {what}.', 'Na área de transferência: {what}.')
add('need_text', 'В буфере не текст, в файл можно дописать только текст.', 'The clipboard does not hold text; only text can be appended to a file.',
    'Die Zwischenablage enthält keinen Text; nur Text kann angehängt werden.', 'Le presse-papiers ne contient pas de texte ; seul du texte peut être ajouté.',
    'El portapapeles no tiene texto; solo se puede añadir texto.', 'Gli appunti non contengono testo; si può aggiungere solo testo.',
    'A área de transferência não tem texto; só é possível acrescentar texto.')
add('nothing_to_repeat', 'Пока нечего повторять: я ещё ничего не нажимал и не печатал.', 'Nothing to repeat yet: I have not clicked or typed anything.',
    'Noch nichts zu wiederholen: ich habe nichts angeklickt oder getippt.', 'Rien à répéter : je n’ai encore rien cliqué ni saisi.',
    'Nada que repetir: todavía no he pulsado ni escrito nada.', 'Niente da ripetere: non ho ancora cliccato né digitato nulla.',
    'Nada para repetir: ainda não cliquei nem escrevi nada.')
# ---- NEO 169: target window and buttons by name
add('no_target', 'Не вижу окна, с которым работать: сейчас открыт только NEO. Переключитесь на нужное окно и повторите.',
    'There is no window to act on: only NEO is open. Switch to the window you need and repeat.',
    'Kein Fenster zum Bedienen: nur NEO ist offen. Wechseln Sie zum gewünschten Fenster und wiederholen Sie.',
    'Aucune fenêtre sur laquelle agir : seul NEO est ouvert. Passez à la fenêtre voulue et répétez.',
    'No hay ninguna ventana sobre la que actuar: solo está NEO. Cambia a la ventana que necesitas y repite.',
    'Nessuna finestra su cui agire: è aperto solo NEO. Passa alla finestra che ti serve e ripeti.',
    'Não há janela onde agir: só o NEO está aberto. Mude para a janela pretendida e repita.')
add('no_element', 'В окне «{window}» не нашёл кнопку «{name}». Скажите «покажи номера», чтобы увидеть, что можно нажать.',
    'No “{name}” button in “{window}”. Say “show numbers” to see what can be clicked.',
    'Keine Schaltfläche „{name}“ in „{window}“. Sagen Sie „zeige Nummern“.',
    'Pas de bouton « {name} » dans « {window} ». Dites « affiche les numéros ».',
    'No hay botón «{name}» en «{window}». Di «muestra los números».',
    'Nessun pulsante «{name}» in «{window}». Di’ «mostra i numeri».',
    'Não há botão «{name}» em «{window}». Diga «mostra os números».')
add('which_element', 'В окне «{window}» подходят несколько элементов: {options}. Какой нажать? Можно сказать «покажи номера» и выбрать номер.',
    'Several elements in “{window}” match: {options}. Which one? You can say “show numbers” and pick a number.',
    'Mehrere Elemente in „{window}“ passen: {options}. Welches? Sagen Sie „zeige Nummern“.',
    'Plusieurs éléments de « {window} » correspondent : {options}. Lequel ? Dites « affiche les numéros ».',
    'Varios elementos de «{window}» coinciden: {options}. ¿Cuál? Di «muestra los números».',
    'Più elementi in «{window}» corrispondono: {options}. Quale? Di’ «mostra i numeri».',
    'Vários elementos em «{window}» correspondem: {options}. Qual? Diga «mostra os números».')
add('found_element', 'Нашёл «{name}» в окне «{window}» — отметил его номером 1. Скажите «нажми номер 1», чтобы нажать.',
    'Found “{name}” in “{window}” and marked it with number 1. Say “click number 1” to press it.',
    '„{name}“ in „{window}“ gefunden und mit Nummer 1 markiert. Sagen Sie „klicke auf Nummer 1“.',
    '« {name} » trouvé dans « {window} », marqué du numéro 1. Dites « clique sur le numéro 1 ».',
    'Encontré «{name}» en «{window}» y lo marqué con el número 1. Di «haz clic en el número 1».',
    'Trovato «{name}» in «{window}», segnato con il numero 1. Di’ «clicca il numero 1».',
    'Encontrei «{name}» em «{window}» e marquei-o com o número 1. Diga «clica no número 1».')
add('focused_field', 'Выбрал поле {index}: «{name}».', 'Selected field {index}: “{name}”.', 'Feld {index} gewählt: „{name}“.',
    'Champ {index} sélectionné : « {name} ».', 'Campo {index} seleccionado: «{name}».', 'Campo {index} selezionato: «{name}».',
    'Campo {index} selecionado: «{name}».')
add('no_field_numbered', 'В окне «{window}» нет поля номер {index} (полей: {count}).', 'There is no field number {index} in “{window}” ({count} fields).',
    'Kein Feld Nummer {index} in „{window}“ ({count} Felder).', 'Pas de champ numéro {index} dans « {window} » ({count} champs).',
    'No hay campo número {index} en «{window}» ({count} campos).', 'Nessun campo numero {index} in «{window}» ({count} campi).',
    'Não há campo número {index} em «{window}» ({count} campos).')

# NEO 187: holding a key for a time is not offered (one press or a few presses only).
add('hold_unsupported', 'Удерживать клавишу в течение времени я не умею — могу нажать её один или несколько раз.',
    'I can’t hold a key for a period of time — I can press it once or a few times.',
    'Eine Taste eine Zeit lang gedrückt halten kann ich nicht – ich kann sie einmal oder mehrmals drücken.',
    'Je ne peux pas maintenir une touche enfoncée pendant un certain temps — je peux l’appuyer une ou plusieurs fois.',
    'No puedo mantener una tecla pulsada durante un tiempo; puedo pulsarla una o varias veces.',
    'Non posso tenere premuto un tasto per un certo tempo: posso premerlo una o più volte.',
    'Não consigo manter uma tecla premida durante um tempo — posso premi-la uma ou várias vezes.')
