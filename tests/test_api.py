from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.telegram import TelegramBot

PASSWORD = 'test-password-1234'


@pytest.fixture
def client(environment):
    env = environment
    app = create_app(env['tmp'] / 'api-data',env['nas'],password=PASSWORD,start_workers=False)
    with TestClient(app,raise_server_exceptions=False) as client:
        assert client.post('/api/login',json={'password':'admin'}).status_code == 200
        client.headers['X-Relay-Request'] = '1'
        assert client.post('/api/auth/password',json={'current_password':'admin','new_password':PASSWORD}).status_code == 200
        assert client.post('/api/logout').status_code == 200
        client.headers.pop('X-Relay-Request')
        yield client


def login(client):
    assert client.post('/api/login',json={'password':PASSWORD}).status_code == 200
    client.headers['X-Relay-Request'] = '1'


def test_auth_and_csrf_and_logout(client):
    assert client.get('/api/state').status_code == 401
    assert client.post('/api/login',json={'password':'wrong'}).status_code == 401
    assert client.post('/api/login',json={'password':PASSWORD}).status_code == 200
    assert client.get('/api/state').status_code == 200
    assert client.post('/api/jobs',json={'direction':'download','sources':['/file']}).status_code == 403
    client.headers['X-Relay-Request'] = '1'
    assert client.post('/api/logout').status_code == 200
    assert client.get('/api/state').status_code == 401


def test_connect_browse_submit_disconnect_persists_job(client,environment):
    env = environment
    login(client)
    (env['remote'] / '한글.txt').write_bytes(b'hello')
    assert client.post('/api/connection',json=env['profile']).status_code == 200
    state = client.get('/api/state').json()
    assert state['connected']
    assert 'password' not in state['profile']
    assert state['profile']['has_password']
    listing = client.get('/api/files/ftp').json()['entries']
    assert listing[0]['name'] == '한글.txt'
    result = client.post('/api/jobs',json={'direction':'download','sources':['/한글.txt'],'mode':'move'})
    assert result.status_code == 202
    assert 'profile' not in result.json()
    job_id = result.json()['id']
    assert client.delete('/api/connection').status_code == 200
    assert client.get(f'/api/jobs/{job_id}').json()['status'] == 'queued'
    assert client.get('/api/files/ftp').status_code == 400
    assert client.post('/api/jobs',json={'direction':'download','sources':['/other']}).status_code == 400
    assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 200
    assert client.get(f'/api/jobs/{job_id}').json()['cancel_requested']


def test_failed_connection_is_visible(client,environment):
    login(client)
    assert client.post('/api/connection',json=environment['profile']).status_code == 200
    bad = dict(environment['profile'],password='wrong')
    response = client.post('/api/connection',json=bad)
    assert response.status_code == 400
    assert response.json()['detail']
    assert client.get('/api/state').json()['connected'] is False


def test_saved_credentials_not_exposed_or_reused_on_other_host(client,environment):
    login(client)
    profile = environment['profile']
    assert client.post('/api/connection',json=profile).status_code == 200
    assert client.post('/api/connection',json=dict(profile,password='')).status_code == 200
    assert 'test-password' not in client.get('/api/state').text
    sealed = client.app.state.store.db.execute('SELECT body FROM profiles').fetchone()[0]
    assert 'test-password' not in sealed


def test_validation_and_mkdir(client,environment):
    login(client)
    assert client.post('/api/folders',json={'side':'nas','parent':'/','name':'new folder'}).status_code == 200
    assert (environment['nas'] / 'new folder').is_dir()
    assert client.post('/api/folders',json={'side':'nas','parent':'/','name':'../escape'}).status_code == 400
    assert client.get('/api/files/nas',params={'path':'/../'}).status_code == 400
    assert client.post('/api/connection',json=dict(environment['profile'],host='host\r\nUSER')).status_code == 422
    assert client.get('/api/jobs/missing').status_code == 404


def test_telegram_private_chat_validation_and_redaction(client,monkeypatch):
    login(client)
    config = {'token':'123456:secret_token','chat_id':'123456','enabled':True}
    assert client.put('/api/telegram',json=dict(config,chat_id='-100123')).status_code == 422
    assert client.put('/api/telegram',json=config).status_code == 200
    result = client.get('/api/state').json()['telegram']
    assert result == {'enabled':True,'has_token':True,'chat_id':'123456'}
    assert client.app.state.bot.status() == '진행중인 작업 없음'
    messages = []
    monkeypatch.setattr(TelegramBot,'send',lambda self,cfg,message: messages.append(message))
    assert client.post('/api/telegram/test',json=dict(config,token='')).status_code == 200
    assert '테스트' in messages[0]


def test_telegram_status_and_notification(environment,monkeypatch):
    env = environment
    bot = TelegramBot(env['store'])
    env['store'].set('telegram',env['store'].seal({'token':'123:token','chat_id':'111','enabled':True}))
    job_id = env['worker'].submit(env['profile'],'download','copy',['/test'],'/')['id']
    env['store'].patch(job_id,status='running',total_files=2,total_bytes=200,transferred_bytes=100,completed_files=1,completed_bytes=100)
    assert '50.0%' in bot.status()
    messages = []
    monkeypatch.setattr(bot,'send',lambda config,message:messages.append(message))
    env['store'].patch(job_id,status='failed',error='connection lost')
    bot.notify(env['store'].job(job_id))
    assert '전송 실패' in messages[0] and '1 / 2개' in messages[0] and '100.0 B' in messages[0]
    assert bot.status() == '진행중인 작업 없음'


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/login',json={'password':'bad'}).status_code == 401
    assert client.post('/api/login',json={'password':'bad'}).status_code == 429


def test_slow_telegram_notifications_do_not_block_transfers(environment,monkeypatch):
    import threading
    import time
    env = environment
    bot = TelegramBot(env['store'])
    entered, release = threading.Event(), threading.Event()
    env['store'].set('telegram',env['store'].seal({'token':'123:token','chat_id':'111','enabled':True}))
    previous = env['worker'].submit(env['profile'],'download','copy',['/previous'],'/')['id']
    env['store'].patch(previous,status='failed',error='test')
    def slow_notification(job):
        entered.set()
        release.wait(5)
    monkeypatch.setattr(bot,'notify',slow_notification)
    bot.notifier.start()
    try:
        assert entered.wait(2)
        (env['remote'] / 'next.txt').write_text('next')
        current = env['worker'].submit(env['profile'],'download','copy',['/next.txt'],'/')['id']
        env['worker'].start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and env['store'].job(current)['status'] != 'succeeded':
            time.sleep(0.02)
        assert env['store'].job(current)['status'] == 'succeeded'
        assert not release.is_set()
    finally:
        release.set()
        bot.stop.set()
        bot.notifier.join(timeout=5)


def test_web_pause_resume_cancel_and_retry_routes(client,environment):
    login(client)
    assert client.post('/api/connection',json=environment['profile']).status_code == 200
    job_id = client.post('/api/jobs',json={'direction':'download','sources':['/file']}).json()['id']
    assert client.post(f'/api/jobs/{job_id}/pause').json()['status'] == 'paused'
    assert client.post(f'/api/jobs/{job_id}/resume').json()['status'] == 'queued'
    assert client.post(f'/api/jobs/{job_id}/pause').json()['status'] == 'paused'
    assert client.post(f'/api/jobs/{job_id}/cancel').json()['status'] == 'cancelled'
    assert client.post(f'/api/jobs/{job_id}/retry').status_code == 200
    assert client.get(f'/api/jobs/{job_id}').json()['status'] == 'queued'
