"""Small, allowlisted voice diagnostics. Never persist raw URLs, paths or output."""
from __future__ import annotations
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import ssl
import socket
import re
import threading
import time
from urllib.error import HTTPError, URLError
from .model_store import VoiceModelError, Cancelled, safe_path

STAGES = frozenset(('model_manifest','model_download','model_check','model_delete','runtime',
                    'runtime_python','runtime_venv','runtime_torch','runtime_qwen','runtime_probe','speech'))
ACTIONS = frozenset(('download','check','delete','bootstrap','speech'))
CODES = frozenset(('manifest_invalid','manifest_incomplete','unsafe_path','deleted','busy','missing_or_damaged',
                   'checksum_failed','changed_during_check','size_mismatch','invalid_range','download_failed',
                   'https_required','unexpected_encoding','runtime_failed','runtime_timeout','runtime_missing',
                   'runtime_python_missing','synthesis_failed','synthesis_timeout','operation_failed','cancelled'))
CATEGORIES = frozenset(('unknown','http_error','connection_error','timeout','certificate_error','permission_denied',
                        'disk_full','memory_error','missing_module','missing_native_library','dependency_conflict',
                        'no_matching_distribution','build_failed','process_failed','process_cleanup_timeout','cancelled',
                        'tls_error','proxy_error','dns_error','connect_timeout','read_timeout','incompatible_python',
                        'incompatible_wheel','missing_build_tool'))
ERROR_TYPES = frozenset(('VoiceModelError','RuntimeFailure','Cancelled','HTTPError','URLError','TimeoutError',
                         'PermissionError','FileNotFoundError','ConnectionError','OSError','MemoryError',
                         'SSLError','SSLCertVerificationError','ModuleNotFoundError','ImportError','RuntimeError',
                         'ValueError','BrokenPipeError','JSONDecodeError','TimeoutExpired'))
logger = logging.getLogger(__name__)
_FILE_LOCK = threading.Lock()
MAX_LOG_BYTES = 64 * 1024


class RuntimeFailure(VoiceModelError):
    def __init__(self, code='runtime_failed', *, stage='runtime', category='process_failed', returncode=None, errno=None, winerror=None, http_status=None):
        super().__init__(code if code in CODES else 'runtime_failed')
        self.stage = stage if stage in STAGES else 'runtime'
        self.category = category if category in CATEGORIES else 'unknown'
        self.returncode = returncode if type(returncode) is int and -(2**31) <= returncode < 2**32 else None
        self.errno = _number(errno,-65535,65535)
        self.winerror = _number(winerror,0,65535)
        self.http_status = _number(http_status,100,599)


def _number(value, low=0, high=2**32-1):
    return value if type(value) is int and low <= value <= high else None


def summarize_failure(error, *, action, stage):
    code = str(error) if isinstance(error, VoiceModelError) and str(error) in CODES else 'operation_failed'
    category = 'unknown'
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(error, Cancelled):category='cancelled'
    elif isinstance(error, HTTPError):category='http_error'
    elif isinstance(cause, ssl.SSLCertVerificationError):category='certificate_error'
    elif isinstance(cause, ssl.SSLError):category='tls_error'
    elif isinstance(cause, socket.gaierror):category='dns_error'
    elif isinstance(cause, TimeoutError):category='timeout'
    elif isinstance(cause, PermissionError):category='permission_denied'
    elif isinstance(cause, MemoryError):category='memory_error'
    elif isinstance(cause, OSError) and getattr(cause,'errno',None)==errno.ENOSPC:category='disk_full'
    elif isinstance(error, (URLError,ConnectionError)):category='connection_error'
    elif isinstance(cause, ModuleNotFoundError):category='missing_module'
    elif isinstance(cause, ImportError):category='missing_native_library'
    inferred_numbers={}
    if isinstance(error,URLError) and not isinstance(error,HTTPError) and category=='connection_error':
        classifier=OutputClassifier()
        classifier.feed(str(cause)[:4096].encode('utf-8','replace'))
        if classifier.category!='unknown':category=classifier.category
        inferred_numbers=classifier.numbers.copy();classifier.finish()
    if isinstance(error, RuntimeFailure):
        stage,category=error.stage,error.category
    result={'action':action if action in ACTIONS else 'download',
            'stage':stage if stage in STAGES else 'model_download','code':code,
            'category':category,'error_type':type(error).__name__ if type(error).__name__ in ERROR_TYPES else 'OtherError'}
    for key,value,low,high in (
        ('http_status',getattr(error,'code',None) if isinstance(error,HTTPError) else getattr(error,'http_status',inferred_numbers.get('http_status')),100,599),
        ('errno',getattr(cause,'errno',inferred_numbers.get('errno')),-65535,65535),('winerror',getattr(cause,'winerror',inferred_numbers.get('winerror')),0,65535),
        ('returncode',error.returncode if isinstance(error,RuntimeFailure) else None,-(2**31),2**32-1)):
        number=_number(value,low,high)
        if number is not None:result[key]=number
    return result


def record_failure(profile, error, *, action, stage):
    result=summarize_failure(error,action=action,stage=stage)
    if result['category']=='cancelled':return result
    # This is the only text sent to any log: fixed keys, enums and numeric codes.
    payload=dict(result,time=int(time.time()))
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
    try:logger.warning('Response voice failure %s',encoded)
    except Exception:pass
    try:
        target=safe_path(profile,'logs/response_voice_failures.jsonl')
        safe_path(profile,'logs/response_voice_failures.jsonl.1')
        target.parent.mkdir(parents=True,exist_ok=True)
        with _FILE_LOCK:
            handler=RotatingFileHandler(target,maxBytes=MAX_LOG_BYTES,backupCount=1,encoding='utf-8')
            try:
                handler.setFormatter(logging.Formatter('%(message)s'))
                handler.handleError=lambda _record:None
                handler.emit(logging.LogRecord(__name__,logging.WARNING,'',0,encoded,(),None))
            finally:handler.close()
    except Exception:
        # Failure to write diagnostics must not replace the original operation outcome.
        pass
    return result


def stage_event(action, stage):
    if action in ACTIONS and stage in STAGES:
        logger.info('Response voice stage action=%s stage=%s',action,stage)


class OutputClassifier:
    """Drain arbitrary process output with bounded RAM; retain only an enum hint."""
    PATTERNS = (
        ('disk_full',('no space left on device','not enough space on the disk','errno 28','winerror 112')),
        ('memory_error',('memoryerror','cannot allocate memory','out of memory','winerror 1455','paging file is too small')),
        ('permission_denied',('permission denied','access is denied','winerror 5')),
        ('certificate_error',('certificate_verify_failed','certificate verify failed')),
        ('tls_error',('sslerror','tlsv1_alert','wrong_version_number','ssl: unexpected_eof')),
        ('proxy_error',('proxyerror','cannot connect to proxy','tunnel connection failed','proxy authentication required')),
        ('dns_error',('name or service not known','getaddrinfo failed','name resolution','nodename nor servname')),
        ('connect_timeout',('connecttimeout','connection timed out','connect timeout')),
        ('read_timeout',('readtimeout','read timed out','read timeout')),
        ('incompatible_python',('requires a different python','require a different python version','requires-python')),
        ('incompatible_wheel',('not a supported wheel on this platform','not a valid win32 application','winerror 193')),
        ('no_matching_distribution',('no matching distribution found','could not find a version that satisfies')),
        ('dependency_conflict',('resolutionimpossible','conflicting dependencies')),
        ('missing_build_tool',('microsoft visual c++ 14.','unable to find vcvarsall','rust compiler not found',
                               'cargo, the rust package manager, is not installed','cmake is not installed','ninja: command not found')),
        ('missing_native_library',('dll load failed','winerror 126','winerror 127','winerror 1114',
                                   'cannot load library','dll initialization routine failed','portaudio library not found')),
        ('missing_module',('modulenotfounderror','no module named')),
        ('build_failed',('failed building wheel','subprocess-exited-with-error')),
        ('timeout',('timed out',)),
        ('connection_error',('failed to establish a new connection','connection refused')),
    )
    def __init__(self):
        self.category='unknown';self._tail=b'';self._rank=len(self.PATTERNS);self.numbers={}
    def feed(self, chunk):
        if not isinstance(chunk,bytes):chunk=bytes(chunk)
        # The reader requests at most 4096 bytes. Bound even a malformed injected reader.
        for offset in range(0,len(chunk),4096):
            data=self._tail+chunk[offset:offset+4096]
            text=data.decode('utf-8','replace').casefold()
            for rank,(category,patterns) in enumerate(self.PATTERNS):
                if rank<self._rank and any(pattern in text for pattern in patterns):
                    self.category,self._rank=category,rank
            for key,pattern,low,high in (
                ('winerror',r'\[winerror ([0-9]{1,5})\]',0,65535),
                ('errno',r'\[errno (-?[0-9]{1,5})\]',-65535,65535),
                ('http_status',r'\b(?:http(?: error| status)?|status code)[: =]+([1-5][0-9]{2})\b',100,599)):
                match=re.search(pattern,text)
                if match:
                    value=int(match.group(1))
                    if low<=value<=high:self.numbers[key]=value
            self._tail=data[-256:]
    def finish(self):self._tail=b''
