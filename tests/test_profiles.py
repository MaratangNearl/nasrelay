import threading
import time

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.store import Store
from remote_servers import webdav_server
from test_api import client, login, PASSWORD


def add(client, profile):
    response = client.post('/api/profiles', json=profile)
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_profiles_crud_multiple_connections_and_explicit_routing(client, environment):
    login(client)
    env = environment
    (env['remote'] / 'ftp-file').write_text('ftp')
    dav_root = env['tmp'] / 'dav'
    dav_root.mkdir()
    (dav_root / 'dav-file').write_text('dav')
    with webdav_server(dav_root) as dav:
        ftp_id, dav_id = add(client, env['profile']), add(client, dav)
        for profile_id in (ftp_id, dav_id):
            assert client.post(f'/api/profiles/{profile_id}/connect').status_code == 200
        state = client.get('/api/state').json()
        assert len(state['profiles']) == 2
        assert all(profile['connected'] for profile in state['profiles'])
        assert state['selected_profile_id'] == dav_id
        assert client.get('/api/files/ftp', params={'profile_id':ftp_id}).json()['entries'][0]['name'] == 'ftp-file'
        assert client.get('/api/files/ftp', params={'profile_id':dav_id}).json()['entries'][0]['name'] == 'dav-file'
        assert client.post('/api/folders', json={'side':'ftp','profile_id':ftp_id,'parent':'/','name':'ftp-folder'}).status_code == 200
        assert (env['remote'] / 'ftp-folder').is_dir()
        assert not (dav_root / 'ftp-folder').exists()
        assert client.post(f'/api/profiles/{ftp_id}/select').status_code == 200
        assert client.get('/api/state').json()['profile']['id'] == ftp_id
        job = client.post('/api/jobs', json={'direction':'download','profile_id':dav_id,'sources':['/dav-file']}).json()
        assert job['profile_id'] == dav_id and job['protocol'] == 'webdav'
        assert 'profile' not in job
        assert client.put(f'/api/profiles/{dav_id}',json=dict(dav,name='Renamed',password='')).status_code == 200
        assert client.app.state.store.profile(dav_id)['password'] == dav['password']
        assert client.delete(f'/api/profiles/{dav_id}').status_code == 200
        # Queued jobs retain encrypted credentials independently of profile lifecycle.
        client.app.state.worker.execute(client.app.state.store.job(job['id']))
        assert client.get(f"/api/jobs/{job['id']}").json()['status'] == 'succeeded'
        assert (env['nas'] / 'dav-file').read_text() == 'dav'
        assert client.get('/api/state').json()['connected']
        assert client.delete('/api/connection',params={'profile_id':ftp_id}).status_code == 200
        assert client.get('/api/files/ftp',params={'profile_id':ftp_id}).status_code == 400
        assert client.delete(f'/api/profiles/{ftp_id}').status_code == 200
        assert client.get('/api/state').json()['profile'] is None
        assert client.post(f'/api/profiles/{dav_id}/select').status_code == 404


def test_edit_and_failed_connection_do_not_disconnect_other_servers(client, environment):
    login(client)
    first, second = [add(client, dict(environment['profile'],name=name)) for name in ['A','B']]
    for profile_id in [first, second]:
        assert client.post(f'/api/profiles/{profile_id}/connect').status_code == 200
    assert client.put(f'/api/profiles/{second}',json=dict(environment['profile'],password='wrong')).status_code == 200
    assert client.post(f'/api/profiles/{second}/connect').status_code == 400
    profiles = {p['id']:p for p in client.get('/api/profiles').json()['profiles']}
    assert profiles[first]['connected'] and not profiles[second]['connected']
    assert 'password' not in profiles[first]
    assert client.get('/api/files/ftp',params={'profile_id':first}).status_code == 200


@pytest.mark.parametrize('change', [{'host':'different.example'}, {'protocol':'sftp'}, {'username':'other'}, {'port':2222}])
def test_credentials_are_not_reused_for_other_identity(client, environment, change):
    login(client)
    profile_id = add(client, environment['profile'])
    response = client.put(f'/api/profiles/{profile_id}',json=dict(environment['profile'],password='',**change))
    assert response.status_code == 200
    assert not client.app.state.store.profile(profile_id)['password']


def test_legacy_profile_migrates_once_and_deletion_does_not_restore_it(tmp_path):
    directory = tmp_path / 'legacy'
    store = Store(directory)
    # Simulate a database from the single-profile release.
    store.set('profiles_migrated', False)
    store.set('profile', store.seal({'name':'legacy','host':'localhost','port':21,'username':'user','password':'secret'}))
    store.close()
    store = Store(directory)
    profiles = store.profiles()
    assert len(profiles) == 1 and profiles[0]['protocol'] == 'ftp'
    assert profiles[0]['password'] == 'secret'
    assert store.get('selected_profile') == profiles[0]['id']
    assert store.get('profile') is None
    ciphertext = store.db.execute('SELECT body FROM profiles').fetchone()[0]
    assert 'secret' not in ciphertext
    store.close()
    store = Store(directory)
    assert len(store.profiles()) == 1
    store.delete_profile(profiles[0]['id'])
    store.close()
    store = Store(directory)
    assert store.profiles() == []
    store.close()


def test_profiles_and_selection_survive_restart_but_connections_reset(client, environment):
    login(client)
    profile_id = add(client, environment['profile'])
    assert client.post(f'/api/profiles/{profile_id}/connect').status_code == 200
    app = create_app(environment['tmp']/'api-data',environment['nas'],start_workers=False)
    with TestClient(app) as restarted:
        assert restarted.post('/api/login',json={'password':PASSWORD}).status_code == 200
        state = restarted.get('/api/state').json()
        assert state['selected_profile_id'] == profile_id
        assert len(state['profiles']) == 1 and not state['connected']


def test_different_servers_transfer_concurrently(environment, monkeypatch):
    env = environment
    root = env['tmp'] / 'dav'
    root.mkdir()
    (env['remote'] / 'slow').write_bytes(b'ftp')
    (root / 'fast').write_bytes(b'dav')
    entered, release = threading.Event(), threading.Event()
    from app.storage import FTPStorage
    original = FTPStorage.download
    def blocked(self, path, callback):
        entered.set()
        assert release.wait(5)
        return original(self, path, callback)
    monkeypatch.setattr(FTPStorage, 'download', blocked)
    with webdav_server(root) as profile:
        first = env['worker'].submit(env['profile'],'download','copy',['/slow'],'/')['id']
        second = env['worker'].submit(profile,'download','copy',['/fast'],'/')['id']
        env['worker'].start()
        try:
            assert entered.wait(2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and env['store'].job(second)['status'] != 'succeeded':
                time.sleep(.02)
            assert env['store'].job(second)['status'] == 'succeeded'
            assert env['store'].job(first)['status'] == 'running'
            assert not release.is_set()
        finally:
            release.set()
            env['worker'].shutdown()


def test_overlapping_nas_targets_are_serialized(environment, monkeypatch):
    env = environment
    root = env['tmp'] / 'dav'
    root.mkdir()
    (env['remote'] / 'same').write_bytes(b'first')
    (root / 'same').write_bytes(b'second')
    entered, release = threading.Event(), threading.Event()
    from app.storage import FTPStorage
    original = FTPStorage.download
    def blocked(self,path,callback):
        entered.set()
        assert release.wait(5)
        return original(self,path,callback)
    monkeypatch.setattr(FTPStorage,'download',blocked)
    with webdav_server(root) as profile:
        first = env['worker'].submit(env['profile'],'download','copy',['/same'],'/')['id']
        second = env['worker'].submit(profile,'download','move',['/same'],'/')['id']
        env['worker'].start()
        try:
            assert entered.wait(2)
            assert env['store'].job(second)['status'] == 'queued'
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and env['store'].job(second)['status'] != 'failed': time.sleep(.02)
            assert env['store'].job(first)['status'] == 'succeeded'
            assert env['store'].job(second)['status'] == 'failed'
            assert (root/'same').read_bytes() == b'second'
            assert (env['nas']/'same').read_bytes() == b'first'
        finally:
            release.set()
            env['worker'].shutdown()


def test_disconnect_during_connection_test_cannot_restore_connection(client, environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import app.main as main
    login(client)
    profile_id = add(client, environment['profile'])
    entered, release = threading.Event(), threading.Event()
    class SlowServer:
        def list(self, path):
            entered.set()
            assert release.wait(3)
            return []
        def close(self): pass
    monkeypatch.setattr(main, 'create_remote', lambda profile: SlowServer())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f'/api/profiles/{profile_id}/connect')
        try:
            assert entered.wait(2)
            # State reads and disconnect must remain available during network I/O.
            assert client.get('/api/profiles').status_code == 200
            assert client.delete('/api/connection', params={'profile_id':profile_id}).status_code == 200
        finally:
            release.set()
        assert pending.result().status_code == 400
    assert not client.get('/api/state').json()['connected']
