"""Voice model preparation for the installer, NEO.cmd and Settings. No synthesis.

NEO 185: three tiers. light = Windows voices (nothing to download); standard = pinned
Qwen3-TTS 0.6B; heavy = Qwen3-TTS 1.7B pinned to the publisher listing at download time.
Without --restore/--tier the saved settings decide: nothing is downloaded unless the user
turned the voice on (no hidden downloads).
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import threading
from jarvis.response_voice.model_store import ModelStore, VoiceModelError, Cancelled
from jarvis.response_voice.runtime import ensure_runtime
from jarvis.response_voice.diagnostics import STAGES, record_failure, stage_event


def prepare(profile, *, explicit=False, store=None, install=None):
    cancel=threading.Event();stage='model_manifest'
    def progress(**event):
        nonlocal stage
        next_stage=event.get('stage') or {'downloading':'model_download','checking':'model_check','runtime':'runtime'}.get(event.get('state'))
        if next_stage in STAGES and next_stage!=stage:
            stage=next_stage;stage_event('bootstrap',stage)
    try:
        store=store or ModelStore(profile)
        if store.tombstone.exists() and not explicit:
            return {'state':'deleted','voice_ready':False}
        getattr(store,'validate_before_download',store._validate)()
        stage='model_download';stage_event('bootstrap',stage)
        result=store.download(cancel,progress,explicit=explicit)
        with store.operation_lock():
            if store.tombstone.exists():raise VoiceModelError('deleted')
            stage='runtime';stage_event('bootstrap',stage)
            if install is None:ensure_runtime(profile,cancel,progress)
            else:install(profile,cancel)
        return dict(result,voices=['Aiden','Serena'],voice_ready=False,synthesis_tested=False)
    except KeyboardInterrupt:
        cancel.set()
        return {'state':'cancelled','voice_ready':False}
    except Exception as error:
        if cancel.is_set() or isinstance(error,Cancelled):
            return {'state':'cancelled','voice_ready':False}
        failure=record_failure(profile,error,action='bootstrap',stage=stage)
        return {'state':failure['code'],'voice_ready':False,'failure':failure}


def saved_voice(profile):
    for path in (profile/'настройки'/'settings.json', profile/'settings.json'):
        try:
            data=json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):
            continue
        voice=data.get('response_voice') if isinstance(data,dict) else None
        return voice if isinstance(voice,dict) else {}
    return {}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--restore',action='store_true')
    parser.add_argument('--tier',choices=('light','standard','heavy'))
    args=parser.parse_args()
    profile=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local')))/'NeoJarvis'
    saved=saved_voice(profile)
    tier=args.tier or saved.get('tier') or 'standard'
    if not args.restore and not args.tier and saved.get('enabled') is not True:
        print(json.dumps({'state':'not_requested','voice_ready':False}),flush=True)
        return 0
    if tier=='light':
        print(json.dumps({'state':'verified','tier':'light','voice_ready':False}),flush=True)
        return 0
    from jarvis.response_voice.tiers import store_for
    result=prepare(profile,explicit=args.restore or bool(args.tier),store=store_for(profile,tier))
    print(json.dumps(result,ensure_ascii=False),flush=True)
    # Optional voice failures must not prevent text/microphone startup.
    return 0 if result['state'] in ('verified','deleted','manifest_incomplete','not_requested') else 2


if __name__ == '__main__': raise SystemExit(main())
