from contextlib import ExitStack
import ctypes
import os
import stat
import subprocess
import sys

import pytest

from app.storage import FTPStorage, NAS, create_remote
from remote_servers import sftp_server, webdav_server


@pytest.mark.parametrize('protocol', ['ftp', 'sftp', 'webdav'])
@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_download_permissions_match_normal_files_while_partials_stay_private(environment, monkeypatch, protocol, mode):
    env = environment
    with ExitStack() as stack:
        profile = env['profile'] if protocol == 'ftp' else stack.enter_context(
            (sftp_server if protocol == 'sftp' else webdav_server)(env['remote']))
        source = env['remote'] / '電影.mp4'
        payload = os.urandom(400000)
        source.write_bytes(payload)
        reference = env['nas'] / 'existing.txt'
        reference.write_text('normal existing file')
        expected_mode = stat.S_IMODE(reference.stat().st_mode)
        remote = create_remote(profile)
        adapter = type(remote)
        remote.close()
        original = adapter.download
        observed = []

        def download(self, path, callback):
            def receive(data):
                callback(data)
                partial = next(env['nas'].glob('.ftn-*.part'))
                observed.append(stat.S_IMODE(partial.stat().st_mode))
                assert not (env['nas'] / source.name).exists()
            original(self, path, receive)
        monkeypatch.setattr(adapter, 'download', download)
        job_id = env['worker'].submit(profile, 'download', mode, ['/' + source.name], '/')['id']
        env['worker'].execute(env['store'].job(job_id))
        result = env['store'].job(job_id)
        assert result['status'] == 'succeeded', result['error']
        target = env['nas'] / source.name
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == expected_mode
        assert result['files'][0]['destination_mode'] == expected_mode
        assert observed and set(observed) == {0o600}
        assert source.exists() == (mode == 'copy')
        assert not list(env['nas'].glob('.ftn-*'))


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('published', [False, True])
def test_staged_download_recovery_restores_permissions(environment, legacy, published):
    env = environment
    (env['remote'] / 'file').write_bytes(b'complete file')
    reference = env['nas'] / 'existing'
    reference.touch()
    expected_mode = stat.S_IMODE(reference.stat().st_mode)
    worker, store = env['worker'], env['store']
    job_id = worker.submit(env['profile'], 'download', 'move', ['/file'], '/')['id']
    remote = FTPStorage(env['profile'])
    try:
        job = worker.plan(store.job(job_id), remote)
    finally:
        remote.close()
    temp = f'/.ftn-{job_id}-0.part'
    fd, mode = worker.nas.create_download_temp(temp)
    with os.fdopen(fd, 'wb') as file:
        file.write(b'complete file')
    job['files'][0]['phase'] = 'staged'
    if not legacy:
        job['files'][0]['destination_mode'] = mode
    if published:
        os.link(worker.nas.local(temp), env['nas'] / 'file')
    store.patch(job_id, files=job['files'])
    worker.execute(store.job(job_id))
    result = store.job(job_id)
    assert result['status'] == 'succeeded', result['error']
    assert stat.S_IMODE((env['nas'] / 'file').stat().st_mode) == expected_mode
    assert not (env['remote'] / 'file').exists()


def test_permission_failure_preserves_move_source_and_can_retry_staged_file(environment, monkeypatch):
    env = environment
    source = env['remote'] / 'file'
    source.write_bytes(b'original')
    original = NAS.finish_download_permissions
    def denied(*args): raise PermissionError('chmod denied')
    monkeypatch.setattr(NAS, 'finish_download_permissions', denied)
    worker, store = env['worker'], env['store']
    job_id = worker.submit(env['profile'], 'download', 'move', ['/file'], '/')['id']
    worker.execute(store.job(job_id))
    result = store.job(job_id)
    assert result['status'] == 'failed'
    assert result['files'][0]['phase'] == 'staged'
    assert source.read_bytes() == b'original'
    assert not (env['nas'] / 'file').exists()
    partial = next(env['nas'].glob('.ftn-*.part'))
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    monkeypatch.setattr(NAS, 'finish_download_permissions', original)
    result['files'][0]['error'] = ''
    store.patch(job_id, files=result['files'], status='queued')
    worker.execute(store.job(job_id))
    assert store.job(job_id)['status'] == 'succeeded'
    assert not source.exists()
    assert (env['nas'] / 'file').read_bytes() == b'original'


def test_file_created_during_permission_restore_is_not_overwritten(environment, monkeypatch):
    env = environment
    source = env['remote'] / 'file'
    source.write_bytes(b'new data')
    original = NAS.finish_download_permissions
    def concurrent_file(self, path, mode=None):
        original(self, path, mode)
        (env['nas'] / 'file').write_bytes(b'old data')
    monkeypatch.setattr(NAS, 'finish_download_permissions', concurrent_file)
    job_id = env['worker'].submit(env['profile'], 'download', 'move', ['/file'], '/')['id']
    env['worker'].execute(env['store'].job(job_id))
    result = env['store'].job(job_id)
    assert result['status'] == 'failed'
    assert source.read_bytes() == b'new data'
    assert (env['nas'] / 'file').read_bytes() == b'old data'


def test_crash_partial_is_recreated_with_normal_creation_mode(tmp_path):
    nas = NAS(tmp_path)
    partial = tmp_path / '.ftn-crash.part'
    partial.write_bytes(b'incomplete')
    partial.chmod(0o600)
    reference = tmp_path / 'reference'
    reference.touch()
    fd, mode = nas.create_download_temp('/.ftn-crash.part')
    os.close(fd)
    assert mode == stat.S_IMODE(reference.stat().st_mode)
    assert partial.read_bytes() == b''
    nas.finish_download_permissions('/.ftn-crash.part', mode)
    assert stat.S_IMODE(partial.stat().st_mode) == mode


def test_private_umask_is_respected_without_changing_process_umask(tmp_path):
    script = '''
import os, sys
from pathlib import Path
from app.storage import NAS
os.umask(0o077)
nas = NAS(Path(sys.argv[1]))
fd, mode = nas.create_download_temp('/.ftn-private.part')
os.close(fd)
nas.finish_download_permissions('/.ftn-private.part', mode)
assert mode == 0o600
assert nas.local('/.ftn-private.part').stat().st_mode & 0o777 == 0o600
assert os.umask(0o077) == 0o077
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path)], check=True)


def test_default_acl_matches_normal_file_after_private_download(tmp_path):
    try:
        acl = ctypes.CDLL('libacl.so.1')
    except OSError:
        pytest.skip('POSIX ACL library unavailable')
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    acl.acl_get_file.restype = ctypes.c_void_p
    acl.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    acl.acl_to_text.restype = ctypes.c_void_p
    acl.acl_free.argtypes = [ctypes.c_void_p]
    definition = acl.acl_from_text(b'u::rwx,g::r-x,g:100:r-x,m::r-x,o::---')
    try:
        if acl.acl_set_file(os.fsencode(tmp_path), 0x4000, definition) != 0:
            pytest.skip('Filesystem does not support default ACLs')
    finally:
        acl.acl_free(definition)
    reference = tmp_path / 'normal'
    reference.touch()
    nas = NAS(tmp_path)
    fd, mode = nas.create_download_temp('/.ftn-acl.part')
    os.close(fd)
    nas.finish_download_permissions('/.ftn-acl.part', mode)
    def access_acl(path):
        value = acl.acl_get_file(os.fsencode(path), 0x8000)
        assert value
        text = acl.acl_to_text(value, None)
        try:
            assert text
            return ctypes.string_at(text)
        finally:
            acl.acl_free(text)
            acl.acl_free(value)
    assert access_acl(tmp_path / '.ftn-acl.part') == access_acl(reference)


def test_repair_command_changes_only_selected_0600_files(tmp_path):
    old = tmp_path / '下載 電影.mp4'
    old.write_bytes(b'existing content stays intact')
    old.chmod(0o600)
    normal = tmp_path / 'normal'
    normal.touch()
    expected = stat.S_IMODE(normal.stat().st_mode)
    untouched = tmp_path / 'untouched'
    untouched.touch()
    untouched.chmod(0o600)
    custom = tmp_path / 'custom'
    custom.touch()
    custom.chmod(0o640)
    result = subprocess.run([sys.executable, '-m', 'app.repair_permissions', '/' + old.name, '/custom'],
                            env=dict(os.environ, NAS_ROOT=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(old.stat().st_mode) == expected
    assert old.read_bytes() == b'existing content stays intact'
    assert stat.S_IMODE(untouched.stat().st_mode) == 0o600
    assert stat.S_IMODE(custom.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.ftn-*'))


@pytest.mark.parametrize('path', ['/../outside', '/link', '/', '/.ftn-active.part'])
def test_repair_command_rejects_unsafe_or_incomplete_targets(tmp_path, path):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    outside.chmod(0o600)
    (tmp_path / 'link').symlink_to(outside)
    partial = tmp_path / '.ftn-active.part'
    partial.touch()
    partial.chmod(0o600)
    result = subprocess.run([sys.executable, '-m', 'app.repair_permissions', path],
                            env=dict(os.environ, NAS_ROOT=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 1
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
