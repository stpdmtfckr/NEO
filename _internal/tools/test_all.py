"""One user-facing test entrypoint: source UI, safe tests, saved APIs, voice, atlas.
Only configured read-only endpoints use the real profile. All mutation/phrase
checks run in isolated child profiles; exports are an explicit allowlist.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT.parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from jarvis.runtime.diagnostics import redact,write_report
from jarvis.testing.release_version import release_number

RELEASE = release_number(ROOT)


def run_helper(command, timeout, env=None, *, label='', progress_file=None):
    """Wait for our helper, with periodic progress; never kill the normal NEO."""
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, cwd=ROOT, env=env, **flags)
    started = time.monotonic()
    last_notice = started
    try:
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                out, _ = process.communicate(timeout=min(10.0, remaining))
                return {'exit_code': process.returncode, 'seconds': round(time.monotonic()-started, 3),
                        'output': redact(out.decode('utf-8', 'replace'))}
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if label and now - last_notice >= 15:
                    detail = ''
                    if progress_file:
                        try:
                            from jarvis.testing.test_contract151 import read_object
                            progress = read_object(progress_file)
                            detail = f" {progress.get('completed', 0)}/{progress.get('planned', '?')} records"
                        except (OSError, ValueError):
                            pass
                    print(f"  [{label}]{detail}; elapsed {int(now-started)}s; still running.", flush=True)
                    last_notice = now
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        _stop_owned(process)
        try:
            out, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b''
        if isinstance(exc, KeyboardInterrupt):
            raise
        return {'exit_code': 124, 'seconds': round(time.monotonic()-started, 3),
                'output': redact(out.decode('utf-8', 'replace'))[-16000:], 'status': 'TIMEOUT'}


def isolated_env(base):
    return dict(os.environ,LOCALAPPDATA=str(base/'local'),APPDATA=str(base/'roaming'),
                PYTHONPATH=str(ROOT),PYTHONUTF8='1',PYTHONIOENCODING='utf-8',
                NEO_TEST_MODE='1',PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                QT_QPA_PLATFORM='offscreen')


def settings_inventory(config):
    """Read-only shape validation. Values, credentials and personal names excluded."""
    defaults=json.loads((ROOT/'config/default_settings.json').read_text(encoding='utf-8'))
    rows=[]
    def walk(obj,prefix=''):
        for key,value in obj.items():
            path=prefix+'.'+key if prefix else key
            if isinstance(value,dict):walk(value,path);continue
            actual=config.get(path,None)
            from jarvis.runtime.diagnostics import sensitive_key
            hidden=sensitive_key(path) or any(x in path.casefold() for x in ('client_id','pairing'))
            numeric_timeout = path == 'ollama.first_token_timeout_seconds'
            valid=type(actual) is type(value) or value is None or (type(value) is float and type(actual) is int) or (numeric_timeout and type(actual) in (int,float))
            rows.append({'key':path,'type':type(actual).__name__,'sensitive_value_excluded':hidden,
                         'status':'TYPE_OK' if valid else 'TYPE_REVIEW'})
    walk(defaults)
    return {'scope':'All declared leaf-setting types; no real-profile writes; UI behavior covered only by executed Qt tests',
            'declared_count':len(rows),'values_exported':False,'rows':rows}


def check_ui():
    if os.name!='nt':return {'status':'NOT_RUN','reason':'Windows required'}
    from bootstrap import launch
    from jarvis.runtime.instance_coordinator import InstanceCoordinator
    coordinator=InstanceCoordinator()
    existing=coordinator.primary_pid() if coordinator.has_primary() else None
    if existing:
        import psutil
        try:
            args=psutil.Process(existing).cmdline()
            match=any(Path(x).resolve()==(ROOT/'app.py').resolve() for x in args if x.endswith('app.py'))
        except (psutil.Error,OSError):match=False
        if not match:return {'status':'OTHER_INSTANCE_NOT_TESTED','reason':'Close an older or unidentified NEO from its tray to test startup','pid':existing}
        return {'status':'REUSED_READY' if coordinator.request('ping',wait_ack=True,timeout=4) else 'PING_UNCONFIRMED','pid':existing}
    code=launch()
    okay=code==0 and coordinator.request('ping',wait_ack=True,timeout=4)
    return {'status':'READY' if okay else 'NOT_READY','launch_exit_code':code,'pid':coordinator.primary_pid(),'assistant_left_running':True}


def _stop_owned(proc):
    """Only our live Popen and its interpreter redirector children, never a PID sweep."""
    if proc is None or proc.poll() is not None:
        return
    try:
        import psutil
        for child in psutil.Process(proc.pid).children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass

def speech_quality_probe(config):
    corpus = PROJECT/'speech_corpus/manifest.json'
    if not corpus.is_file():
        return {'status': 'NOT_RUN', 'reason': 'No WAV corpus supplied'}
    model = os.environ.get('NEO_SPEECH_MODEL_DIR', '')
    if not model:
        try:
            from huggingface_hub import try_to_load_from_cache
            from faster_whisper.utils import _MODELS
            name = str(config.get('voice.stt_model', 'small'))
            if (Path(name)/'model.bin').is_file(): model = name
            else:
                cached = try_to_load_from_cache(_MODELS.get(name, name), 'model.bin')
                if isinstance(cached, str): model = str(Path(cached).parent)
        except Exception:
            pass
    if not model or not (Path(model)/'model.bin').is_file():
        return {'status': 'MODEL_UNAVAILABLE', 'reason': 'Set NEO_SPEECH_MODEL_DIR to an existing local model'}
    with tempfile.TemporaryDirectory(prefix='neo-speech-check-') as folder:
        target = Path(folder)/'speech.json'
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        result = run_helper([sys.executable, str(ROOT/'tools/verify_speech.py'), '--corpus', str(corpus), '--model', model, '--output', str(target)], 900, env, label='recorded speech')
        if not target.is_file():
            return {'status':'ERROR', 'exit_code':result['exit_code']}
        data = json.loads(target.read_text(encoding='utf-8'))
        return {'status': 'PASS' if result['exit_code']==0 and data['summary']['passed'] > 0 and data['summary']['unscored']==0 else 'FINDINGS', 'summary': data['summary'],
                'raw_audio_exported':False, 'transcripts_exported':False}


def pack(output, report, atlas_path):
    """Allowlisted, structure-aware sanitization; invalid input cannot look successful."""
    from jarvis.runtime.diagnostics import report_bytes
    destination=output.parent/(output.name+'_RESULTS.zip')
    temporary=destination.with_suffix('.partial')
    allowed=['speech_quality.json','settings_inventory.json','integrations.json','ui.json','safe_tests.log',
             'safe_tests_summary.json','voice_setup.log','voice_setup_status.json',
             'voice_runtime_install.log','atlas_runner.log','resources.json','wave_last.json',
             'build_integrity.json']
    contents={};errors=[]
    for name in allowed:
        path=output/name
        if not path.is_file() or path.is_symlink():continue
        text=path.read_text(encoding='utf-8',errors='replace')
        if name.endswith('.json'):
            try:content=report_bytes(json.loads(text))
            except ValueError:
                errors.append(name)
                content=report_bytes({'status':'INVALID_SOURCE_JSON','file':name})
        else:content=redact(text).encode('utf-8')
        contents['reports/'+name]=content
    if errors:
        report.update(status='DIAGNOSTIC_ERROR',exit_code=2,invalid_source_json=errors)
        report.setdefault('failure_reasons',[]).append('invalid_source_json')
    write_report(output/'report.json',report)
    contents['reports/report.json']=report_bytes(report)
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in contents.items():archive.writestr(name,content)
        if atlas_path and atlas_path.is_file() and not atlas_path.is_symlink():archive.write(atlas_path,'atlas/RESULTS.zip')
        elif (output/'atlas').is_dir():
            for name in ('summary.json','progress.json','manifest.json','commands.jsonl.gz','language_switches.jsonl','runner.log'):
                candidate=output/'atlas'/name
                if candidate.is_file() and not candidate.is_symlink():archive.write(candidate,'atlas_partial/'+name)
        archive.writestr('README.txt',f'NEO {RELEASE} unified diagnostics. Settings: names/types only. No private profile, keys, database or audio. Inspect logs before sharing.\n')
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip():raise RuntimeError('Report ZIP CRC failure')
        for name in archive.namelist():
            if name.endswith('.json'):json.loads(archive.read(name))
    temporary.replace(destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--show-atlas-window', action='store_true', help='Optional per-phrase GUI; the default test runs the same sequential cases in the console')
    parser.add_argument('--limit-per-language', type=int, default=0)
    parser.add_argument('--offline-test', action='store_true', help='developer: no UI, downloads or account probes')
    parser.add_argument('--invocation-id')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--verify-receipt', type=Path)
    parser.add_argument('--process-exit', type=int)
    a = parser.parse_args(argv)
    from bootstrap import ReadOnlyConfig, DATA
    from jarvis.testing.test_contract151 import (
        atlas_complete, outcome, read_object, receipt_path, write_receipt, verify_receipt)
    source = Path(__file__).resolve()
    diagnostics = DATA / 'diagnostics'
    if a.verify_receipt:
        return verify_receipt(diagnostics, a.invocation_id or '', a.verify_receipt, a.process_exit, source)
    invocation = a.invocation_id or uuid.uuid4().hex
    try:
        expected_receipt = receipt_path(diagnostics, invocation)
        if a.receipt and a.receipt.resolve() != expected_receipt.resolve():
            raise ValueError('Receipt must be in this test profile diagnostics directory')
        if expected_receipt.exists():
            raise ValueError('An existing invocation may not be reused')
    except ValueError as exc:
        print('TEST_INVOCATION_ERROR=' + str(exc), flush=True)
        return 2
    config = ReadOnlyConfig()
    output = diagnostics / (f'NEO_test_{RELEASE}_' + time.strftime('%Y%m%d_%H%M%S_') + invocation[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {'version': f'NEO-{RELEASE}-UNIFIED.1', 'invocation_id': invocation, 'started_at': time.time(),
              'status': 'RUNNING', 'real_account_mutations': False, 'steps': {}, 'output': str(output),
              'offline_test': a.offline_test, 'limited_atlas': a.limit_per_language > 0,
              'atlas_completion_verified': False}
    atlas_zip = None
    exit_code = 2
    sampler = None
    initial_sources = None
    print('UNIFIED_TEST_STARTED=' + invocation, flush=True)
    print('REPORT_DIRECTORY=' + str(output), flush=True)

    def phase(name, caption):
        report['active_step'] = name
        write_report(output / 'report.json', report)
        print(caption, flush=True)

    try:
        from jarvis.testing.build_integrity156 import source_snapshot, verify_release
        initial_sources = source_snapshot(PROJECT)
        integrity = verify_release(PROJECT, snapshot=initial_sources)
        if integrity['status'] == 'NOT_PRESENT':
            integrity = {'status': 'BASELINE_RECORDED', 'files_checked': len(initial_sources),
                         'packaged_reference_checked': False,
                         'reason': 'Source fingerprints recorded for before/after comparison; no separate manifest required'}
        integrity['source_sha256'] = initial_sources
        report['steps']['build_integrity'] = {k: v for k, v in integrity.items() if k != 'source_sha256'}
        write_report(output/'build_integrity.json', integrity)
        print('BUILD_INTEGRITY=' + integrity['status'], flush=True)
        if integrity['status'] in {'MISMATCH', 'INVALID'}:
            raise RuntimeError('Release files do not match the optional legacy manifest. Extract the complete archive into a new folder.')
        phase('ui', '[1/5] Normal NEO readiness (an existing current instance is reused).')
        ui = {'status': 'NOT_RUN', 'reason': 'offline developer mode'} if a.offline_test else check_ui()
        write_report(output/'ui.json', ui)
        report['steps']['ui'] = ui
        print('  UI_STATUS=' + str(ui.get('status')), flush=True)
        from jarvis.testing.resource_sample150 import Sampler
        sampler = Sampler(ui.get('pid') if ui.get('status') in {'READY', 'REUSED_READY'} else None)
        sampler.start()
        phase('safe_tests', '[2/5] Isolated regression tests and settings inventory.')
        inventory = settings_inventory(config)
        write_report(output/'settings_inventory.json', inventory)
        report['steps']['settings'] = {
            'declared_count': inventory['declared_count'],
            'type_review_count': sum(r['status'] != 'TYPE_OK' for r in inventory['rows'])}
        with tempfile.TemporaryDirectory(prefix=f'neo-test-{RELEASE}-') as temp:
            env = isolated_env(Path(temp))
            result = run_helper([sys.executable, '-u', str(ROOT/'safe_test_runner.py')], 2100, env, label='safe tests')
            safe_report_path = Path(env['LOCALAPPDATA'])/'NeoJarvis/diagnostics/test_results_latest.json'
            try:
                safe_summary = read_object(safe_report_path)
            except (OSError, ValueError):
                safe_summary = {'status': 'REPORT_MISSING', 'tests': []}
            # Copy before the isolated profile is removed. No XML/assertion secrets.
            write_report(output/'safe_tests_summary.json', safe_summary)
        (output/'safe_tests.log').write_text(result['output'], encoding='utf-8', newline='\n')
        tests = safe_summary.get('tests', [])
        report['steps']['safe_tests'] = {k: v for k, v in result.items() if k != 'output'}
        report['steps']['safe_tests']['tests_recorded'] = len(tests) if isinstance(tests, list) else 0
        if safe_summary.get('exit_code') != result['exit_code']:
            report['steps']['safe_tests']['tests_recorded'] = 0
        print('  SAFE_TESTS_EXIT=' + str(result['exit_code']) + '; RECORDS=' + str(len(tests)), flush=True)
        speech_quality = speech_quality_probe(config)
        report['steps']['speech_quality'] = speech_quality
        write_report(output/'speech_quality.json', speech_quality)
        phase('integrations', '[3/5] Saved API credentials: read-only requests; quotas may apply.')
        from jarvis.testing.integration_probes150 import targets
        connections = []
        for target, _, _ in targets(config):
            if a.offline_test:
                row = {'provider': target, 'status': 'NOT_RUN'}
            else:
                result = run_helper([sys.executable, '-u', '-m', 'jarvis.testing.integration_probes150', '--target', target], 30, label='API '+target)
                try:
                    row = json.loads(result['output'])
                    if not isinstance(row, dict) or not isinstance(row.get('status'), str):
                        raise ValueError('Invalid API probe result')
                except (ValueError, TypeError):
                    row = {'provider': target, 'status': 'TIMEOUT' if result['exit_code'] == 124 else 'PROBE_ERROR'}
            connections.append(row)
            print('  ' + target + ': ' + row['status'], flush=True)
        write_report(output/'integrations.json', {'results': connections, 'credentials_exported': False, 'writes_performed': False})
        report['steps']['integrations'] = {row['provider']: row['status'] for row in connections}
        phase('atlas', '[4/5] Complete phrase atlas: sequential replies; automatic language settings; NO real OS actions.')
        atlas_dir = output/'atlas'
        cmd = [sys.executable, '-u', str(ROOT/'tools/atlas_all_test.py'), '--root', str(PROJECT), '--output', str(atlas_dir),
               '--auto-close', '--limit-per-language', str(max(0, a.limit_per_language))]
        if a.headless or not a.show_atlas_window:
            cmd.append('--headless')
        result = run_helper(cmd, 24*3600, label='atlas', progress_file=atlas_dir/'progress.json')
        (output/'atlas_runner.log').write_text(result['output'], encoding='utf-8', newline='\n')
        atlas_zip = output/'atlas_RESULTS.zip'
        try:
            atlas_summary = read_object(atlas_dir/'summary.json')
        except (OSError, ValueError):
            atlas_summary = {'status': 'INCOMPLETE', 'exit_code': result['exit_code']}
        report['steps']['atlas'] = atlas_summary
        report['atlas_helper_exit_code'] = result['exit_code']
        report['atlas_completion_verified'] = atlas_complete(atlas_summary, result['exit_code'], atlas_zip)
        exit_code, reasons = outcome(report, report['atlas_completion_verified'])
        report['failure_reasons'] = reasons
        report['status'] = 'INCOMPLETE' if exit_code == 2 else 'FINISHED_WITH_FINDINGS' if exit_code else 'FINISHED'
        print('  ATLAS_RECORDED=' + str(atlas_summary.get('completed', 0)) + '/' + str(atlas_summary.get('planned', '?')), flush=True)
    except KeyboardInterrupt:
        report['status'] = 'INTERRUPTED'
        exit_code = 2
    except Exception as exc:
        report.update(status='DIAGNOSTIC_ERROR', error_type=type(exc).__name__, detail=redact(str(exc))[:400])
        exit_code = 2
    finally:
        if initial_sources is not None:
            from jarvis.testing.build_integrity156 import source_snapshot
            try:
                final_sources = source_snapshot(PROJECT)
                unchanged = initial_sources == final_sources
                report['source_unchanged_during_test'] = unchanged
                if not unchanged:
                    report.update(status='DIAGNOSTIC_ERROR', exit_code=2)
                    report.setdefault('failure_reasons', []).append('release_sources_changed_during_test')
                    exit_code = 2
            except (OSError, ValueError):
                report.update(status='DIAGNOSTIC_ERROR', source_unchanged_during_test=False)
                report.setdefault('failure_reasons', []).append('release_sources_unreadable_after_test')
                exit_code = 2
        if sampler:
            try:
                write_report(output/'resources.json', sampler.finish())
            except Exception as exc:
                report['resource_report_error'] = type(exc).__name__
                report['status'] = 'DIAGNOSTIC_ERROR'
                exit_code = 2
        # Aggregate diagnostic from the last real My Wave request, possibly before
        # this test. No token, signed audio URL, track history, or account identity.
        try:
            source_wave = DATA/'diagnostics'/'wave_last.json'
            if source_wave.is_file():
                raw = read_object(source_wave)
                keys = ('station','reason','probe_count','received_candidates','recent_filtered',
                        'unavailable_filtered','accepted','errors','time','repeat_floor_enforced')
                write_report(output/'wave_last.json', {'scope':'last_real_wave_request_not_a_test_action',
                    'data':{k:raw[k] for k in keys if k in raw}})
        except (OSError, ValueError):
            pass
        report.update(exit_code=exit_code, finished_at=time.time(), assistant_left_running=True, active_step='packaging')
        print('[5/5] Packaging sanitized reports. Do not send your profile/settings/tokens.', flush=True)
        try:
            archive = pack(output, report, atlas_zip)
            exit_code = int(report.get('exit_code', exit_code))
            write_receipt(diagnostics, invocation, archive, exit_code, source)
            print('\nOVERALL_STATUS=' + report['status'])
            print('DIAGNOSTIC_REASONS=' + (', '.join(report.get('failure_reasons', [])) or 'none'))
            print('DIAGNOSTIC_EXIT_CODE=' + str(exit_code))
            print('SEND_THIS_ZIP=' + str(archive), flush=True)
        except Exception as exc:
            print('REPORT_PACKAGING_FAILED=' + type(exc).__name__ + '; REPORT_DIRECTORY=' + str(output), flush=True)
            exit_code = 2
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
