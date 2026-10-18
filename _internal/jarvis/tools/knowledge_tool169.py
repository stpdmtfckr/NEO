"""NEO 169 tool ``knowledge``: answers from the user's own files in the allowed folders."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.runtime import knowledge169
from jarvis.tools.registry import ToolExecution

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
    'no_folders': ('Я пока не ищу в ваших файлах. Скажите «разреши искать в папке Документы» — и я буду искать только там.',
                   'I do not search your files yet. Say “allow searching the folder Documents” and I will search only there.',
                   'Ich durchsuche noch keine Dateien. Sagen Sie „erlaube die Suche im Ordner Dokumente“.',
                   'Je ne cherche pas encore dans vos fichiers. Dites « autorise la recherche dans le dossier Documents ».',
                   'Aún no busco en tus archivos. Di «permite buscar en la carpeta Documentos».',
                   'Non cerco ancora nei tuoi file. Di’ «consenti la ricerca nella cartella Documenti».',
                   'Ainda não procuro nos seus ficheiros. Diga «permite procurar na pasta Documentos».'),
    'nothing': ('В разрешённых папках не нашёл ничего про «{query}».', 'Nothing about “{query}” in the allowed folders.',
                'In den erlaubten Ordnern steht nichts zu „{query}“.', 'Rien sur « {query} » dans les dossiers autorisés.',
                'No encontré nada sobre «{query}» en las carpetas permitidas.', 'Nulla su «{query}» nelle cartelle consentite.',
                'Nada sobre «{query}» nas pastas permitidas.'),
    'indexing': ('Индекс ещё строится — ответ может быть неполным.', 'The index is still being built; the answer may be incomplete.',
                 'Der Index wird noch erstellt; die Antwort kann unvollständig sein.', 'L’index est en cours de création ; la réponse peut être incomplète.',
                 'El índice aún se está creando; la respuesta puede estar incompleta.', 'L’indice è ancora in costruzione; la risposta può essere incompleta.',
                 'O índice ainda está a ser criado; a resposta pode estar incompleta.'),
    'found': ('Нашёл в ваших файлах:', 'Found in your files:', 'In Ihren Dateien gefunden:', 'Trouvé dans vos fichiers :',
              'Encontrado en tus archivos:', 'Trovato nei tuoi file:', 'Encontrado nos seus ficheiros:'),
    'sources': ('Источники:', 'Sources:', 'Quellen:', 'Sources :', 'Fuentes:', 'Fonti:', 'Fontes:'),
    'added': ('Теперь ищу в папке {folder}. Индексирую в фоне — это не мешает работе.', 'I now search the folder {folder}. Indexing runs in the background.',
              'Ich durchsuche jetzt den Ordner {folder}. Die Indizierung läuft im Hintergrund.', 'Je cherche maintenant dans le dossier {folder}. L’indexation se fait en arrière-plan.',
              'Ahora busco en la carpeta {folder}. El índice se crea en segundo plano.', 'Ora cerco nella cartella {folder}. L’indicizzazione avviene in background.',
              'Agora procuro na pasta {folder}. A indexação corre em segundo plano.'),
    'removed': ('Больше не ищу в папке {folder}; её данные удалены из индекса.', 'I no longer search {folder}; its data was removed from the index.',
                'Ich durchsuche {folder} nicht mehr; die Daten wurden aus dem Index entfernt.', 'Je ne cherche plus dans {folder} ; ses données ont été retirées.',
                'Ya no busco en {folder}; sus datos se quitaron del índice.', 'Non cerco più in {folder}; i dati sono stati rimossi.',
                'Já não procuro em {folder}; os dados foram removidos do índice.'),
    'no_folder': ('Не нашёл папку «{folder}».', 'Folder “{folder}” not found.', 'Ordner „{folder}“ nicht gefunden.', 'Dossier « {folder} » introuvable.',
                  'No encontré la carpeta «{folder}».', 'Cartella «{folder}» non trovata.', 'Pasta «{folder}» não encontrada.'),
    'folders': ('Ищу только в этих папках ({files} файлов в индексе):', 'I search only these folders ({files} files indexed):',
                'Ich suche nur in diesen Ordnern ({files} Dateien im Index):', 'Je cherche seulement dans ces dossiers ({files} fichiers indexés) :',
                'Solo busco en estas carpetas ({files} archivos indexados):', 'Cerco solo in queste cartelle ({files} file indicizzati):',
                'Procuro só nestas pastas ({files} ficheiros no índice):'),
    'reindex': ('Обновляю индекс в фоне: перечитаю только изменённые файлы.', 'Updating the index in the background: only changed files are read again.',
                'Index wird im Hintergrund aktualisiert: nur geänderte Dateien.', 'Mise à jour de l’index en arrière-plan : seuls les fichiers modifiés.',
                'Actualizo el índice en segundo plano: solo los archivos cambiados.', 'Aggiorno l’indice in background: solo i file modificati.',
                'Atualizo o índice em segundo plano: só os ficheiros alterados.'),
}


_LANGS188 = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_REVOKE188 = {
    'ambiguous': ('Разрешено несколько папок с именем «{folder}». Доступ к какой отозвать? Назови номер или полный путь:\n{choices}',
                  'Several allowed folders are named “{folder}”. Which one should lose access? Give its number or full path:\n{choices}',
                  'Mehrere erlaubte Ordner heißen „{folder}“. Für welchen soll der Zugriff entzogen werden? Nenne Nummer oder vollständigen Pfad:\n{choices}',
                  'Plusieurs dossiers autorisés s’appellent « {folder} ». Lequel retirer ? Indiquez son numéro ou son chemin complet :\n{choices}',
                  'Hay varias carpetas permitidas llamadas «{folder}». ¿A cuál le quito el acceso? Di su número o su ruta completa:\n{choices}',
                  'Più cartelle consentite si chiamano «{folder}». A quale tolgo l’accesso? Indica il numero o il percorso completo:\n{choices}',
                  'Há várias pastas permitidas chamadas «{folder}». A qual retiro o acesso? Diz o número ou o caminho completo:\n{choices}'),
    'missing': ('Доступ к папке {folder} уже отозван. Ничего не менял.', 'Access to {folder} was already revoked. Nothing changed.',
                'Der Zugriff auf {folder} war bereits entzogen. Nichts geändert.', 'L’accès à {folder} était déjà retiré. Rien n’a changé.',
                'El acceso a {folder} ya estaba retirado. No he cambiado nada.', 'L’accesso a {folder} era già stato revocato. Nessuna modifica.',
                'O acesso a {folder} já tinha sido retirado. Nada mudou.'),
    'save_failed': ('Не удалось сохранить отзыв доступа к папке {folder}: доступ остаётся прежним.',
                    'Could not save the revocation for {folder}: access is unchanged.',
                    'Der Entzug für {folder} konnte nicht gespeichert werden: Der Zugriff bleibt unverändert.',
                    'Impossible d’enregistrer le retrait de {folder} : l’accès reste inchangé.',
                    'No se pudo guardar la retirada de acceso a {folder}: el acceso sigue igual.',
                    'Impossibile salvare la revoca per {folder}: l’accesso resta invariato.',
                    'Não foi possível guardar a retirada de acesso a {folder}: o acesso mantém-se.'),
    'index_failed': ('Доступ к папке {folder} отозван, но её записи в индексе удалить полностью не удалось; при следующем обновлении индекса NEO повторит очистку.',
                     'Access to {folder} was revoked, but its index entries could not be fully removed; NEO will retry on the next index update.',
                     'Der Zugriff auf {folder} wurde entzogen, aber die Indexeinträge konnten nicht vollständig gelöscht werden; NEO versucht es beim nächsten Update erneut.',
                     'L’accès à {folder} est retiré, mais ses entrées d’index n’ont pas pu être toutes supprimées ; NEO réessaiera à la prochaine mise à jour.',
                     'Se retiró el acceso a {folder}, pero no se pudieron borrar del todo sus entradas del índice; NEO lo reintentará en la próxima actualización.',
                     'L’accesso a {folder} è revocato, ma le sue voci nell’indice non sono state eliminate del tutto; NEO riproverà al prossimo aggiornamento.',
                     'O acesso a {folder} foi retirado, mas as entradas no índice não foram todas apagadas; o NEO tenta de novo na próxima atualização.'),
}


def _t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def _code(config: Any) -> str:
    return str(config.get('app.language', 'ru') or 'ru')


def _resolve_folder(name: str) -> Path | None:
    value = str(name or '').strip().strip('«»"“”')
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path if path.is_dir() else None
    try:
        from jarvis.desktop_resolver import find_user_path
        found = find_user_path(value, kind='folder', max_seconds=3.0)
    except Exception:
        found = None
    return found if found is not None and Path(found).is_dir() else None


def ask(config: Any, ollama: Any, query: str) -> ToolExecution:
    code = _code(config)
    service = knowledge169.service(config)
    if not service.folders():
        return ToolExecution(False, _t('no_folders', code), {'requires_setup': True})
    sources = service.search(query, limit=int(config.get('knowledge.max_sources', 5) or 5))
    if not sources:
        text = _t('nothing', code, query=query)
        if service.busy():
            text += '\n' + _t('indexing', code)
        return ToolExecution(True, text, {'sources': []})
    from jarvis.runtime import local_chat169
    answer = local_chat169.answer(ollama, config, query, code, sources=sources)
    citations = knowledge169.cite(sources, code)
    if answer:
        text = f"{answer}\n\n{_t('sources', code)}\n{citations}"
    else:
        # No local model available: show the fragments themselves, each with its source.
        parts = [_t('found', code)]
        for number, source in enumerate(sources[:3], 1):
            snippet = ' '.join(source['text'].split())
            parts.append(f'[{number}] {snippet[:300]}' + ('…' if len(snippet) > 300 else ''))
        text = '\n'.join(parts) + f"\n\n{_t('sources', code)}\n{citations}"
    if service.busy():
        text += '\n' + _t('indexing', code)
    text += '\n' + _remember_sources188(config, sources, bool(answer), code)
    return ToolExecution(True, text, {'sources': [{'file': s['file'], 'location': s['location']} for s in sources]})


def run(config: Any, ollama: Any, args: dict[str, Any]) -> ToolExecution:
    code = _code(config)
    action = str(args.get('action', '') or '')
    service = knowledge169.service(config)
    if action == 'ask':
        return ask(config, ollama, str(args.get('query', '') or ''))
    if action == 'add_folder':
        folder = _resolve_folder(str(args.get('folder', '')))
        if folder is None:
            return ToolExecution(False, _t('no_folder', code, folder=args.get('folder', '')), {'requires_clarification': True})
        service.add_folder(folder)
        return ToolExecution(True, _t('added', code, folder=folder), {'folder': str(folder)})
    if action == 'remove_folder':
        # NEO 188 NEO-PRIVACY-001: revoke exactly one grant, named by its full path.
        name = str(args.get('folder', '') or '')
        matches = service.resolve_folders(name)
        if not matches:
            return ToolExecution(False, _t('no_folder', code, folder=name), {'outcome': 'missing', 'folder': name})
        if len(matches) > 1:
            choices = '\n'.join(f'{i}. {path}' for i, path in enumerate(matches[:8], 1))
            return ToolExecution(False, _REVOKE188['ambiguous'][_LANGS188.index(code) if code in _LANGS188 else 1].format(folder=name, choices=choices),
                                 {'outcome': 'ambiguous', 'requires_clarification': True, 'options': matches[:8]})
        path = matches[0]
        outcome = service.revoke(path)
        if outcome == 'removed':
            return ToolExecution(True, _t('removed', code, folder=path), {'outcome': 'removed', 'folder': path})
        text = _REVOKE188[outcome][_LANGS188.index(code) if code in _LANGS188 else 1].format(folder=path)
        return ToolExecution(outcome == 'index_failed', text, {'outcome': outcome, 'folder': path})
    if action in {'ask_file', 'compare'}:
        return _scoped188(config, ollama, service, args, code)
    if action in {'show_source', 'open_source', 'reveal_source'}:
        return _inspect188(config, service, args, code)
    if action == 'folders':
        folders = service.folders()
        if not folders:
            return ToolExecution(True, _t('no_folders', code), {'folders': []})
        stats = service.index.stats()
        return ToolExecution(True, _t('folders', code, files=stats['files']) + '\n' + '\n'.join('• ' + f for f in folders),
                             {'folders': folders, **stats})
    if action == 'reindex':
        service.refresh(force=True)
        return ToolExecution(True, _t('reindex', code), {})
    raise ValueError(f'unknown knowledge action: {action}')


def _scoped188(config: Any, ollama: Any, service: Any, args: dict[str, Any], code: str) -> ToolExecution:
    """NEO 188 NEO-DOC-001: answer from exactly one chosen file, or compare exactly two."""
    from jarvis.runtime import document_scope188 as scope
    index = service.index
    action = str(args.get('action', ''))
    names = [str(args.get('file', '') or '')] if action == 'ask_file' else [str(args.get('file_a', '') or ''), str(args.get('file_b', '') or '')]
    chosen: list[str] = []
    for name in names:
        found = scope.resolve(index, name)
        if not found:
            return ToolExecution(False, scope.t('unknown', code, name=name), {'outcome': 'unknown_file', 'requires_clarification': True})
        if len(found) > 1:
            choices = '\n'.join(f'{i}. {path}' for i, path in enumerate(found[:8], 1))
            return ToolExecution(False, scope.t('ambiguous', code, name=name, choices=choices),
                                 {'outcome': 'ambiguous', 'requires_clarification': True, 'options': found[:8]})
        chosen.append(found[0])
    labels = [scope.short_label(path, chosen) for path in chosen]
    query = str(args.get('query', '') or '').strip()
    notes = [scope.t('stale', code, a=label) for path, label in zip(chosen, labels) if scope.stale(index, path)]
    from jarvis.runtime import local_chat169
    if action == 'ask_file':
        sources = index.search(query, 5, files=chosen)
        head = scope.t('scope', code, a=labels[0])
        if not sources:
            return ToolExecution(True, '\n'.join([head, scope.t('nothing', code, a=labels[0])] + notes),
                                 {'outcome': 'not_found', 'files': chosen})
        answer = local_chat169.answer(ollama, config, query, code, sources=sources)
        body = answer or '\n'.join(f'[{n}] ' + ' '.join(s['text'].split())[:300] for n, s in enumerate(sources[:3], 1))
        text = '\n'.join([head, body, '', _t('sources', code), knowledge169.cite(sources, code)] + notes)
        text += '\n' + _remember_sources188(config, sources, bool(answer), code)
        return ToolExecution(True, text, {'outcome': 'answered', 'files': chosen,
                                          'sources': [{'file': s['file'], 'location': s['location']} for s in sources]})
    if chosen[0] == chosen[1]:
        return ToolExecution(False, scope.t('same', code, a=labels[0]), {'outcome': 'same_file', 'requires_clarification': True})
    sides = []
    for path in chosen:
        hits = index.search(query, 3, files=[path]) if query else index.first_chunks(path, 3)
        sides.append(hits)
    head = scope.t('scope_pair', code, a=labels[0], b=labels[1])
    for side, (hits, label) in enumerate(zip(sides, labels)):
        if not hits:
            return ToolExecution(True, '\n'.join([head, scope.t('missing_side', code, side='AB'[side], name=label)] + notes),
                                 {'outcome': 'missing_side', 'side': 'AB'[side], 'files': chosen})
    topic = scope._TOPIC[scope._ORDER.index(code) if code in scope._ORDER else 1].format(q=query) if query else ''
    question = scope.t('compare_question', code, topic=topic)
    labelled = [dict(hit, file=f"{'AB'[side]}: {hit['file']}") for side, hits in enumerate(sides) for hit in hits]
    answer = local_chat169.answer(ollama, config, question, code, sources=labelled)
    if answer:
        body = answer
    else:
        body = '\n'.join(f"{'AB'[side]} [{n}] " + ' '.join(hit['text'].split())[:240]
                          for side, hits in enumerate(sides) for n, hit in enumerate(hits[:2], 1))
    text = '\n'.join([head, body, '', _t('sources', code), knowledge169.cite(labelled, code)] + notes)
    text += '\n' + _remember_sources188(config, labelled, bool(answer), code)
    return ToolExecution(True, text, {'outcome': 'compared', 'files': chosen,
                                      'sources': [{'file': s['file'], 'location': s['location']} for s in labelled]})


def _scope_key188(config: Any) -> str:
    return str(getattr(getattr(config, 'paths', None), 'root', '') or 'desktop')


def _remember_sources188(config: Any, sources: list[dict[str, Any]], passed_to_model: bool, code: str) -> str:
    """NEO 188 NEO-DOC-002: keep the exact excerpts of THIS answer for «покажи источник N»."""
    from jarvis.runtime import source_inspect188 as inspect
    inspect.remember(_scope_key188(config), inspect.excerpts(sources, passed_to_model=passed_to_model))
    return inspect.t('hint', code)


def _inspect188(config: Any, service: Any, args: dict[str, Any], code: str) -> ToolExecution:
    from jarvis.runtime import source_inspect188 as inspect
    rows = inspect.recall(_scope_key188(config))
    if not rows:
        return ToolExecution(False, inspect.t('none', code), {'outcome': 'no_answer'})
    try:
        number = int(args.get('index', 0) or 0)
    except (TypeError, ValueError):
        number = 0
    row = next((r for r in rows if r['n'] == number), None)
    if row is None:
        return ToolExecution(False, inspect.t('bad_index', code, count=len(rows), n=number), {'outcome': 'bad_index'})
    current = inspect.state(row, service.folders())
    if current == 'revoked':
        return ToolExecution(False, inspect.t('revoked', code), {'outcome': 'revoked'})
    place = knowledge169.location_text(row['location'], code)
    where = f' ({place})' if place else ''
    shown = (row['side'] + ': ' if row.get('side') else '') + row['file']
    action = str(args.get('action', ''))
    if action == 'show_source':
        note = inspect.t({'ok': 'note_ok', 'changed': 'note_changed', 'missing': 'note_missing'}[current], code)
        key = 'view' if row.get('passed_to_model') else 'view_snippet'
        text = inspect.t(key, code, n=number, file=shown, where=where, excerpt=' '.join(row['excerpt'].split()), note=note)
        return ToolExecution(True, text, {'outcome': 'shown', 'state': current, 'file': row['file'], 'location': row['location']})
    if current == 'missing':
        return ToolExecution(False, inspect.t('note_missing', code), {'outcome': 'missing'})
    import os
    if action == 'open_source' and os.path.splitext(row['file'])[1].casefold() not in inspect.OPENABLE:
        return ToolExecution(False, inspect.t('unsupported', code, file=row['file']), {'outcome': 'unsupported'})
    try:
        if os.name != 'nt':
            raise OSError('opening documents is supported on Windows only')
        if action == 'open_source':
            os.startfile(row['file'])          # the exact original; no shell string is built
            return ToolExecution(True, inspect.t('opened', code, file=row['file'], where=where), {'outcome': 'opened', 'file': row['file']})
        import subprocess
        subprocess.Popen(['explorer.exe', '/select,', row['file']])
        return ToolExecution(True, inspect.t('revealed', code, file=row['file']), {'outcome': 'revealed', 'file': row['file']})
    except OSError:
        return ToolExecution(False, inspect.t('open_failed', code, file=row['file']), {'outcome': 'open_failed'})
