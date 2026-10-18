"""One service boundary for GUI, voice and authenticated Telegram actions."""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime

from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution
from jarvis.runtime.event_store import EventStore, read_observed_history
from jarvis.runtime.game_locations import find_game_locations
from jarvis.ui.desktop_events import request_desktop
from jarvis.ui.shelf_store import ShelfStore

_MESSAGES={
 'ru':('Готово.','Выполняю действие.','Найдено: {count}.','Ничего не найдено.','История приостановлена.','История возобновлена.'),
 'en':('Done.','Working on that.','Found: {count}.','Nothing found.','History paused.','History resumed.'),
 'de':('Erledigt.','Ich führe das aus.','Gefunden: {count}.','Nichts gefunden.','Verlauf pausiert.','Verlauf fortgesetzt.'),
 'es':('Hecho.','Estoy realizando la acción.','Encontrados: {count}.','No se encontró nada.','Historial pausado.','Historial reanudado.'),
 'fr':('Terminé.','Je m’en occupe.','Résultats : {count}.','Aucun résultat.','Historique en pause.','Historique repris.'),
 'it':('Fatto.','Me ne occupo.','Risultati: {count}.','Nessun risultato.','Cronologia sospesa.','Cronologia ripresa.'),
 'pt':('Concluído.','Estou a tratar disso.','Resultados: {count}.','Nada encontrado.','Histórico pausado.','Histórico retomado.'),
}
_EMPTY_CHECKLIST187={
 'ru':('Создал пустой список «{title}». Пунктов в нём пока нет.','Список «{title}» сохранён без пунктов.'),
 'en':('Created an empty list «{title}». It has no items yet.','The list «{title}» was saved with no items.'),
 'de':('Leere Liste «{title}» erstellt. Sie hat noch keine Einträge.','Die Liste «{title}» wurde ohne Einträge gespeichert.'),
 'fr':('Liste vide «{title}» créée. Elle ne contient encore aucun élément.','La liste «{title}» a été enregistrée sans éléments.'),
 'es':('He creado la lista vacía «{title}». Aún no tiene elementos.','La lista «{title}» se guardó sin elementos.'),
 'it':('Ho creato l’elenco vuoto «{title}». Non ha ancora voci.','L’elenco «{title}» è stato salvato senza voci.'),
 'pt':('Criei a lista vazia «{title}». Ainda não tem itens.','A lista «{title}» foi guardada sem itens.'),
}
_DETAIL_MESSAGES={
 'ru':('Сейчас занят другим действием. Попробуйте ещё раз через несколько секунд.','Запись изменилась или ещё не открыта. Откройте её и повторите правку.','Уточните нужный путь.','В проверенных источниках путь не найден.','подтверждён','предположение','Открыта папка'),
 'en':('I am busy with another action. Try again in a few seconds.','The item has changed or has not been opened yet. Open it and try the edit again.','Please specify the desired path.','No path was found in the checked sources.','verified','inferred','Folder opened'),
 'de':('Ich bin gerade mit einer anderen Aktion beschäftigt. Versuche es in ein paar Sekunden erneut.','Der Eintrag wurde geändert oder noch nicht geöffnet. Öffne ihn und wiederhole die Änderung.','Bitte den gewünschten Pfad angeben.','In den geprüften Quellen wurde kein Pfad gefunden.','bestätigt','abgeleitet','Ordner geöffnet'),
 'fr':('Je termine une autre action. Réessayez dans quelques secondes.','L’élément a changé ou n’a pas encore été ouvert. Ouvrez-le et réessayez la modification.','Précise le chemin souhaité.','Aucun chemin trouvé dans les sources vérifiées.','vérifié','supposé','Dossier ouvert'),
 'es':('Estoy ocupado con otra acción. Inténtalo de nuevo en unos segundos.','El elemento ha cambiado o aún no se ha abierto. Ábrelo y repite la edición.','Indica la ruta deseada.','No se encontró ninguna ruta en las fuentes consultadas.','verificado','inferido','Carpeta abierta'),
 'it':('Sto completando un’altra azione. Riprova tra qualche secondo.','L’elemento è cambiato o non è ancora stato aperto. Aprilo e ripeti la modifica.','Specifica il percorso desiderato.','Nessun percorso trovato nelle fonti controllate.','verificato','dedotto','Cartella aperta'),
 'pt':('Estou a concluir outra ação. Tenta novamente daqui a alguns segundos.','O item mudou ou ainda não foi aberto. Abre-o e repete a alteração.','Indica o caminho pretendido.','Não foi encontrado um caminho nas fontes verificadas.','verificado','inferido','Pasta aberta'),
}
_LOCATION_ORDER=('install','exe','config','saves','logs','mods','screenshots','videos','related')
_LOCATION_LEGEND={
 'ru':'✓ — подтверждено, • — найдено по названию папки.',
 'en':'✓ — verified, • — found by folder name.',
 'de':'✓ — bestätigt, • — anhand des Ordnernamens gefunden.',
 'fr':'✓ — vérifié, • — trouvé d’après le nom du dossier.',
 'es':'✓ — verificado, • — encontrado por el nombre de la carpeta.',
 'it':'✓ — verificato, • — trovato dal nome della cartella.',
 'pt':'✓ — verificado, • — encontrado pelo nome da pasta.',
}


def _grouped_locations(rows,labels,language):
    """One heading per kind, one line per place; source tags stay in the data."""
    lines=[]
    kinds=[k for k in _LOCATION_ORDER if any(r.get('kind')==k for r in rows)]
    kinds+=[k for k in dict.fromkeys(r.get('kind') for r in rows) if k not in kinds]
    for kind in kinds:
        label=str(labels.get(kind,kind))
        lines.append('')
        lines.append(label[:1].upper()+label[1:])
        for row in (r for r in rows if r.get('kind')==kind):
            mark='✓' if row.get('confirmed') else '•'
            line=f"  {mark} {row['path']}"
            children=row.get('children') or []
            if children:
                shown=', '.join(sorted(children,key=str.casefold)[:12])
                line+=f" ({len(children)}: {shown}{', …' if len(children)>12 else ''})"
            lines.append(line)
    lines.append('')
    lines.append(_LOCATION_LEGEND.get(language,_LOCATION_LEGEND['en']))
    return '\n'.join(lines)


_QUEUE_LABELS = {
 'shelf.show':['Открываю полку.','Opening the shelf.','Ich öffne das Regal.','J’ouvre l’étagère.','Abriendo la estantería.','Apro lo scaffale.','A abrir a prateleira.'],
 'shelf.hide':['Скрываю полку.','Hiding the shelf.','Ich blende das Regal aus.','Je masque l’étagère.','Ocultando la estantería.','Nascondo lo scaffale.','A ocultar a prateleira.'],
 'shelf.toggle':['Переключаю видимость полки.','Toggling the shelf.','Ich schalte die Regalanzeige um.','Je change la visibilité de l’étagère.','Cambiando la visibilidad de la estantería.','Cambio la visibilità dello scaffale.','A alternar a visibilidade da prateleira.'],
 'shelf.copy':['Копирую выбранные объекты полки.','Copying the selected shelf items.','Ich kopiere die ausgewählten Regaleinträge.','Je copie les éléments sélectionnés de l’étagère.','Copiando los elementos seleccionados.','Copio gli elementi selezionati dello scaffale.','A copiar os itens selecionados da prateleira.'],
 'browser.open':['Открываю просмотр.','Opening the viewer.','Ich öffne die Ansicht.','J’ouvre la visionneuse.','Abriendo el visor.','Apro il visualizzatore.','A abrir o visualizador.'],
 'browser.close':['Закрываю окно просмотра.','Closing the viewer.','Ich schließe die Ansicht.','Je ferme la visionneuse.','Cerrando el visor.','Chiudo il visualizzatore.','A fechar o visualizador.'],
 'browser.pin':['Меняю закрепление окна просмотра.','Updating the viewer pin.','Ich ändere die Anheftung der Ansicht.','Je modifie l’épinglage de la visionneuse.','Cambiando la fijación del visor.','Cambio il blocco del visualizzatore.','A alterar a fixação do visualizador.'],
 'browser.zoom':['Меняю масштаб просмотра.','Adjusting the viewer zoom.','Ich ändere den Zoom der Ansicht.','Je règle le zoom de la visionneuse.','Ajustando el zoom del visor.','Regolo lo zoom del visualizzatore.','A ajustar o zoom do visualizador.'],
 'settings.open':['Открываю настройки.','Opening settings.','Ich öffne die Einstellungen.','J’ouvre les paramètres.','Abriendo los ajustes.','Apro le impostazioni.','A abrir as definições.'],
}
_LANG_ORDER = ('ru','en','de','fr','es','it','pt')
_GAME_KIND_LABELS = {
 'ru':('установка','исполняемый файл','сохранения','настройки','скриншоты','видеозаписи','журналы','моды'),
 'en':('installation','executable','saves','settings','screenshots','videos','logs','mods'),
 'de':('Installation','Programmdatei','Spielstände','Einstellungen','Screenshots','Videos','Protokolle','Mods'),
 'fr':('installation','exécutable','sauvegardes','paramètres','captures d’écran','vidéos','journaux','mods'),
 'es':('instalación','ejecutable','partidas guardadas','ajustes','capturas','vídeos','registros','mods'),
 'it':('installazione','eseguibile','salvataggi','impostazioni','screenshot','video','registri','mod'),
 'pt':('instalação','executável','jogos guardados','definições','capturas','vídeos','registos','mods'),
}


def notify_network_changed(registry):
    for name in ('yandex_adapter','spotify_adapter','discord_adapter','youtube_adapter'):
        callback=getattr(getattr(registry,name,None),'on_network_changed',None)
        if callable(callback):
            try:
                callback()
            except Exception:
                import logging
                logging.getLogger(__name__).debug('Network reconnect deferred for %s',name,exc_info=True)


def register_desktop_features(registry):
    config=registry.config
    shelf=ShelfStore(config.paths.root)
    store=EventStore(config.paths.root,config)
    def text(n,**kw):
        return _MESSAGES.get(str(config.get('app.language','en')),_MESSAGES['en'])[n].format(**kw)
    def detail(n):
        return _DETAIL_MESSAGES.get(str(config.get('app.language','en')),_DETAIL_MESSAGES['en'])[n]
    def queued(action,**payload):
        ok=request_desktop(action,**payload)
        data={'queued':ok,'completed':False,'desktop_action':action,'outcome_pending':bool(ok)}
        if payload.get('id'):data['window_id']=payload['id']
        language=str(config.get('app.language','en'))
        index=_LANG_ORDER.index(language) if language in _LANG_ORDER else 1
        label=_QUEUE_LABELS.get(action,_QUEUE_LABELS['browser.open'])[index]
        return ToolExecution(ok,label if ok else detail(0),data)
    def shelf_tool(args):
        action=args['action']
        oid=str(args.get('id',args.get('object_id','')))
        if action in {'show','hide','toggle'}:
            return queued('shelf.'+action)
        if action=='list':
            rows=shelf.list(args.get('kind') or None,str(args['shelf']) if args.get('shelf') not in (None,'') else None)
            message=text(2,count=len(rows))+'\n'+'\n'.join(f"{r['title']} [{r['kind']}; id={r['id']}]" for r in rows[:50])
            return ToolExecution(True,message.strip(),rows)
        if action in {'read','get','update','pin','check','delete','copy'} and not oid:
            title=str(args.get('name') or args.get('title','')).strip().casefold()
            # NEO 183: the name is looked up inside the chosen shelf (for update the target shelf is a
            # new value, so its search scope is «scope_shelf»); the choices show their shelf.
            scope=args.get('scope_shelf') if action=='update' else args.get('shelf')
            scope=str(scope) if scope not in (None,'') else None
            matching=[r for r in shelf.list(None,scope) if r['title'].casefold()==title]
            if len(matching)!=1:
                return ToolExecution(False,{
                    'ru':'Уточните название или идентификатор объекта полки.',
                    'en':'Please specify the shelf item title or ID.',
                    'de':'Bitte den Titel oder die ID des Regaleintrags angeben.',
                    'fr':'Précisez le titre ou l’identifiant de l’élément.',
                    'es':'Indica el título o ID del elemento.',
                    'it':'Specifica il titolo o ID dell’elemento.',
                    'pt':'Informe o título ou ID do item.',
                }.get(config.get('app.language','en'),'Please specify the shelf item ID.')+(''.join(f"\n• {r['title']} — {r.get('shelf','default')} (id={r['id']})" for r in matching[:10])),
                {'ambiguous':len(matching)>1,'matches':[{'id':r['id'],'title':r['title'],'shelf':r.get('shelf','default')} for r in matching]})
            oid=matching[0]['id']
        if action in {'read','get'}:
            row=shelf.get(oid)
            body=row['content'] or row['title']
            if row['kind']=='checklist':
                body=row['title']+'\n'+'\n'.join(f"{n}. [{'x' if item.get('checked') else ' '}] {item.get('text','')}" for n,item in enumerate(row['items'],1))
            return ToolExecution(True,body,row)
        if action=='create':
            row=shelf.create(str(args.get('kind','note')),title=str(args.get('title','')),content=str(args.get('content',args.get('text',''))),items=args.get('items'),path=str(args.get('path','')),**{k:args[k] for k in ('shelf','order','pinned','board') if args.get(k) is not None})
        elif action=='import_file':
            row=shelf.import_file(str(args.get('path','')),managed=bool(args.get('managed',False)))
        elif action in {'update','pin','check'}:
            current=shelf.get(oid)
            if action in {'update','check'} and 'version' not in args:
                return ToolExecution(False,detail(1),{'version_required':True,'id':oid,'current_version':current['version']})
            version=int(args.get('version',current['version']))
            changes={k:v for k,v in args.items() if k in shelf.EDITABLE}
            # A title used to resolve the item is not an implicit rename.
            if not args.get('id') and not args.get('object_id'):
                changes.pop('title',None)
            if 'text' in args:changes['content']=str(args['text'])
            if action=='pin':
                changes['pinned']=bool(args.get('pinned',True))
            if action=='check':
                if current['kind']!='checklist':raise ValueError('Not a checklist')
                index=int(args.get('index',-1))
                items=current['items']
                if not 0<=index<len(items):raise ValueError('Checklist item index out of range')
                items[index]['checked']=bool(args.get('checked',True))
                changes={'items':items}
            row=shelf.update(oid,version,**changes)
        elif action=='delete':
            shelf.delete(oid,expected_version=args.get('version'))
            row={'id':oid,'deleted':True,'original_deleted':False}
        elif action=='copy':
            return queued('shelf.copy',ids=args.get('ids') or [oid])
        else:
            raise ValueError('Unknown shelf action')
        request_desktop('shelf.refresh')
        if action in {'create','update'} and isinstance(row,dict) and row.get('kind')=='checklist' and not row.get('items'):
            # HIST-044: an empty checklist is said to be empty, never «done» as if filled.
            language=str(config.get('app.language','en'))
            title=str(row.get('title','') or '')
            message=_EMPTY_CHECKLIST187.get(language,_EMPTY_CHECKLIST187['en'])[0 if action=='create' else 1].format(title=title)
            return ToolExecution(True,message,{**row,'empty':True,'localized186':True})
        return ToolExecution(True,text(0),row)
    def downloads(args):
        rows=store.downloads(int(args.get('limit',50)))
        from jarvis.runtime.desktop_services import event_text
        lines=[text(2,count=len(rows)) if rows else text(3)]
        for row in rows[:30]:
            state=row['state']
            key={'detected':'download.started','downloading':'download.started'}.get(state,'download.'+state)
            payload={**row,'name':Path(row['path']).name}
            label=event_text({'message_key':key,'data':payload},config.get('app.language','en'))
            if state=='paused':
                label={'ru':'Загрузка приостановлена','en':'Download paused','de':'Download pausiert','fr':'Téléchargement en pause','es':'Descarga en pausa','it':'Download in pausa','pt':'Transferência em pausa'}.get(config.get('app.language','en'),'Download paused')
            lines.append(label+'\n'+row['path']+f" [{row['size']} B]")
        return ToolExecution(True,'\n'.join(lines),{'enabled':bool(config.get('security.smart_downloads_enabled',False)),'downloads':rows})
    def activity(args):
        action=args['action']
        if action in {'pause','resume'}:
            config.data.setdefault('activity',{})['enabled']=action=='resume'
            config.save(config.data)
            request_desktop('services.reconfigure')
            return ToolExecution(True,text(4 if action=='pause' else 5))
        if action=='clear':
            store.clear_activity()
            return ToolExecution(True,text(0))
        def timestamp(value,default):
            if not value:return default
            if isinstance(value,(float,int)):return float(value)
            return datetime.fromisoformat(str(value)).timestamp()
        if args.get('period'):
            evidence=read_observed_history(config,scope=str(args.get('scope','desktop')),period=str(args['period']),
                kind=str(args.get('kind','activity')),query=str(args.get('query','')),limit=int(args.get('limit',100)))
            rows=evidence['rows']
        elif str(args.get('scope','desktop')) != 'desktop':
            rows=[]
        else:
            rows=store.query(kind=str(args.get('kind','activity')),query=str(args.get('query','')),since=timestamp(args.get('since'),0),until=timestamp(args.get('until'),None),limit=int(args.get('limit',100)))
        if action=='export':
            target=config.paths.root/'exports'/('activity-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
            return ToolExecution(True,text(0),{'path':str(target)})
        lines=[text(2,count=len(rows)) if rows else text(3)]
        for row in rows[:50]:
            payload=row.get('data',{})
            # Display observed labels, not a dump of arbitrary event metadata.
            observed=' · '.join(dict.fromkeys(str(payload[k]) for k in ('app','process','process_name','title','path','folder_path','destination','name') if payload.get(k)))
            lines.append(datetime.fromtimestamp(row['created']).strftime('%Y-%m-%d %H:%M')+' · '+row['kind']+' · '+observed)
        return ToolExecution(True,'\n'.join(lines),rows)
    def game_locations(args):
        def progress(rows):
            from jarvis.runtime.briefing_stream import emit
            emit('locations.paths',game_locations_text(rows,args),'ready')
        rows=find_game_locations(config,str(args.get('name','')),str(args.get('kind','all')),
            on_progress=progress if args['action']!='open' else None)
        if args['action']=='open':
            selected=[row for row in rows if not args.get('path') or row['path']==args['path']]
            if len(selected)!=1:
                return ToolExecution(False,detail(2) if selected else detail(3),{'ambiguous':len(selected)>1,'results':selected})
            path=Path(selected[0]['path'])
            # "Open the game's executable location" opens its containing folder,
            # and never runs code as an incidental effect of path discovery.
            directory=path.parent if path.is_file() else path
            if not directory.is_dir():raise FileNotFoundError(directory)
            from jarvis.tools.implementations import _open_path
            _open_path(directory)
            return ToolExecution(True,detail(6)+': '+str(directory),{'path':str(directory),'source':selected[0]['source'],'confidence':selected[0]['confidence']})
        return ToolExecution(True,game_locations_text(rows,args),rows)
    def game_locations_text(rows,args):
        language=str(config.get('app.language','en'))
        labels=dict(zip(('install','exe','saves','config','screenshots','videos','logs','mods'),_GAME_KIND_LABELS.get(language,_GAME_KIND_LABELS['en'])))
        labels['related']={'ru':'связанная папка','en':'related folder','de':'zugehöriger Ordner','fr':'dossier associé','es':'carpeta relacionada','it':'cartella correlata','pt':'pasta relacionada'}.get(language,'related folder')
        message=text(2,count=len(rows))+'\n'+_grouped_locations(rows,labels,language) if rows else detail(3)
        if args.get('kind','all') == 'all':
            missing=[labels[k] for k in labels if k != 'related' and not any(row['kind']==k for row in rows)]
            unknown={'ru':'В проверенных источниках не найдены','en':'Not found in the checked sources','de':'In den geprüften Quellen nicht gefunden','fr':'Non trouvés dans les sources consultées','es':'No encontrados en las fuentes consultadas','it':'Non trovati nelle fonti consultate','pt':'Não encontrados nas fontes consultadas'}.get(language,'Not found in the checked sources')
            if missing: message+='\n\n'+unknown+': '+', '.join(missing)+'.'
        status=getattr(rows,'status',{})
        if status.get('searching'):
            loading={
              'ru':'Ищу связанные локальные папки…', 'en':'Searching related local folders…',
              'de':'Zugehörige lokale Ordner werden gesucht…', 'fr':'Recherche des dossiers locaux associés…',
              'es':'Buscando carpetas locales relacionadas…', 'it':'Ricerca delle cartelle locali correlate…',
              'pt':'A procurar pastas locais relacionadas…',
            }
            message+='\n'+loading.get(language,loading['en'])
        elif status.get('continuable'):
            partial={
              'ru':'Проверена часть локальных папок. Повторите этот запрос, чтобы продолжить поиск с места остановки.',
              'en':'Some local folders have been checked. Repeat this request to continue the search.',
              'de':'Ein Teil der lokalen Ordner wurde geprüft. Wiederhole die Anfrage, um die Suche fortzusetzen.',
              'fr':'Une partie des dossiers locaux a été vérifiée. Répétez la demande pour poursuivre la recherche.',
              'es':'Se ha revisado parte de las carpetas locales. Repite la petición para continuar la búsqueda.',
              'it':'È stata controllata una parte delle cartelle locali. Ripeti la richiesta per continuare la ricerca.',
              'pt':'Foi verificada parte das pastas locais. Repete o pedido para continuar a pesquisa.',
            }
            message+='\n'+partial.get(language,partial['en'])
        elif status.get('incomplete'):
            limited={
              'ru':'Системные, недоступные папки и ссылки не проверялись. Это найденные пути, а не гарантия полного списка на компьютере.',
              'en':'System folders, inaccessible folders and links were excluded; these are discovered paths, not a guaranteed complete list.',
              'de':'Systemordner, unzugängliche Ordner und Verknüpfungen wurden ausgelassen; die Liste ist möglicherweise unvollständig.',
              'fr':'Les dossiers système, inaccessibles et les liens ont été exclus ; la liste peut être incomplète.',
              'es':'Se excluyeron carpetas del sistema, inaccesibles y enlaces; la lista puede estar incompleta.',
              'it':'Sono state escluse cartelle di sistema, inaccessibili e collegamenti; l’elenco potrebbe essere incompleto.',
              'pt':'Foram excluídas pastas do sistema, inacessíveis e ligações; a lista pode estar incompleta.',
            }
            message+='\n'+limited.get(language,limited['en'])
        return message
    def browser(args):
        action=args['action']
        payload={k:v for k,v in args.items() if k!='action'}
        if action=='open' and payload.get('path') and not payload.get('id'):
            import uuid
            payload['id']='image-'+uuid.uuid4().hex
        return queued('browser.'+action,**payload)
    def settings(args):
        # NEW185-BOOKSHELF-SETTINGS-SECTION-LOSS: every catalogue phrase of this action is about
        # the bookshelf/widgets page; a missing section is that page, never «Дополнительно».
        return queued('settings.open',section=str(args.get('section') or 'widgets'))
    def register(name,actions,handler,properties,risk):
        props={'action':{'type':'string','enum':actions},**properties}
        registry.register(ToolDefinition(name,name,{'type':'object','properties':props,'required':['action'],'additionalProperties':False},handler,risk))
    s={'type':'string'}; b={'type':'boolean'}; i={'type':'integer'}
    register('shelf',['show','hide','toggle','list','read','get','create','update','check','delete','import_file','pin','copy'],shelf_tool,
             {'kind':s,'title':s,'name':s,'content':s,'text':s,'items':{'type':'array'},'path':s,'managed':b,'id':s,'object_id':s,'version':i,'pinned':b,'ids':{'type':'array','items':s},'shelf':s,'scope_shelf':s,'order':i,'index':i,'checked':b},lambda a:Risk.MEDIUM if a['action'] in {'delete','import_file','copy'} else Risk.SAFE)
    register('downloads',['list','status'],downloads,{'limit':i},lambda a:Risk.SAFE)
    register('activity',['list','search','pause','resume','clear','export'],activity,{'query':s,'kind':s,'since':s,'until':s,'period':{'type':'string','enum':['today','yesterday','recent','all']},'scope':s,'limit':i},lambda a:Risk.HIGH if a['action']=='clear' else Risk.SAFE)
    register('game_locations',['find','query','open'],game_locations,{'name':s,'kind':s,'path':s},lambda a:Risk.MEDIUM if a['action']=='open' else Risk.SAFE)
    register('mini_browser',['open','close','pin','zoom'],browser,{'url':s,'path':s,'id':s,'zoom':{'type':'number'},'pinned':b},lambda a:Risk.MEDIUM if a['action']=='open' else Risk.SAFE)
    register('desktop_features',['settings'],settings,{'section':s},lambda a:Risk.SAFE)
