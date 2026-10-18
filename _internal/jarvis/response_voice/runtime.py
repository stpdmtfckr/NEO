"""An isolated optional Python environment: never change NEO's ASR dependencies."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import time
from .model_store import Cancelled
from .diagnostics import OutputClassifier, RuntimeFailure


def runtime_python(profile):
    root = Path(profile) / 'voice-runtime-qwen-0.1.1'
    return root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def runtime_available(profile):
    exe = runtime_python(profile)
    return exe.is_file() and (exe.parent.parent / 'installed.json').is_file()


def creation_python():
    # The GUI may run as branded NEO.exe. Use the same real base interpreter as
    # NEO's other venv builders; never search PATH or assume the branded host is Python.
    from jarvis.runtime.process_host171 import base_python
    selected=Path(base_python())
    if not selected.is_file() or selected.name.casefold()=='neo.exe':
        raise RuntimeFailure('runtime_python_missing',stage='runtime_python',category='missing_module')
    return selected


def ensure_runtime(profile, cancel, progress=lambda **kw: None, *, run=None):
    if runtime_available(profile):return
    exe=runtime_python(profile)
    def invoke(stage,command):
        if cancel.is_set():raise Cancelled('cancelled')
        progress(state='runtime',stage=stage,done=0,total=0)
        try:
            if run is None:_run(command,cancel,stage=stage)
            else:run(command,cancel)  # Inert injectable runner; no package work in QA.
        except (Cancelled,RuntimeFailure):raise
        except Exception as error:
            from .diagnostics import summarize_failure
            hint=summarize_failure(error,action='download',stage=stage)
            raise RuntimeFailure(stage=stage,category=hint['category'],errno=hint.get('errno'),winerror=hint.get('winerror'),http_status=hint.get('http_status')) from None
    progress(state='runtime',stage='runtime_python',done=0,total=0)
    python=creation_python()
    invoke('runtime_venv',[str(python),'-m','venv',str(exe.parent.parent)])
    invoke('runtime_torch',[str(exe),'-m','pip','install','--disable-pip-version-check','--no-input',
                           '--index-url','https://download.pytorch.org/whl/cpu','torch==2.9.1','torchaudio==2.9.1'])
    invoke('runtime_qwen',[str(exe),'-m','pip','install','--disable-pip-version-check','--no-input',
                          '--index-url','https://pypi.org/simple','qwen-tts==0.1.1','sounddevice==0.5.3'])
    invoke('runtime_probe',[str(exe),'-c','import qwen_tts, torch, sounddevice; assert torch.__version__.startswith("2.9.1")'])
    if cancel.is_set():raise Cancelled('cancelled')
    (exe.parent.parent/'installed.json').write_text('{"qwen_tts":"0.1.1","torch":"2.9.1","cpu_only":true}',encoding='utf-8')


def _run(command, cancel, *, stage='runtime', spawn=None, timeout_seconds=1800):
    from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
    if cancel.is_set():raise Cancelled('cancelled')
    options=hidden_process_kwargs()
    if os.name=='nt':
        options['creationflags']=int(options.get('creationflags',0)) | int(getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0x4000))
    classifier=OutputClassifier()
    process=(spawn or subprocess.Popen)(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,bufsize=0,**options)
    try:
        # NEO uses Python 3.12, which supports nonblocking Windows pipe descriptors.
        # Drain on this operation worker, avoiding a reader thread stuck in a child pipe.
        os.set_blocking(process.stdout.fileno(),False)
        deadline=time.monotonic()+max(.01,float(timeout_seconds))
        exited_at=None
        while True:
            eof=False
            for _ in range(16):
                chunk=process.stdout.read(4096)
                if chunk is None:break
                if not chunk:
                    eof=True;break
                classifier.feed(chunk)
            if cancel.is_set():raise Cancelled('cancelled')
            returncode=process.poll()
            now=time.monotonic()
            if returncode is not None:
                if exited_at is None:exited_at=now
                if eof or now-exited_at>=.5:
                    if returncode:
                        raise RuntimeFailure(stage=stage,
                            category=classifier.category if classifier.category!='unknown' else 'process_failed',
                            returncode=returncode,errno=classifier.numbers.get('errno'),
                            winerror=classifier.numbers.get('winerror'),http_status=classifier.numbers.get('http_status'))
                    return
            elif now>deadline:
                raise RuntimeFailure('runtime_timeout',stage=stage,category='timeout')
            cancel.wait(.02 if returncode is not None else .1)
    finally:
        classifier.finish()
        if process.poll() is None:process.kill()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
        try:process.stdout.close()
        except Exception:pass
