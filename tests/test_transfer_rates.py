import threading

import pytest

from app.storage import FTPStorage
from app.telegram import TelegramBot
from app.transfer_rates import TransferRates
from app.worker import public_job
from test_api import client, login


def test_current_rate_is_independent_of_polling_and_expires_when_stalled():
    clock = [10.0]
    rates = TransferRates(clock=lambda: clock[0])
    rates.start('first')
    rates.start('second')
    clock[0] = 11.0
    rates.record('first', 1024)
    rates.record('second', 4096)
    assert rates.current('first') == 1024
    assert rates.current('first') == 1024  # A second viewer cannot consume samples.
    assert rates.current('second') == 4096
    clock[0] = 12.0
    assert rates.current('first') == 512
    clock[0] = 16.0
    assert rates.current('first') == 0
    rates.record('first', 10240)
    assert rates.current('first') == 2048
    rates.stop('first')
    assert rates.current('first') == 0
    rates.start('first')
    assert rates.current('first') == 0


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_worker_samples_bytes_and_telegram_reports_speed_at_query_time(environment, monkeypatch, direction):
    env = environment
    clock = [0.0]
    env['store'].transfer_rates = TransferRates(clock=lambda: clock[0])
    source = env['remote'] if direction == 'download' else env['nas']
    target = env['nas'] if direction == 'download' else env['remote']
    payload = b'x' * 600000
    (source / 'file').write_bytes(payload)
    entered, release = threading.Event(), threading.Event()
    method = 'download' if direction == 'download' else 'upload'
    original = getattr(FTPStorage, method)

    def progress(callback):
        first = True
        def sample(data):
            nonlocal first
            clock[0] = 1.0 if first else clock[0] + 1.0
            callback(data)
            if first:
                first = False
                entered.set()
                assert release.wait(5)
        return sample

    if direction == 'download':
        monkeypatch.setattr(FTPStorage, method, lambda self, path, callback: original(self, path, progress(callback)))
    else:
        monkeypatch.setattr(FTPStorage, method, lambda self, path, file, callback: original(self, path, file, progress(callback)))
    job_id = env['worker'].submit(env['profile'], direction, 'copy', ['/file'], '/')['id']
    thread = threading.Thread(target=env['worker'].execute, args=(env['store'].job(job_id),))
    thread.start()
    try:
        assert entered.wait(3)
        job = env['store'].job(job_id)
        value = public_job(job, env['store'].transfer_rates)['speed_bytes_per_second']
        assert value > 0
        bot = TelegramBot(env['store'])
        assert f'현재 속도: {value / 1024:,.1f} KiB/s' in bot.status()
        clock[0] = 7.0
        assert '현재 속도: 0.0 B/s' in bot.status()
    finally:
        release.set()
        thread.join(timeout=5)
    result = env['store'].job(job_id)
    assert result['status'] == 'succeeded', result['error']
    assert public_job(result, env['store'].transfer_rates)['speed_bytes_per_second'] == 0
    assert env['store'].transfer_rates.current(job_id) == 0
    assert (target / 'file').read_bytes() == payload


@pytest.mark.parametrize('status', ['queued', 'planning', 'paused', 'succeeded', 'failed', 'cancelled'])
def test_inactive_jobs_never_show_a_stale_rate(status):
    clock = [0.0]
    rates = TransferRates(clock=lambda: clock[0])
    rates.start('job')
    clock[0] = 1.0
    rates.record('job', 100)
    assert rates.current('job') == 100
    assert public_job({'id':'job','status':status}, rates)['speed_bytes_per_second'] == 0


def test_api_state_and_job_endpoint_report_current_rate(client, environment):
    login(client)
    store = client.app.state.store
    clock = [0.0]
    store.transfer_rates = TransferRates(clock=lambda: clock[0])
    job = client.app.state.worker.submit(environment['profile'], 'download', 'copy', ['/file'], '/')
    store.patch(job['id'], status='running')
    store.transfer_rates.start(job['id'])
    clock[0] = 1.0
    store.transfer_rates.record(job['id'], 1024)
    assert client.get('/api/state').json()['jobs'][0]['speed_bytes_per_second'] == 1024
    assert client.get('/api/jobs/' + job['id']).json()['speed_bytes_per_second'] == 1024
    clock[0] = 2.0
    assert client.get('/api/jobs/' + job['id']).json()['speed_bytes_per_second'] == 512
    clock[0] = 7.0
    assert client.get('/api/state').json()['jobs'][0]['speed_bytes_per_second'] == 0
    assert 'speed_bytes_per_second' not in store.job(job['id'])
