"""NEO.cmd --atlas-test; GUI by default, --headless for automated QA.

Uses a brand-new profile. This does not launch another normal wake/Discord
instance, import credentials, modify autostart, or close user applications.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
import uuid


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--selection', choices=['full','native','groups','atlas188'], default='full')
    parser.add_argument('--languages', default='ru,en,de,fr,es,it,pt')
    parser.add_argument('--limit', type=int, default=0, help='0 means no artificial case limit')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--no-live', action='store_true')
    args = parser.parse_args(argv)
    if args.limit < 0: parser.error('--limit cannot be negative')
    if args.selection == 'atlas188' and not args.headless:
        parser.error('--selection atlas188 is a headless routing report; add --headless')
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.local')))/'NeoJarvis/diagnostics/atlas_runs'
    output = args.output or base/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
    if output.exists(): parser.error('Output must be a new directory; existing files are never overwritten.')
    output = output.resolve()
    # Set BEFORE importing application code; account/global storage cannot resolve
    # to the real profile even if an old helper ignores the supplied AppPaths.
    os.environ['LOCALAPPDATA'] = str(output/'isolated_os')
    os.environ['APPDATA'] = str(output/'isolated_os')
    os.environ['NEOJARVIS_CACHE_DIR'] = str(output/'isolated_profile/cache')
    os.environ['NEO_TEST_MODE'] = '1'
    from jarvis.testing.atlas_session import AtlasSession, atlas_cases
    from jarvis.runtime.natural_commands import LANGUAGES
    locales = [code.strip() for code in args.languages.split(',') if code.strip()]
    if not locales or any(code not in LANGUAGES for code in locales):
        parser.error('--languages must contain supported locale codes')
    from jarvis.testing.isolation import external_io_blocked
    if not args.headless:
        try:
            from PySide6.QtWidgets import QApplication
            from jarvis.ui.atlas_test_window import AtlasTestWindow
        except ImportError:
            print('Qt is unavailable. Run NEO.cmd --repair or use --headless for routing-only QA.', file=sys.stderr)
            return 2
        app = QApplication.instance() or QApplication(sys.argv[:1])
        session = AtlasSession(output, locale=locales[0])
        logging.basicConfig(filename=str(output/'runner.log'), level=logging.WARNING, encoding='utf-8')
        window = AtlasTestWindow(session, locales, args.selection, args.limit, not args.no_live)
        window.show()
        with external_io_blocked():
            return app.exec()
    if args.selection == 'atlas188':
        # NEO 188: the 14.10 seven-language atlas (19 593 phrases), routing only.
        from jarvis.testing import atlas188
        with external_io_blocked():
            session = AtlasSession(output, locale=locales[0])
            logging.basicConfig(filename=str(output/'runner.log'), level=logging.WARNING, encoding='utf-8')
            results = []
            try:
                for i, (row, phrase) in enumerate(atlas188.cases(atlas188.load(), locales)):
                    if args.limit and i >= args.limit: break
                    try:
                        results.append(atlas188.check(session, row, phrase))
                    except Exception as exc:
                        results.append(atlas188.Result(row['intent'], row['language'], phrase, 'ERROR', '', [], f'{type(exc).__name__}: {exc}'))
                    if (i+1) % 1000 == 0: print(f'{i+1} phrases checked', flush=True)
            except KeyboardInterrupt:
                session.stop.set()
            finally:
                session.finish()
            (output/'atlas188_results.json').write_text(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=1), encoding='utf-8')
            summary = atlas188.summarize(results)
            (output/'atlas188_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print('REPORT:', output)
        return int(any(r.status in {'ROUTE_MISMATCH', 'NO_ROUTE', 'EXCLUDED_VIOLATED', 'ERROR'} for r in results))
    with external_io_blocked():
        session = AtlasSession(output, locale=locales[0])
        logging.basicConfig(filename=str(output/'runner.log'), level=logging.WARNING, encoding='utf-8')
        try:
            for i, case in enumerate(atlas_cases(locales,args.selection)):
                if args.limit and i >= args.limit: break
                session.check(case)
                if (i+1) % 1000 == 0: print(f'{i+1} phrases checked',flush=True)
            if not args.no_live:
                for _ in session.live_fixtures(locales): pass
                for _ in session.file_fixtures(locales): pass
        except KeyboardInterrupt:
            session.stop.set()
        finally:
            summary = session.finish()
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('REPORT:', output)
    return int(any(summary['counts'].get(key,0) for key in
                   ('ROUTE_MISMATCH','UNEXPECTED_ACTION','INVALID_ARGUMENTS','ERROR','LIVE_FAIL','CLEANUP_FAIL')))


if __name__ == '__main__':
    raise SystemExit(main())
