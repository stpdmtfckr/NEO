"""Process-local I/O guard for the opt-in runner, NOT an OS security sandbox."""
from contextlib import contextmanager
from unittest.mock import patch
import os
import socket
import subprocess
import webbrowser


def _blocked(*_args, **_kwargs):
    raise PermissionError('QA forbids network access and external process launches')


@contextmanager
def external_io_blocked():
    # This belongs only to the dedicated QA process. It never patches the
    # already running production assistant or any other application.
    patches = [patch.object(socket, 'create_connection', _blocked),
               patch.object(socket, 'getaddrinfo', _blocked),
               patch.object(socket.socket, 'connect', _blocked),
               patch.object(socket.socket, 'connect_ex', _blocked),
               patch.object(subprocess, 'Popen', _blocked),
               patch.object(webbrowser, 'open', _blocked)]
    if hasattr(os, 'startfile'):
        patches.append(patch.object(os, 'startfile', _blocked))
    for entry in patches: entry.start()
    try:
        yield
    finally:
        for entry in reversed(patches): entry.stop()
