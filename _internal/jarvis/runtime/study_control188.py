"""NEO 188 NEO-STUDY-001: the agent side of a practice session (see study188).

``respond`` returns ``(text, waiting_for_answer)`` when the input belongs to the
practice session, or None so that ordinary routing handles it. A paused or
finished session takes no answers; only its own controls.
"""
from __future__ import annotations

import ntpath
import re
from pathlib import Path
from typing import Any

from jarvis.runtime import study188 as st


def _sessions(agent: Any) -> dict:
    store = getattr(agent, '_study_sessions188', None)
    if store is None:
        store = agent._study_sessions188 = {}
    return store


def _scope(agent: Any) -> str:
    return str(getattr(agent, '_conversation_key', 'desktop') or 'desktop')


def active(agent: Any, scope: str = 'desktop') -> st.Session | None:
    return (getattr(agent, '_study_sessions188', None) or {}).get(scope)


def _service(agent: Any):
    from jarvis.runtime import knowledge169
    return knowledge169.service(agent.config)


def _root(agent: Any) -> Path:
    return Path(agent.config.paths.root)


def _resolve(agent: Any, name: str, code: str) -> tuple[str | None, str | None]:
    from jarvis.runtime import document_scope188
    index = _service(agent).index
    found = document_scope188.resolve(index, name)
    if not found and not any(sep in name for sep in ('\\', '/', '.')):
        # «по конспекту» → конспект.txt: an inflected form of an allowed file's own name.
        wanted = name.casefold().replace('ё', 'е').strip()
        found = []
        for row in index.files():
            stem = ntpath.basename(str(row['path']).replace('/', '\\')).rsplit('.', 1)[0].casefold().replace('ё', 'е')
            if len(stem) >= 4 and (wanted.startswith(stem) and len(wanted) - len(stem) <= 3
                                   or stem.startswith(wanted[:-2]) and len(wanted) >= 6 and len(stem) - len(wanted) <= 2):
                found.append(row['path'])
    if not found:
        return None, st.t('unknown_file', code, name=name)
    if len(found) > 1:
        return None, st.t('ambiguous_file', code, name=name, choices='\n'.join(f'{i}. {p}' for i, p in enumerate(found[:8], 1)))
    return found[0], None


def _material(agent: Any, path: str) -> tuple[list[dict], dict | None]:
    index = _service(agent).index
    row = next((r for r in index.files() if r['path'] == path), None)
    return index.first_chunks(path, st.MAX_CHUNKS), row


def _allowed_and_current(agent: Any, session: st.Session) -> bool:
    """The source is still granted (indexed), on disk, unchanged since the questions were built."""
    from jarvis.runtime import document_scope188
    index = _service(agent).index
    if document_scope188.stale(index, session.path):
        return False
    chunks, row = _material(agent, session.path)
    return bool(chunks) and st.source_version(chunks, row) == session.version


def _next(session: st.Session, code: str, prefix: str) -> tuple[str, bool]:
    st.advance(session)
    if session.state == 'finished':
        return (prefix + '\n\n' + st.summary(session, code)).strip(), False
    return (prefix + '\n\n' + st.ask(session, code)).strip(), True


def _quoted(text: str) -> bool:
    value = str(text or '').strip()
    return len(value) >= 2 and value[0] in '«"“\'' and value[-1] in '»"”\''


def _looks_like_command(text: str, code: str) -> bool:
    if _quoted(text) or len(str(text).split()) < 2:
        return False
    try:
        from jarvis.runtime.feature_commands import parse_feature_command
        from jarvis.runtime.natural_commands import looks_like_new_command
        return parse_feature_command(text, code) is not None or looks_like_new_command(text, code)
    except Exception:
        return False


def _start(agent: Any, request: dict, code: str) -> tuple[str, bool]:
    path, problem = _resolve(agent, request['file'], code)
    if path is None:
        return problem, False
    chunks, row = _material(agent, path)
    session = st.build(path, chunks, row, code, count=request['count'], section=request['section'], options=request['options'])
    label = ntpath.basename(path.replace('/', '\\'))
    if session is None:
        return st.t('no_text', code, file=label, section=request['section'] or '—'), False
    _sessions(agent)[_scope(agent)] = session
    agent._study_last188 = path
    return st.t('start', code, file=label, n=session.count) + '\n\n' + st.ask(session, code), True


def _resume_saved(agent: Any, name: str, code: str) -> tuple[str, bool]:
    path, problem = _resolve(agent, name, code)
    if path is None:
        return problem, False
    saved = st.load_checkpoint(_root(agent), path)
    label = ntpath.basename(path.replace('/', '\\'))
    if saved is None:
        return st.t('no_checkpoint', code), False
    chunks, row = _material(agent, path)
    session = st.build(path, chunks, row, saved.get('language') or code, count=int(saved.get('count') or 1),
                       section=str(saved.get('section') or ''), options=bool(saved.get('options')), order=list(saved.get('order') or []))
    if session is None or session.version != saved.get('version'):
        return st.t('changed', code, file=label), False
    for question, status in zip(session.questions, saved.get('statuses') or []):
        question.status = str(status)
    session.position = max(0, min(int(saved.get('position') or 0), len(session.questions)))
    session.state = 'asked' if session.current is not None else 'finished'
    _sessions(agent)[_scope(agent)] = session
    agent._study_last188 = path
    if session.state == 'finished':
        return st.summary(session, code), False
    return st.t('resumed', code, file=label) + '\n\n' + st.ask(session, code), True


def _delete(agent: Any, session: st.Session | None, code: str) -> tuple[str, bool]:
    source = session.path if session is not None else getattr(agent, '_study_last188', '')
    if not source or not st.has_checkpoint(_root(agent), source):
        return st.t('no_checkpoint', code), False
    removed, path = st.delete_checkpoint(_root(agent), source)
    label = ntpath.basename(str(source).replace('/', '\\'))
    return (st.t('deleted', code, file=label) if removed else st.t('delete_failed', code, p=str(path))), False


def respond(agent: Any, text: str, code: str) -> tuple[str, bool] | None:
    scope = _scope(agent)
    session = _sessions(agent).get(scope)
    request = st.parse_start(text, code)
    if request is not None:
        return _start(agent, request, code)
    saved_name = st.parse_continue_file(text, code)
    if saved_name is not None:
        return _resume_saved(agent, saved_name, code)
    cmd = st.command(text, code)
    if session is None:
        if cmd == 'delete':
            return _delete(agent, None, code)
        if st.without_session188(text, code):
            return st.t('no_session188', code), False
        return None
    if cmd == 'cancel':
        _sessions(agent).pop(scope, None)
        note = st.t('has_checkpoint', code, file=session.label) if st.has_checkpoint(_root(agent), session.path) else ''
        return st.t('cancelled', code) + note, False
    if cmd == 'delete':
        return _delete(agent, session, code)
    if cmd == 'save':
        try:
            st.save_checkpoint(_root(agent), session)
        except OSError:
            return st.t('save_failed', code), False
        current = session.current
        number = current.number if current is not None else session.count
        return st.t('saved', code, file=session.label, i=number, n=session.count), False
    if session.state == 'paused':
        if cmd != 'resume':
            return None                                  # paused: no answers, ordinary commands run normally
        if not _allowed_and_current(agent, session):
            _sessions(agent).pop(scope, None)
            return st.t('changed', code, file=session.label), False
        session.state = 'asked'
        return st.t('resumed', code, file=session.label) + '\n\n' + st.ask(session, code), True
    if session.state == 'finished':
        if cmd == 'retry':
            skipped = [i for i, q in enumerate(session.questions) if q.status == 'skipped' and not q.retried]
            if session.retry_used or not skipped:
                return st.t('no_retry', code), False
            session.retry_used = True
            for i in skipped:
                session.questions[i].retried = True
            session.retry_queue = skipped
            session.state = 'asked'
            return st.ask(session, code), True
        if cmd in {'resume', 'repeat'}:
            return st.summary(session, code), False
        return None
    question = session.current
    if question is None:
        session.state = 'finished'
        return st.summary(session, code), False
    if cmd == 'pause':
        session.state = 'paused'
        return st.t('paused', code), False
    if cmd == 'resume':
        return st.ask(session, code), True
    if cmd == 'repeat':
        return st.ask(session, code, other=True), True      # same number, not a new attempt
    if cmd == 'hint':
        question.hint_level = min(2, question.hint_level + 1)
        if question.hint_level == 1:
            return st.t('hint1', code, c=question.answer[0], n=len(question.answer)), True
        return st.t('hint2', code, mask=st.mask(question.answer)), True
    if cmd == 'explain':
        if question.status == 'new':
            question.status = 'skipped'                         # seen, not answered: never counted as wrong
        body = st.t('explain', code, a=question.answer, s=question.sentence) + '\n' + st.where(session, question, code)
        return _next(session, code, body)
    if cmd == 'skip':
        question.status = 'skipped'
        return _next(session, code, st.t('skipped', code))
    if cmd == 'retry':
        return st.t('no_retry', code), True
    if _looks_like_command(text, code):
        session.state = 'paused'
        return st.t('command_paused', code), False
    answer = str(text or '').strip().strip('«»"“”')
    verdict = st.check(question, answer, code)
    if verdict == 'unclear':
        return st.t('unclear', code, heard=answer[:80]) + '\n\n' + st.ask(session, code), True
    question.attempts += 1
    question.status = verdict
    if verdict == 'matched':
        body = st.t('matched', code)
    elif verdict == 'close':
        body = st.t('close', code, a=question.answer)
    else:
        body = st.t('wrong', code, a=question.answer)
    body += '\n«' + question.sentence + '»\n' + st.where(session, question, code)
    return _next(session, code, body)
