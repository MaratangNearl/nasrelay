import base64
from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient
import pyotp
import pytest

from app.main import create_app
from app.security import AuthService, recovery_hash
from app.store import Store

PASSWORD = 'new-secure-password-123'


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'data',tmp_path / 'nas',start_workers=False)
    with TestClient(app,raise_server_exceptions=False) as client:
        client.headers['X-Relay-Request'] = '1'
        yield client


def onboard(client):
    result = client.post('/api/login',json={'username':'admin','password':'admin'})
    assert result.status_code == 200
    assert result.json()['must_change_password']
    result = client.post('/api/auth/password',json={'current_password':'admin','new_password':PASSWORD})
    assert result.status_code == 200, result.text


def enroll(client,clock):
    response = client.post('/api/auth/totp/setup',json={'current_password':PASSWORD})
    assert response.status_code == 200, response.text
    data = response.json()
    code = pyotp.TOTP(data['secret']).at(clock[0])
    response = client.post('/api/auth/totp/enable',json={'code':code})
    assert response.status_code == 200, response.text
    return data, response.json()['recovery_codes']


@pytest.fixture
def clock(monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr('app.security.time.time',lambda:clock[0])
    return clock


def test_first_login_is_restricted_until_password_changed(client):
    assert client.post('/api/login',json={'username':'other','password':'admin'}).status_code == 401
    assert client.post('/api/login',json={'username':'admin','password':'admin'}).json()['must_change_password']
    assert client.get('/api/auth/me').json()['must_change_password']
    assert client.get('/api/state').status_code == 403
    assert client.get('/api/files/nas').status_code == 403
    assert client.post('/api/auth/totp/setup',json={'current_password':'admin'}).status_code == 403
    assert client.post('/api/auth/password',json={'current_password':'wrong','new_password':PASSWORD}).status_code == 400
    assert client.post('/api/auth/password',json={'current_password':'admin','new_password':'short'}).status_code == 422
    assert client.post('/api/auth/password',json={'current_password':'admin','new_password':PASSWORD}).status_code == 200
    assert client.get('/api/state').status_code == 200
    assert not client.get('/api/auth/me').json()['must_change_password']
    record = client.app.state.store.get('account')
    assert PASSWORD not in str(record)
    client.post('/api/logout')
    assert client.post('/api/login',json={'password':'admin'}).status_code == 401
    assert client.post('/api/login',json={'password':PASSWORD}).status_code == 200


def test_password_persists_restart_and_env_cannot_reset_it(tmp_path):
    data, nas = tmp_path/'data',tmp_path/'nas'
    with TestClient(create_app(data,nas,start_workers=False)) as client:
        client.headers['X-Relay-Request'] = '1'
        onboard(client)
    with TestClient(create_app(data,nas,password='environment-overwrite-password',start_workers=False)) as client:
        assert client.post('/api/login',json={'password':'environment-overwrite-password'}).status_code == 401
        assert client.post('/api/login',json={'password':PASSWORD}).status_code == 200
        assert not client.get('/api/auth/me').json()['must_change_password']


def test_legacy_account_migration_preserves_existing_password_and_requires_change(tmp_path):
    data = tmp_path/'data'
    store = Store(data)
    store.close()
    old_password = 'legacy-existing-password'
    with TestClient(create_app(data,tmp_path/'nas',password=old_password,start_workers=False)) as client:
        client.headers['X-Relay-Request'] = '1'
        assert client.post('/api/login',json={'password':'admin'}).status_code == 401
        assert client.post('/api/login',json={'password':old_password}).status_code == 200
        assert client.get('/api/auth/me').json()['migrated']
        assert client.get('/api/state').status_code == 403
        assert client.post('/api/auth/password',json={'current_password':old_password,'new_password':PASSWORD}).status_code == 200


def test_totp_setup_confirm_login_and_replay_rejection(client,clock):
    onboard(client)
    assert client.post('/api/auth/totp/setup',json={'current_password':'wrong'}).status_code == 400
    data, codes = enroll(client,clock)
    assert base64.b64decode(data['qr_code'].split(',')[1]).startswith(b'\x89PNG\r\n\x1a\n')
    assert data['secret'] in data['uri'] and 'otpauth://totp/' in data['uri']
    assert len(codes) == len(set(codes)) == 5
    account = client.app.state.store.get('account')
    assert data['secret'] not in str(account)
    assert not any(code in str(account) for code in codes)
    me = client.get('/api/auth/me').json()
    assert me['totp_enabled'] and me['recovery_codes_remaining'] == 5
    assert data['secret'] not in client.get('/api/state').text
    client.post('/api/logout')
    result = client.post('/api/login',json={'password':PASSWORD})
    assert result.json() == {'requires_totp':True}
    assert client.get('/api/state').status_code == 401
    used_code = pyotp.TOTP(data['secret']).at(clock[0])
    assert client.post('/api/login',json={'password':PASSWORD,'code':used_code}).status_code == 401
    clock[0] += 30
    code = pyotp.TOTP(data['secret']).at(clock[0])
    assert client.post('/api/login',json={'password':PASSWORD,'code':code}).status_code == 200
    client.post('/api/logout')
    assert client.post('/api/login',json={'password':PASSWORD,'code':code}).status_code == 401


def test_five_recovery_codes_are_each_single_use_and_persist(client,clock):
    onboard(client)
    _, codes = enroll(client,clock)
    for index, code in enumerate(codes):
        client.post('/api/logout')
        assert client.post('/api/login',json={'password':PASSWORD,'code':code.lower()}).status_code == 200
        assert client.get('/api/auth/me').json()['recovery_codes_remaining'] == 4-index
        client.post('/api/logout')
        assert client.post('/api/login',json={'password':PASSWORD,'code':code}).status_code == 401
    assert client.app.state.store.get('account')['recovery_hashes'] == []


def test_recovery_code_cannot_be_redeemed_concurrently(client,clock):
    onboard(client)
    _, codes = enroll(client,clock)
    service = client.app.state.accounts
    def redeem(_):
        try:
            return service.login('admin',PASSWORD,codes[0],'concurrent')[0].get('ok',False)
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(redeem,range(2))) == [False,True]
    assert len(client.app.state.store.get('account')['recovery_hashes']) == 4


def test_totp_deletion_requires_password_and_factor_and_revokes_sessions(client,clock):
    onboard(client)
    data, codes = enroll(client,clock)
    service = client.app.state.accounts
    other = service.login('admin',PASSWORD,codes[0],'other')[1]
    assert client.request('DELETE','/api/auth/totp',json={'current_password':PASSWORD,'code':''}).status_code == 400
    assert client.request('DELETE','/api/auth/totp',json={'current_password':'wrong','code':codes[1]}).status_code == 400
    response = client.request('DELETE','/api/auth/totp',json={'current_password':PASSWORD,'code':codes[1]})
    assert response.status_code == 200, response.text
    assert not client.get('/api/auth/me').json()['totp_enabled']
    assert client.get('/api/auth/me').json()['recovery_codes_remaining'] == 0
    assert client.app.state.store.get('account')['totp_secret'] is None
    with pytest.raises(Exception):
        service.authorize(other)
    client.post('/api/logout')
    assert client.post('/api/login',json={'password':PASSWORD}).status_code == 200


def test_pending_totp_is_session_bound_and_expires(client,clock):
    onboard(client)
    data = client.post('/api/auth/totp/setup',json={'current_password':PASSWORD}).json()
    code = pyotp.TOTP(data['secret']).at(clock[0])
    client.post('/api/logout')
    client.post('/api/login',json={'password':PASSWORD})
    assert client.post('/api/auth/totp/enable',json={'code':code}).status_code == 400
    data = client.post('/api/auth/totp/setup',json={'current_password':PASSWORD}).json()
    clock[0] += 601
    assert client.post('/api/auth/totp/enable',json={'code':pyotp.TOTP(data['secret']).at(clock[0])}).status_code == 400
    assert not client.get('/api/auth/me').json()['totp_enabled']


def test_security_mutations_require_csrf_header_and_revoke_other_password_sessions(client):
    onboard(client)
    service = client.app.state.accounts
    other = service.login('admin',PASSWORD,'','other')[1]
    client.headers.pop('X-Relay-Request')
    assert client.post('/api/auth/totp/setup',json={'current_password':PASSWORD}).status_code == 403
    client.headers['X-Relay-Request'] = '1'
    assert client.post('/api/auth/password',json={'current_password':PASSWORD,'new_password':PASSWORD+'2'}).status_code == 200
    with pytest.raises(Exception):
        service.authorize(other)
    assert client.get('/api/state').status_code == 200


def test_totp_and_spent_recovery_code_survive_restart(tmp_path):
    data,nas = tmp_path/'data',tmp_path/'nas'
    with TestClient(create_app(data,nas,start_workers=False)) as client:
        client.headers['X-Relay-Request'] = '1'
        onboard(client)
        _,codes = enroll(client,[time.time()])
        client.post('/api/logout')
        assert client.post('/api/login',json={'password':PASSWORD,'code':codes[0]}).status_code == 200
    with TestClient(create_app(data,nas,password='ignored-override-password',start_workers=False)) as client:
        assert client.post('/api/login',json={'password':PASSWORD}).json()['requires_totp']
        assert client.get('/api/state').status_code == 401
        assert client.post('/api/login',json={'password':PASSWORD,'code':codes[0]}).status_code == 401
        assert client.post('/api/login',json={'password':PASSWORD,'code':codes[1]}).status_code == 200
        assert client.get('/api/auth/me').json()['recovery_codes_remaining'] == 3


def test_fresh_install_uses_admin_admin_even_if_legacy_env_is_set(tmp_path):
    with TestClient(create_app(tmp_path/'data',tmp_path/'nas',password='unused-old-environment-password',start_workers=False)) as client:
        assert client.post('/api/login',json={'password':'unused-old-environment-password'}).status_code == 401
        assert client.post('/api/login',json={'password':'admin'}).json()['must_change_password']
        assert client.get('/api/state').status_code == 403
