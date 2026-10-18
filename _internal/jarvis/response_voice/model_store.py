"""Pinned mixed publisher identities: Git-object SHA1 for ordinary files, SHA256 for weights."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

MODEL_ID = 'Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice'
REVISION = '85e237c12c027371202489a0ec509ded67b5e4b5'
MANIFEST_PATH = Path(__file__).resolve().parents[2] / 'config' / 'response_voice_manifest.json'
INTEGRITY_POLICY = "hf-pinned-mixed-v1"
CHUNK_BYTES = 1024 * 1024
# Already verified publisher identities, pinned here as well as in the shipped manifest.
# Weight blob IDs identify LFS pointer objects and are deliberately not accepted.
PINNED_FILES = {'README.md': (3263, 'git', 'git-blob-sha1', 'ed613da23a5e133a506b13b79055854938b9e67c'),
 'config.json': (4908, 'git', 'git-blob-sha1', '5e8d5fc100536984e3a89bd87ef08e59e322b29b'),
 'generation_config.json': (245, 'git', 'git-blob-sha1', '1872b16d4535564abf2db5d6debe6cddd82b7f2e'),
 'merges.txt': (1671839, 'git', 'git-blob-sha1', '20024bfe7c83998e9aeaf98a0cd6a2ce6306c2f0'),
 'model.safetensors': (1811626576,
                       'lfs',
                       'sha256',
                       'bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb'),
 'preprocessor_config.json': (127, 'git', 'git-blob-sha1', '0525dd953bb9241912f7147666f0d535165d5d4f'),
 'speech_tokenizer/config.json': (2336, 'git', 'git-blob-sha1', '06cc8dc4c5ec8a1929086b71b98c313020d9268b'),
 'speech_tokenizer/configuration.json': (76,
                                         'git',
                                         'git-blob-sha1',
                                         'ab58e2eaf53cd14a1a2a7527d9261ceea93a24cd'),
 'speech_tokenizer/model.safetensors': (682293092,
                                        'lfs',
                                        'sha256',
                                        '836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258'),
 'speech_tokenizer/preprocessor_config.json': (234,
                                               'git',
                                               'git-blob-sha1',
                                               'ba40914f4f49ab98a8ca545d4892ef7291a39592'),
 'tokenizer_config.json': (7344, 'git', 'git-blob-sha1', '6ff9fd60cc623bb54bbd603cbd418c97a11528d7'),
 'vocab.json': (2776833, 'git', 'git-blob-sha1', '4783fe10ac3adce15ac8f358ef5462739852c569')}


class VoiceModelError(RuntimeError):
    pass


class Cancelled(VoiceModelError):
    pass


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))


def safe_path(root, name):
    path = PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or any(p in ('..', '.') for p in path.parts):
        raise VoiceModelError('unsafe_path')
    root = Path(root)
    target = root.joinpath(*path.parts)
    for parent in [root, target, *target.parents]:
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise VoiceModelError('unsafe_path')
    if not target.resolve().is_relative_to(root.resolve()):
        raise VoiceModelError('unsafe_path')
    return target


def validate_manifest(manifest, *, expected_files=None, model_id=MODEL_ID, revision=REVISION, policy=INTEGRITY_POLICY):
    """Strict policy validation; expected_files is used only by synthetic unit fixtures."""
    expected = PINNED_FILES if expected_files is None else expected_files
    if not isinstance(manifest, dict):
        raise VoiceModelError('manifest_invalid')
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 2
            or manifest.get('integrity_policy') != policy
            or manifest.get('model_id') != model_id or manifest.get('revision') != revision):
        raise VoiceModelError('manifest_invalid')
    # No implicit schema-1 fallback or conflicting legacy integrity switches.
    if any(key in manifest for key in ('hashes_complete', 'sha256', 'verification_policy')):
        raise VoiceModelError('manifest_invalid')
    files = manifest.get('files')
    if not isinstance(files, list) or len(files) != len(expected):
        raise VoiceModelError('manifest_invalid')
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'path', 'size', 'storage', 'integrity'}:
            raise VoiceModelError('manifest_invalid')
        name = entry['path']
        if (not isinstance(name, str) or not name or name not in expected or name in seen
                or name.startswith('/') or '\\' in name or ':' in name
                or any(part in ('', '.', '..') for part in name.split('/'))):
            raise VoiceModelError('manifest_invalid')
        seen.add(name)
        if type(entry['size']) is not int or entry['size'] <= 0:
            raise VoiceModelError('manifest_invalid')
        integrity = entry['integrity']
        if not isinstance(integrity, dict) or set(integrity) != {'algorithm', 'digest'}:
            raise VoiceModelError('manifest_invalid')
        storage, algorithm, digest = entry['storage'], integrity['algorithm'], integrity['digest']
        if (storage, algorithm) not in (('git', 'git-blob-sha1'), ('lfs', 'sha256')):
            raise VoiceModelError('manifest_invalid')
        # Even a synthetic fixture cannot interpret a weight's pointer SHA1 as weight integrity.
        if name.endswith('.safetensors') and (storage, algorithm) != ('lfs', 'sha256'):
            raise VoiceModelError('manifest_invalid')
        width = 64 if algorithm == 'sha256' else 40
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{' + str(width) + '}', digest):
            raise VoiceModelError('manifest_incomplete')
        if (entry['size'], storage, algorithm, digest) != expected[name]:
            raise VoiceModelError('manifest_invalid')
    if seen != set(expected) or type(manifest.get('total_bytes')) is not int:
        raise VoiceModelError('manifest_invalid')
    if manifest['total_bytes'] != sum(row[0] for row in expected.values()):
        raise VoiceModelError('manifest_invalid')
    return files


def stream_digest(stream, size, algorithm, cancel=None):
    """Hash exact bytes with a hard byte cap; never decode or normalize Git blobs."""
    if type(size) is not int or size < 0:
        raise VoiceModelError('size_mismatch')
    if algorithm == 'sha256':
        digest = hashlib.sha256()
    elif algorithm == 'git-blob-sha1':
        # Plain SHA1 is not Git's hardened collision-detecting implementation.
        digest = hashlib.sha1()
        digest.update(b'blob ' + str(size).encode('ascii') + b'\0')
    else:
        raise VoiceModelError('manifest_invalid')
    count = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled('cancelled')
        chunk = stream.read(min(CHUNK_BYTES, size - count + 1))
        if cancel is not None and cancel.is_set():
            raise Cancelled('cancelled')
        if not chunk:
            break
        count += len(chunk)
        if count > size:
            raise VoiceModelError('size_mismatch')
        digest.update(chunk)
    if count != size:
        raise VoiceModelError('size_mismatch')
    return digest.hexdigest()


def file_identity(path):
    info=path.stat()
    return (info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_dev,info.st_ino)


def _stat_identity(info):
    return (info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_dev,info.st_ino)


def verify_file(path, entry, cancel=None):
    """Return the identity actually hashed, refusing changes during verification."""
    if not path.is_file():
        raise VoiceModelError('missing_or_damaged')
    before=file_identity(path)
    if before[0]!=entry['size']:
        raise VoiceModelError('missing_or_damaged')
    with path.open('rb') as stream:
        if _stat_identity(os.fstat(stream.fileno()))!=before:
            raise VoiceModelError('changed_during_check')
        actual=stream_digest(stream,entry['size'],entry['integrity']['algorithm'],cancel)
        after_handle=_stat_identity(os.fstat(stream.fileno()))
    if before!=after_handle or before!=file_identity(path):
        raise VoiceModelError('changed_during_check')
    if actual!=entry['integrity']['digest']:
        raise VoiceModelError('checksum_failed')
    return before


def require_https(url):
    parsed = urlsplit(str(url))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise VoiceModelError('https_required')


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        require_https(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


class HTTPTransport:
    """Pinned HTTPS request, with downgrade rejection at every redirect and final URL."""
    def __init__(self, opener=None):
        self.opener = opener or build_opener(HTTPSOnlyRedirectHandler())

    def open(self, url, offset):
        require_https(url)
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        response = self.opener.open(Request(url, headers=headers), timeout=15)
        try:
            require_https(response.geturl())
            encoding = response.headers.get('Content-Encoding', 'identity').strip().casefold()
            if encoding not in ('', 'identity'):
                raise VoiceModelError('unexpected_encoding')
        except Exception:
            response.close()
            raise
        return response


class ModelStore:
    model_id = MODEL_ID
    revision = REVISION
    folder = 'qwen-response-voice'
    tombstone_name = 'response_voice.deleted.json'

    def __init__(self, profile, manifest=None, transport=None):
        self.profile = Path(profile)
        self.root = self.profile / 'models' / self.folder
        self.tombstone = self.profile / self.tombstone_name
        self.manifest = manifest if manifest is not None else load_manifest()
        self.transport = transport or HTTPTransport()
        self._lock = threading.Lock()
        self._verified_signature = None

    def verification_error(self):
        try:
            self._validate()
        except VoiceModelError as error:
            return str(error)
        return None

    def _validate(self):
        validate_manifest(self.manifest)
        for entry in self.manifest['files']:
            safe_path(self.root, entry['path'])

    def validate_before_download(self):
        """Caller-side preflight. Standard stores ship a pinned manifest."""
        self._validate()

    def signature(self):
        values = []
        for entry in self.manifest['files']:
            path = safe_path(self.root, entry['path'])
            values.append((entry['path'], *file_identity(path)))
        return tuple(values)

    def ready_cached(self):
        try:
            self._validate()
            return not self.tombstone.exists() and self._verified_signature is not None and self.signature() == self._verified_signature
        except (OSError, VoiceModelError):
            return False

    def check(self, cancel=None, progress=lambda **kw: None):
        self._validate()
        if self.tombstone.exists():
            raise VoiceModelError('deleted')
        self._verified_signature = None
        try:
            before = self.signature()
        except OSError:
            raise VoiceModelError("missing_or_damaged") from None
        done = 0
        for entry in self.manifest['files']:
            if cancel is not None and cancel.is_set():
                raise Cancelled('cancelled')
            path = safe_path(self.root, entry['path'])
            verify_file(path, entry, cancel)
            done += entry['size']
            progress(state='checking', done=done, total=self.manifest['total_bytes'], file=entry['path'])
        after = self.signature()
        if before != after:
            raise VoiceModelError("changed_during_check")
        self._verified_signature = after
        return {'state': 'verified', 'done': done, 'total': done, 'verification_mode': 'full_check'}

    @contextmanager
    def operation_lock(self):
        # Shared by the GUI and CMD preinstaller. A stale lock is reported, never stolen.
        import threading
        if getattr(self, '_lock_owner186', None) == threading.get_ident():
            # NEW185-TTS-02: the heavy store takes the lock before resolving its
            # manifest; the inherited download step runs inside the same claim.
            yield
            return
        if not self._lock.acquire(blocking=False):
            raise VoiceModelError('busy')
        fd = None
        lock = self.profile / 'response_voice.install.lock'
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode('ascii'))
            self._lock_owner186 = threading.get_ident()
            yield
        except FileExistsError:
            raise VoiceModelError('busy') from None
        finally:
            self._lock_owner186 = None
            try:
                if fd is not None:
                    try:
                        os.close(fd)
                    finally:
                        lock.unlink(missing_ok=True)
            finally:
                # NEO 187 (SRC-VOICE-LOCK-CLEANUP-01): the in-process mutex is
                # released even when close/unlink fail; their error still surfaces.
                self._lock.release()

    def download(self, cancel, progress=lambda **kw: None, *, explicit=False):
        # Must fail BEFORE network, runtime installation, or clearing a deletion marker.
        self._validate()
        if self.tombstone.exists() and not explicit:
            raise VoiceModelError('deleted')
        with self.operation_lock():
            # Deletion can complete after the optimistic check but before this lock.
            if self.tombstone.exists() and not explicit:
                raise VoiceModelError("deleted")
            if cancel.is_set():
                raise Cancelled("cancelled")
            if explicit:
                self.tombstone.unlink(missing_ok=True)
            self._verified_signature = None
            self.root.mkdir(parents=True, exist_ok=True)
            total = self.manifest['total_bytes']
            done = 0
            verified_identities = {}
            for entry in self.manifest['files']:
                if cancel.is_set():
                    raise Cancelled('cancelled')
                target = safe_path(self.root, entry['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file():
                    try:
                        progress(state='checking',done=done,total=total,file=entry['path'])
                        identity=verify_file(target,entry,cancel)
                    except Cancelled:
                        raise
                    except VoiceModelError:
                        pass
                    else:
                        verified_identities[entry['path']]=identity
                        done += entry['size']
                        continue
                part = safe_path(self.root, entry['path'] + '.part')
                offset = part.stat().st_size if part.is_file() else 0
                if offset > entry['size']:
                    part.unlink(); offset = 0
                if offset < entry['size']:
                    url = f'https://huggingface.co/{self.model_id}/resolve/{self.revision}/{entry["path"]}'
                    with self.transport.open(url, offset) as response:
                        status = getattr(response, 'status', 200)
                        if status == 206:
                            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                                                 str(response.headers.get("Content-Range", "")))
                            bounds = tuple(map(int, match.groups())) if match else None
                            # An open-ended Range request must cover exactly the pinned remainder.
                            if bounds != (offset, entry["size"] - 1, entry["size"]):
                                raise VoiceModelError("invalid_range")
                        elif status == 200:
                            offset = 0
                        else:
                            raise VoiceModelError('download_failed')
                        with part.open('ab' if offset else 'wb') as stream:
                            while True:
                                if cancel.is_set():
                                    raise Cancelled('cancelled')
                                chunk = response.read(min(CHUNK_BYTES, entry['size'] - offset + 1))
                                if cancel.is_set():
                                    raise Cancelled('cancelled')
                                if not chunk:
                                    break
                                if offset + len(chunk) > entry['size']:
                                    raise VoiceModelError('size_mismatch')
                                stream.write(chunk)
                                offset += len(chunk)
                                progress(state='downloading', done=done + offset, total=total, file=entry['path'])
                if cancel.is_set():
                    raise Cancelled('cancelled')
                if not part.is_file() or part.stat().st_size != entry['size']:
                    raise VoiceModelError('size_mismatch')
                try:
                    progress(state='checking',done=done,total=total,file=entry['path'])
                    identity=verify_file(part,entry,cancel)
                except Cancelled:
                    raise
                except VoiceModelError:
                    part.unlink(missing_ok=True)
                    raise
                if cancel.is_set():raise Cancelled('cancelled')
                if file_identity(part)!=identity:raise VoiceModelError('changed_during_check')
                os.replace(part,target)
                promoted=file_identity(target)
                # Atomic rename can update ctime; it must retain the verified file's
                # inode/device, byte count and mtime. Track the new ctime from here.
                if promoted[:2]!=identity[:2] or promoted[3:]!=identity[3:]:
                    raise VoiceModelError('changed_during_check')
                verified_identities[entry['path']]=promoted
                done += entry['size']
            if cancel.is_set():raise Cancelled('cancelled')
            progress(state='checking',done=done,total=total,file=self.manifest['files'][-1]['path'])
            if cancel.is_set():raise Cancelled('cancelled')
            self._validate()
            expected=tuple((entry['path'],*verified_identities[entry['path']]) for entry in self.manifest['files'])
            try:
                current=self.signature()
            except OSError:
                raise VoiceModelError('changed_during_check') from None
            if current!=expected:raise VoiceModelError('changed_during_check')
            # Every file was hashed in this locked operation. Do not read ~2.5GB a
            # second time. This receipt is process-local and is never persisted.
            self._verified_signature=current
            return {'state':'verified','done':done,'total':total,'verification_mode':'download_file_hashes'}

    def delete(self):
        with self.operation_lock():
            # Marker lives outside the model directory and survives partial deletion/failure.
            marker = safe_path(self.profile, self.tombstone_name.replace('.json', '.tmp'))
            safe_path(self.profile, self.tombstone_name)
            marker.write_text(json.dumps({'model_id': self.model_id, 'deleted_at': time.time()}), encoding='utf-8')
            os.replace(marker, self.tombstone)
            self._verified_signature = None
            safe_path(self.profile, 'models/' + self.folder)
            if self.root.exists():
                shutil.rmtree(self.root)
            return {'state': 'deleted', 'done': 0, 'total': self.manifest['total_bytes']}


# ---- NEO 185: heavy tier, Qwen3-TTS 1.7B CustomVoice -------------------------------------
HEAVY_MODEL_ID = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
RESOLVED_POLICY = 'hf-resolved-mixed-v1'
# A sanity bound, not a size claim: refuse a listing that would download far more than expected.
HEAVY_MAX_BYTES = 8 * 1024 ** 3
_SKIPPED_FILES = ('.gitattributes',)


def manifest_from_listing(listing, model_id=HEAVY_MODEL_ID):
    """Build a mixed-integrity manifest from a Hugging Face ``/api/models/<id>/revision/<rev>?blobs=true`` reply.

    Every file is pinned to the listing's commit: LFS weights by the publisher's SHA-256,
    ordinary files by their Git blob SHA-1, exactly as the shipped 0.6B manifest.
    """
    if not isinstance(listing, dict):
        raise VoiceModelError('manifest_invalid')
    revision = listing.get('sha')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise VoiceModelError('manifest_invalid')
    files = []
    for sibling in listing.get('siblings') or []:
        if not isinstance(sibling, dict):
            raise VoiceModelError('manifest_invalid')
        name = sibling.get('rfilename')
        if not isinstance(name, str) or name in _SKIPPED_FILES or name.endswith(('.png', '.jpg', '.jpeg', '.gif')):
            continue
        lfs = sibling.get('lfs')
        if isinstance(lfs, dict):
            entry = {'path': name, 'size': lfs.get('size'), 'storage': 'lfs',
                     'integrity': {'algorithm': 'sha256', 'digest': lfs.get('sha256')}}
        else:
            entry = {'path': name, 'size': sibling.get('size'), 'storage': 'git',
                     'integrity': {'algorithm': 'git-blob-sha1', 'digest': sibling.get('blobId')}}
        files.append(entry)
    files.sort(key=lambda row: str(row['path']))
    manifest = {'schema_version': 2, 'model_id': model_id, 'revision': revision,
                'total_bytes': sum(row['size'] for row in files if type(row['size']) is int),
                'integrity_policy': RESOLVED_POLICY, 'files': files}
    validate_resolved(manifest, model_id)
    return manifest


def validate_resolved(manifest, model_id=HEAVY_MODEL_ID):
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise VoiceModelError('manifest_unresolved')
    revision = manifest.get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise VoiceModelError('manifest_invalid')
    expected = {}
    for entry in manifest['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('integrity'), dict):
            raise VoiceModelError('manifest_invalid')
        expected[entry.get('path')] = (entry.get('size'), entry.get('storage'), entry['integrity'].get('algorithm'),
                                      entry['integrity'].get('digest'))
    if not any(str(name).endswith('.safetensors') for name in expected):
        raise VoiceModelError('manifest_invalid')
    files = validate_manifest(manifest, expected_files=expected, model_id=model_id, revision=revision, policy=RESOLVED_POLICY)
    if manifest['total_bytes'] > HEAVY_MAX_BYTES:
        raise VoiceModelError('manifest_invalid')
    return files


class HeavyModelStore(ModelStore):
    """Qwen3-TTS 1.7B: the file list and digests come from Hugging Face at download time.

    NEO cannot ship pinned digests for this model, so the first download fetches the
    publisher's listing for the current commit and pins every file to it (SHA-256 for
    weights, Git SHA-1 for small files). Later checks use that saved listing.
    """
    model_id = HEAVY_MODEL_ID
    folder = 'qwen-response-voice-1.7b'
    tombstone_name = 'response_voice_heavy.deleted.json'

    def __init__(self, profile, manifest=None, transport=None):
        self.manifest_path = Path(profile) / 'models' / (self.folder + '.manifest.json')
        if manifest is None:
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                manifest = {'total_bytes': 0, 'files': []}
        super().__init__(profile, manifest=manifest, transport=transport)
        self.revision = str(self.manifest.get('revision') or '')

    def _validate(self):
        validate_resolved(self.manifest, self.model_id)
        for entry in self.manifest['files']:
            safe_path(self.root, entry['path'])

    def validate_before_download(self):
        # NEO 187 (REC-TTS-CALLER-01): a not-yet-fetched file list is resolved
        # by download() itself under the install lock. Only that one case is
        # left to the store; a malformed saved listing still fails here.
        try:
            self._validate()
        except Cancelled:
            raise
        except VoiceModelError as error:
            if str(error) != 'manifest_unresolved':
                raise

    def resolve(self, cancel=None):
        url = f'https://huggingface.co/api/models/{self.model_id}/revision/main?blobs=true'
        with self.transport.open(url, 0) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if cancel is not None and cancel.is_set():
            raise Cancelled('cancelled')
        if len(raw) > 4 * 1024 * 1024:
            raise VoiceModelError('manifest_invalid')
        try:
            listing = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise VoiceModelError('manifest_invalid') from None
        manifest = manifest_from_listing(listing, self.model_id)
        for entry in manifest['files']:
            safe_path(self.root, entry['path'])
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        # NEW185-TTS-03: a fresh, exclusively created temporary file in the models
        # folder; an existing «.tmp» (or a link planted there) is never followed.
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix='.' + self.folder + '.', suffix='.manifest.tmp', dir=str(self.manifest_path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(manifest, ensure_ascii=False, indent=1))
            os.replace(temporary, self.manifest_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.manifest = manifest
        self.revision = manifest['revision']
        return manifest

    def download(self, cancel, progress=lambda **kw: None, *, explicit=False):
        if self.tombstone.exists() and not explicit:
            raise VoiceModelError('deleted')
        # NEW185-TTS-01: an operation cancelled before it started touches nothing.
        if cancel is not None and cancel.is_set():
            raise Cancelled('cancelled')
        # NEW185-TTS-02: claim the install lock BEFORE any request or manifest write.
        with self.operation_lock():
            if cancel is not None and cancel.is_set():
                raise Cancelled('cancelled')
            try:
                self._validate()
            except VoiceModelError:
                progress(state='checking', stage='model_manifest', done=0, total=0)
                self.resolve(cancel)
            return super().download(cancel, progress, explicit=explicit)

    def delete(self):
        result = super().delete()
        self.manifest_path.unlink(missing_ok=True)
        self.manifest = {'total_bytes': 0, 'files': []}
        return result
