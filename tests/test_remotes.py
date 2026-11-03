import os

import httpx
import pytest

from app.main import Profile
from app.remotes import SFTPStorage, WebDAVStorage
from app.storage import create_remote
from app.store import Store
from app.worker import TransferWorker
from remote_servers import sftp_server, webdav_server


@pytest.fixture(params=['sftp', 'webdav'])
def remote(request, tmp_path):
    root = tmp_path / 'server'
    root.mkdir()
    with (sftp_server if request.param == 'sftp' else webdav_server)(root) as profile:
        yield root, profile


def test_protocol_browse_and_stream_roundtrip(remote, tmp_path):
    root, profile = remote
    payload = os.urandom(650000)
    (root / '한글 #%.bin').write_bytes(payload)
    server = create_remote(profile)
    try:
        assert server.list('/')[0]['name'] == '한글 #%.bin'
        received = bytearray()
        server.download('/한글 #%.bin', received.extend)
        assert received == payload
        server.mkdir('/새 폴더/nested')
        local = tmp_path / 'upload'
        local.write_bytes(payload)
        progress = []
        with local.open('rb') as file:
            server.upload('/새 폴더/nested/.ftn-upload.part', file, lambda data: progress.append(len(data)))
        assert sum(progress) == len(payload)
        assert not server.list('/새 폴더/nested')
        assert server.stat('/새 폴더/nested/.ftn-upload.part')['size'] == len(payload)
        server.rename('/새 폴더/nested/.ftn-upload.part', '/새 폴더/nested/result.bin')
        assert (root / '새 폴더/nested/result.bin').read_bytes() == payload
        (root / 'existing').write_bytes(b'keep')
        with pytest.raises((OSError, ValueError)):
            server.rename('/새 폴더/nested/result.bin', '/existing')
        assert (root / 'existing').read_bytes() == b'keep'
        with pytest.raises((OSError, ValueError)):
            server.rmdir('/새 폴더/nested')
        assert (root / '새 폴더/nested/result.bin').exists()
        server.remove('/새 폴더/nested/result.bin')
        server.rmdir('/새 폴더/nested')
        server.rmdir('/새 폴더')
        assert not server.exists('/새 폴더')
        server.remove('/missing')
        with pytest.raises(ValueError): server.stat('/../outside')
    finally:
        server.close()


@pytest.mark.parametrize('direction', ['download', 'upload'])
@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_protocol_worker_recursive_transfer(remote, tmp_path, direction, mode):
    root, profile = remote
    nas = tmp_path / 'nas'
    nas.mkdir()
    source, target = (root, nas) if direction == 'download' else (nas, root)
    (source / 'folder/empty').mkdir(parents=True)
    (source / 'folder/한글.bin').write_bytes(os.urandom(300000))
    payload = (source / 'folder/한글.bin').read_bytes()
    store = Store(tmp_path / 'queue')
    worker = TransferWorker(store, nas)
    try:
        job_id = worker.submit(profile, direction, mode, ['/folder'], '/')['id']
        worker.execute(store.job(job_id))
        job = store.job(job_id)
        assert job['status'] == 'succeeded', job['error']
        assert job['completed_files'] == 1
        assert (target / 'folder/한글.bin').read_bytes() == payload
        assert (target / 'folder/empty').is_dir()
        assert (source / 'folder').exists() == (mode == 'copy')
    finally:
        store.close()


def test_sftp_fingerprint_and_symlink_confinement(tmp_path):
    root = tmp_path / 'sftp'
    (root / 'base').mkdir(parents=True)
    (root / 'outside').write_text('outside')
    (root / 'base/link').symlink_to(root / 'outside')
    with sftp_server(root) as profile:
        with pytest.raises(ValueError, match='지문'):
            SFTPStorage(dict(profile, host_key_sha256='SHA256:' + 'A' * 43))
        with pytest.raises(Exception):
            SFTPStorage(dict(profile, password='wrong'))
        server = SFTPStorage(dict(profile, base_path='/base'))
        try:
            assert server.list('/') == []
            with pytest.raises(ValueError, match='심볼릭'):
                server.download('/link', lambda chunk: None)
        finally:
            server.close()


def test_webdav_base_path_and_auth(tmp_path):
    (tmp_path / 'base').mkdir()
    (tmp_path / 'base/file').write_bytes(b'hello')
    (tmp_path / 'outside').write_bytes(b'outside')
    with webdav_server(tmp_path) as profile:
        server = WebDAVStorage(dict(profile, base_path='/base'))
        try:
            assert [entry['name'] for entry in server.list('/')] == ['file']
            assert server.stat('/file')['size'] == 5
        finally:
            server.close()
        bad = WebDAVStorage(dict(profile, password='wrong'))
        try:
            with pytest.raises(ValueError, match='401'): bad.list('/')
        finally:
            bad.close()


@pytest.mark.parametrize('protocol,port,tls', [('ftp',21,False), ('sftp',22,False), ('webdav',443,True)])
def test_profile_protocol_defaults(protocol, port, tls):
    profile = Profile(host='example.com', protocol=protocol)
    assert (profile.port, profile.tls) == (port, tls)


def test_webdav_rejects_external_hrefs_and_unsafe_xml():
    server = WebDAVStorage({'host':'example.com','username':'user','password':'secret','base_path':'/base'})
    try:
        for href in ['https://other.example/base/file', '/outside/file', '/base/../file', '/base/nested/deeper']:
            data = f'<d:multistatus xmlns:d="DAV:"><d:response><d:href>{href}</d:href></d:response></d:multistatus>'.encode()
            with pytest.raises(ValueError): server._properties(data, '/')
        with pytest.raises(Exception):
            server._properties(b'<!DOCTYPE x [<!ENTITY a "secret">]><x>&a;</x>', '/')
    finally:
        server.close()


def test_webdav_multistatus_delete_failure_is_not_success():
    with pytest.raises(ValueError, match='207'):
        WebDAVStorage.check(httpx.Response(207))


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_pause_resume_preserves_source_and_cleans_partial_data(remote, tmp_path, monkeypatch, direction):
    root, profile = remote
    nas = tmp_path / 'nas'
    nas.mkdir()
    source, target = (root, nas) if direction == 'download' else (nas, root)
    payload = os.urandom(600000)
    (source / 'large').write_bytes(payload)
    store = Store(tmp_path / 'queue')
    worker = TransferWorker(store, nas)
    adapter = SFTPStorage if profile['protocol'] == 'sftp' else WebDAVStorage
    original = getattr(adapter, direction)
    job_id = worker.submit(profile, direction, 'move', ['/large'], '/')['id']
    if direction == 'download':
        def paused(self, path, callback):
            def receive(data):
                worker.control('pause', job_id)
                callback(data)
            return original(self, path, receive)
    else:
        def paused(self, path, file, callback):
            def sent(data):
                worker.control('pause', job_id)
                callback(data)
            return original(self, path, file, sent)
    monkeypatch.setattr(adapter, direction, paused)
    try:
        worker.execute(store.job(job_id))
        assert store.job(job_id)['status'] == 'paused'
        assert (source/'large').read_bytes() == payload
        assert not list(target.glob('.ftn-*'))
        assert not (target/'large').exists()
        monkeypatch.setattr(adapter, direction, original)
        worker.control('resume', job_id)
        worker.execute(store.job(job_id))
        assert store.job(job_id)['status'] == 'succeeded', store.job(job_id)['error']
        assert (target/'large').read_bytes() == payload
        assert not (source/'large').exists()
    finally:
        store.close()
