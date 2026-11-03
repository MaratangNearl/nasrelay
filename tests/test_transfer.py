import os
import threading
import time

import pytest

from app.storage import NAS, FTPStorage, clean_path
from app.store import Store
from app.worker import TransferWorker, TERMINAL


def execute(env, direction, mode, sources, destination='/'):
    public = env['worker'].submit(env['profile'], direction, mode, sources, destination)
    env['worker'].execute(env['store'].job(public['id']))
    return env['store'].job(public['id'])


@pytest.mark.parametrize('direction', ['download', 'upload'])
@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_recursive_binary_and_empty_files(environment, direction, mode):
    env = environment
    source, target = (env['remote'], env['nas']) if direction == 'download' else (env['nas'], env['remote'])
    (source / '한글 폴더' / '빈 폴더').mkdir(parents=True)
    payload = os.urandom(700_123)
    (source / '한글 폴더' / '사진.bin').write_bytes(payload)
    (source / '한글 폴더' / 'empty.txt').touch()
    job = execute(env, direction, mode, ['/한글 폴더', '/한글 폴더/사진.bin'])
    assert job['status'] == 'succeeded', job['error']
    assert job['total_files'] == job['completed_files'] == 2
    assert job['completed_bytes'] == len(payload)
    assert (target / '한글 폴더' / '사진.bin').read_bytes() == payload
    assert (target / '한글 폴더' / 'empty.txt').stat().st_size == 0
    assert (target / '한글 폴더' / '빈 폴더').is_dir()
    assert (source / '한글 폴더').exists() == (mode == 'copy')
    assert not list(target.rglob('.ftn-*'))


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_collision_preserves_both_files_and_other_items_continue(environment, direction):
    env = environment
    source, target = (env['remote'], env['nas']) if direction == 'download' else (env['nas'], env['remote'])
    (source / 'same.txt').write_bytes(b'new file')
    (source / 'other.txt').write_bytes(b'other')
    (target / 'same.txt').write_bytes(b'old file')
    job = execute(env, direction, 'move', ['/same.txt', '/other.txt'])
    assert job['status'] == 'failed'
    assert job['completed_files'] == 1
    assert (target / 'same.txt').read_bytes() == b'old file'
    assert (source / 'same.txt').read_bytes() == b'new file'
    assert (target / 'other.txt').read_bytes() == b'other'
    assert not (source / 'other.txt').exists()


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_delete_failure_retry_does_not_recopy(environment, direction, monkeypatch):
    env = environment
    source, target = (env['remote'], env['nas']) if direction == 'download' else (env['nas'], env['remote'])
    (source / 'data').write_bytes(b'abc')
    adapter = FTPStorage if direction == 'download' else NAS
    original = adapter.remove
    def failing_remove(self, path):
        if path == '/data':
            raise PermissionError('source delete denied')
        return original(self,path)
    monkeypatch.setattr(adapter,'remove',failing_remove)
    job = execute(env,direction,'move',['/data'])
    assert job['status'] == 'failed'
    assert job['files'][0]['phase'] == 'copied'
    assert (source / 'data').exists()
    assert (target / 'data').read_bytes() == b'abc'
    monkeypatch.setattr(adapter,'remove',original)
    job['files'][0]['error'] = ''
    job = env['store'].patch(job['id'],status='queued',files=job['files'])
    env['worker'].execute(job)
    result = env['store'].job(job['id'])
    assert result['status'] == 'succeeded', result['error']
    assert not (source / 'data').exists()


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_restart_after_publish_before_state_save(environment, direction):
    env = environment
    source, target = (env['remote'], env['nas']) if direction == 'download' else (env['nas'], env['remote'])
    (source / 'item.bin').write_bytes(b'complete')
    job = execute(env, direction, 'copy', ['/item.bin'])
    assert job['status'] == 'succeeded'
    job['files'][0]['phase'] = 'staged'
    job = env['store'].patch(job['id'],status='queued',files=job['files'],mode='move',completed_files=0,completed_bytes=0)
    env['worker'].execute(job)
    result = env['store'].job(job['id'])
    assert result['status'] == 'succeeded', result['error']
    assert not (source / 'item.bin').exists()
    assert (target / 'item.bin').read_bytes() == b'complete'


def test_cancel_mid_transfer_preserves_original_and_cleans_partial(environment, monkeypatch):
    env = environment
    (env['remote'] / 'large').write_bytes(os.urandom(1024 * 1024))
    original = FTPStorage.download
    def cancel_after_first_chunk(self,path,callback):
        def receive(data):
            env['store'].patch(job_id,cancel_requested=True)
            callback(data)
        original(self,path,receive)
    monkeypatch.setattr(FTPStorage,'download',cancel_after_first_chunk)
    job_id = env['worker'].submit(env['profile'],'download','move',['/large'],'/')['id']
    env['worker'].execute(env['store'].job(job_id))
    assert env['store'].job(job_id)['status'] == 'cancelled'
    assert (env['remote'] / 'large').exists()
    assert not list(env['nas'].iterdir())


def test_restart_resumes_durable_queue(environment):
    env = environment
    (env['remote'] / 'resume.txt').write_text('resume')
    job_id = env['worker'].submit(env['profile'],'download','copy',['/resume.txt'],'/')['id']
    env['store'].patch(job_id,status='running')
    # A fresh Store/worker observes only persisted data, not the submitting process.
    second_store = Store(env['tmp'] / 'data')
    worker = TransferWorker(second_store,env['nas'])
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and second_store.job(job_id)['status'] not in TERMINAL:
            time.sleep(0.02)
        assert second_store.job(job_id)['status'] == 'succeeded'
        assert (env['nas'] / 'resume.txt').read_text() == 'resume'
    finally:
        worker.shutdown()
        second_store.close()


def test_source_change_preserves_source(environment, monkeypatch):
    env = environment
    (env['remote'] / 'changing').write_bytes(b'original')
    original = FTPStorage.download
    def modified(self,path,callback):
        original(self,path,callback)
        (env['remote'] / 'changing').write_bytes(b'new content')
    monkeypatch.setattr(FTPStorage,'download',modified)
    job = execute(env,'download','move',['/changing'])
    assert job['status'] == 'failed'
    assert (env['remote'] / 'changing').read_bytes() == b'new content'
    assert not (env['nas'] / 'changing').exists()


@pytest.mark.parametrize('path',['/../etc/passwd','/a/../../x','/foo\r\nDELE file','/foo\x00bar','/foo\\bar'])
def test_path_rejection(path):
    with pytest.raises(ValueError):
        clean_path(path)


def test_nas_symlink_rejection(environment):
    env = environment
    (env['nas'] / 'escape').symlink_to(env['remote'],target_is_directory=True)
    with pytest.raises(ValueError):
        env['worker'].nas.local('/escape/secret')
    assert env['worker'].nas.list('/') == []


def test_ftp_base_directory_isolation(environment):
    env = environment
    (env['remote'] / 'allowed').mkdir()
    (env['remote'] / 'outside').write_text('secret')
    profile = dict(env['profile'],base_path='/allowed')
    remote = FTPStorage(profile)
    try:
        assert remote.list('/') == []
        with pytest.raises(ValueError):
            remote.stat('/../outside')
    finally:
        remote.close()


def test_legacy_nlst_fallback(environment):
    env = environment
    (env['remote'] / '한글.txt').write_bytes(b'abc')
    (env['remote'] / 'sub').mkdir()
    remote = FTPStorage(env['profile'])
    try:
        remote.cwd('/')
        entries = remote._legacy_list('/')
        assert {x['name']:x['is_dir'] for x in entries} == {'한글.txt':False,'sub':True}
    finally:
        remote.close()
