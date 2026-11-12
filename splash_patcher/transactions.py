"""Durable backups, staged multi-JAR updates, rollback, and crash recovery."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

import psutil

from .discovery import Installation
from .patcher import patch_jar


def digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.manifest-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def jar_path(root: Path, relative: str) -> Path:
    path = root / relative
    resolved = path.resolve(strict=True)
    if (not resolved.is_relative_to((root / 'lib').resolve()) or path.is_symlink()
            or resolved.suffix.lower() != '.jar' or not resolved.is_file()):
        raise ValueError(f'Invalid installation JAR: {relative}')
    # Reject junctions and symlinked parent paths as well.
    if path.absolute() != resolved:
        raise ValueError(f'JAR path contains a symlink/junction: {relative}')
    return resolved


def ensure_closed(ide: Installation) -> None:
    """Inspect executable paths; fail closed for inaccessible matching launchers."""
    for process in psutil.process_iter(['pid', 'name']):
        if process.pid == os.getpid():
            continue
        name = (process.info['name'] or '').lower()
        try:
            exe = process.exe()
            if exe and Path(exe).resolve().is_relative_to(ide.root):
                raise ValueError(f'Close {ide.name} first (process {process.pid}: {name})')
        except psutil.AccessDenied:
            if name in ide.launchers:
                raise ValueError(f'Cannot inspect {name}; close the IDE before continuing')
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue


def compatible_original(original: Path, installed: Path) -> None:
    """Imported originals may differ only in PNG contents, with identical layout."""
    with zipfile.ZipFile(original) as a, zipfile.ZipFile(installed) as b:
        if a.testzip() or b.testzip() or original.stat().st_size != installed.stat().st_size:
            raise ValueError('Original backup is corrupt or has a different archive size')
        for x, y in zip(a.infolist(), b.infolist(), strict=True):
            if (x.filename, x.header_offset, x.compress_size, x.compress_type) != (
                    y.filename, y.header_offset, y.compress_size, y.compress_type):
                raise ValueError('Original backup does not match this JAR layout')
            if not x.filename.lower().endswith('.png') and a.read(x) != b.read(y):
                raise ValueError(f'Original backup differs in non-image resource {x.filename}')


@contextmanager
def workspace_lock(folder: Path):
    """An OS lock releases automatically if either process crashes."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.lock').open('a+b') as f:
        f.seek(0)
        if not f.read(1):
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another operation is using this installation workspace') from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


class Manager:
    def __init__(self, ide: Installation, workspace: Path):
        self.ide = ide
        key = hashlib.sha256(os.path.normcase(str(ide.root)).encode()).hexdigest()[:16]
        self.folder = workspace.resolve() / 'installations' / key
        self.folder.mkdir(parents=True, exist_ok=True)
        self.manifest = self.folder / 'manifest.json'
        self.pending = self.folder / 'pending.json'

    def state(self) -> dict:
        if self.manifest.exists():
            state = read_json(self.manifest)
            if state['root'] != str(self.ide.root) or state['build'] != self.ide.build:
                raise ValueError('IDE build changed. Keep this workspace for the old build; choose a new workspace.')
            return state
        return {'schema': 1, 'root': str(self.ide.root), 'build': self.ide.build, 'jars': {}}

    def prepare(self, changes: dict[str, dict[str, bytes]] | None,
                originals: dict[str, Path] | None = None,
                confirmed_originals: bool = False) -> Path:
        """None means restore all known backups. Preparation never changes installed JARs."""
        with workspace_lock(self.folder):
            if self.pending.exists():
                raise ValueError('An unfinished operation exists. Use Recover interrupted operation first.')
            ensure_closed(self.ide)
            state = self.state()
            selected = sorted(changes if changes is not None else state['jars'])
            if not selected:
                raise ValueError('No queued replacements or backups to restore')
            operation = self.folder / 'operations' / uuid.uuid4().hex
            operation.mkdir(parents=True)
            before_state = json.loads(json.dumps(state))
            items = []
            for index, relative in enumerate(selected):
                target = jar_path(self.ide.root, relative)
                current_hash = digest(target)
                record = state['jars'].get(relative)
                if record:
                    backup = self.folder / record['backup']
                    if digest(backup) != record['original_sha256']:
                        raise ValueError(f'Original backup was changed: {backup}')
                    if current_hash != record['current_sha256']:
                        raise ValueError(f'Installed JAR changed outside this app: {relative}. Rescan after resolving the change.')
                else:
                    source = (originals or {}).get(relative)
                    if source is None:
                        if not confirmed_originals:
                            raise ValueError(f'Confirm an untouched installation or select an original backup for {relative}')
                        source = target
                    compatible_original(source, target)
                    backup = self.folder / 'originals' / (uuid.uuid4().hex + '.jar')
                    backup.parent.mkdir(exist_ok=True)
                    with source.open('rb') as src, backup.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if digest(source) != digest(backup):
                        raise ValueError('Original changed while backing it up')
                    record = {'backup': str(backup.relative_to(self.folder)),
                              'original_sha256': digest(backup), 'current_sha256': current_hash}
                    state['jars'][relative] = record
                before = operation / f'{index}.before.jar'
                shutil.copyfile(target, before)
                if digest(before) != current_hash:
                    raise ValueError('Installed JAR changed while staging')
                after = operation / f'{index}.after.jar'
                if changes is None:
                    shutil.copyfile(backup, after)
                else:
                    replacements = changes[relative]
                    patch_jar(before, after, replacements)
                    image_folder = operation / f'{index}.images'
                    image_folder.mkdir()
                    image_map = {}
                    for number, (entry, png) in enumerate(replacements.items()):
                        image_file = image_folder / f'{number}.png'
                        image_file.write_bytes(png)
                        image_map[entry] = image_file.name
                    write_json(image_folder / 'entries.json', image_map)
                after_hash = digest(after)
                record['current_sha256'] = after_hash
                items.append({'relative': relative, 'before': str(before), 'after': str(after),
                              'before_sha256': current_hash, 'after_sha256': after_hash})
            # Persist newly created originals even if UAC is canceled or preparation is abandoned.
            baseline = json.loads(json.dumps(state))
            for item in items:
                baseline['jars'][item['relative']]['current_sha256'] = item['before_sha256']
            write_json(self.manifest, baseline)
            plan = {'schema': 1, 'root': str(self.ide.root), 'build': self.ide.build,
                    'folder': str(self.folder), 'items': items, 'before_state': baseline,
                    'after_state': state, 'status': 'prepared'}
            plan_path = operation / 'plan.json'
            write_json(plan_path, plan)
            write_json(self.pending, {'plan': str(plan_path)})
            return plan_path

    def recover(self) -> Path:
        if not self.pending.exists():
            raise ValueError('No interrupted operation exists')
        return Path(read_json(self.pending)['plan'])


def atomic_copy(source: Path, target: Path, expected_hash: str) -> None:
    """Same-directory replacement. The original path remains intact until replace succeeds."""
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.splash-', suffix='.jar')
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as dst, source.open('rb') as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copymode(target, tmp)
        if digest(tmp) != expected_hash:
            raise ValueError('Staged file checksum changed')
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def execute(plan_path: Path, recover: bool = False) -> None:
    plan_path = plan_path.resolve(strict=True)
    plan = read_json(plan_path)
    ide = Installation.load(Path(plan['root']))
    manager_folder = Path(plan['folder']).resolve(strict=True)
    if ide.build != plan['build'] or not plan_path.is_relative_to(manager_folder / 'operations'):
        raise ValueError('Invalid operation path or IDE build changed')
    pending = manager_folder / 'pending.json'
    with workspace_lock(manager_folder):
        if not pending.exists() or Path(read_json(pending)['plan']).resolve() != plan_path:
            raise ValueError('Operation is no longer pending')
        ensure_closed(ide)
        items = plan['items']
        if len({item['relative'] for item in items}) != len(items):
            raise ValueError('Duplicate JAR destinations')
        for item in items:
            target = jar_path(ide.root, item['relative'])
            for field in ('before', 'after'):
                file = Path(item[field]).resolve(strict=True)
                if not file.is_relative_to(plan_path.parent) or digest(file) != item[field + '_sha256']:
                    raise ValueError('Staged file checksum or path changed')
            allowed = (item['before_sha256'], item['after_sha256']) if recover else (item['before_sha256'],)
            if digest(target) not in allowed:
                raise ValueError(f'Installed JAR changed: {item["relative"]}; preserved pending operation for recovery')
        if recover:
            rollback(plan, ide, manager_folder, plan_path)
            return
        plan['status'] = 'installing'
        write_json(plan_path, plan)
        try:
            for item in items:
                target = jar_path(ide.root, item['relative'])
                if digest(target) != item['before_sha256']:
                    raise ValueError('Installed file changed during installation')
                atomic_copy(Path(item['after']), target, item['after_sha256'])
            write_json(manager_folder / 'manifest.json', plan['after_state'])
            plan['status'] = 'complete'
            write_json(plan_path, plan)
            pending.unlink()
        except Exception as failure:
            try:
                rollback(plan, ide, manager_folder, plan_path)
            except Exception as rollback_error:
                raise RuntimeError(f'{failure}\nRollback incomplete: {rollback_error}. Use Recover interrupted operation.') from failure
            raise RuntimeError(f'{failure}\nAll installed changes were rolled back.') from failure


def rollback(plan, ide, folder, plan_path):
    for item in reversed(plan['items']):
        target = jar_path(ide.root, item['relative'])
        actual = digest(target)
        if actual == item['before_sha256']:
            continue
        if actual != item['after_sha256'] or digest(Path(item['before'])) != item['before_sha256']:
            raise ValueError(f'Unexpected file state during rollback: {target}')
        atomic_copy(Path(item['before']), target, item['before_sha256'])
    write_json(folder / 'manifest.json', plan['before_state'])
    plan['status'] = 'rolled_back'
    write_json(plan_path, plan)
    (folder / 'pending.json').unlink()


def clear_cache(ide: Installation, override: Path | None = None) -> str:
    """Called by the unelevated GUI, preserving the invoking user's cache location."""
    if override is None:
        if os.name != 'nt' or not os.environ.get('LOCALAPPDATA'):
            return 'Automatic cache detection is Windows-only; choose a custom splash folder if needed.'
        cache = Path(os.environ['LOCALAPPDATA']) / 'JetBrains' / ide.data_directory / 'splash'
    else:
        cache = override
    if cache.name.lower() != 'splash' or cache.is_symlink():
        raise ValueError('Cache override must be a real directory named splash')
    if not cache.exists():
        return f'No splash cache found at {cache}'
    renamed = cache.with_name('splash.backup-' + uuid.uuid4().hex[:12])
    cache.rename(renamed)
    return f'Splash cache preserved as {renamed}'
