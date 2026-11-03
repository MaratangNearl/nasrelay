import os
import threading

import pytest

from app.storage import FTPStorage
from app.store import Store
from app.telegram import TelegramBot
from app.worker import TransferWorker


def submit(env, paths=None):
    return env['worker'].submit(env['profile'],'download','move',paths or ['/file'],'/')['id']


@pytest.mark.parametrize('direction',['download','upload'])
def test_pause_during_transfer_then_resume_preserves_completed_files(environment,monkeypatch,direction):
    env = environment
    source, destination = (env['remote'],env['nas']) if direction == 'download' else (env['nas'],env['remote'])
    (source/'a').write_bytes(b'first')
    payload = os.urandom(800000)
    (source/'large').write_bytes(payload)
    worker,store = env['worker'],env['store']
    job_id = worker.submit(env['profile'],direction,'move',['/a','/large'],'/')['id']
    method = 'download' if direction == 'download' else 'upload'
    original = getattr(FTPStorage,method)
    if direction == 'download':
        def pause(self,path,callback):
            def receive(data):
                worker.control('pause',job_id)
                callback(data)
            original(self,path,receive if path == '/large' else callback)
    else:
        def pause(self,path,file,callback):
            def sent(data):
                worker.control('pause',job_id)
                callback(data)
            original(self,path,file,sent if path.endswith('-1.part') else callback)
    monkeypatch.setattr(FTPStorage,method,pause)
    worker.execute(store.job(job_id))
    result = store.job(job_id)
    assert result['status'] == 'paused', result
    assert result['completed_files'] == 1
    assert result['transferred_bytes'] == len(b'first')
    assert (destination/'a').read_bytes() == b'first'
    assert not (source/'a').exists()
    assert (source/'large').read_bytes() == payload
    assert not (destination/'large').exists()
    assert not list(destination.glob('.ftn-*'))
    monkeypatch.setattr(FTPStorage,method,original)
    assert worker.control('resume',job_id)['status'] == 'queued'
    worker.execute(store.job(job_id))
    result = store.job(job_id)
    assert result['status'] == 'succeeded', result['error']
    assert result['completed_files'] == 2
    assert (destination/'large').read_bytes() == payload
    assert not (source/'large').exists()


def test_paused_job_can_be_cancelled_without_ftp_connection(environment):
    env = environment
    (env['remote']/'file').write_bytes(b'original')
    job_id = submit(env)
    assert env['worker'].control('pause',job_id)['status'] == 'paused'
    assert env['worker'].control('cancel',job_id)['status'] == 'cancelled'
    env['worker'].execute(env['store'].job(job_id))
    assert (env['remote']/'file').read_bytes() == b'original'
    assert not list(env['nas'].iterdir())


def test_paused_state_survives_worker_restart(environment):
    env = environment
    job_id = submit(env)
    env['store'].patch(job_id,status='running',pause_requested=True)
    second = Store(env['tmp']/'data')
    worker = TransferWorker(second,env['nas'])
    worker.start()
    try:
        assert second.job(job_id)['status'] == 'paused'
        assert second.job(job_id)['pause_requested']
    finally:
        worker.shutdown()
        second.close()


@pytest.mark.parametrize('next_action,expected',[('resume','queued'),('cancel','cancelled')])
def test_pause_acknowledgment_race_does_not_lose_later_action(environment,monkeypatch,next_action,expected):
    env = environment
    (env['remote']/'file').write_bytes(os.urandom(600000))
    job_id = submit(env)
    original = FTPStorage.download
    def pause_then_act(self,path,callback):
        def receive(data):
            env['worker'].control('pause',job_id)
            try:
                callback(data)
            finally:
                env['worker'].control(next_action,job_id)
        original(self,path,receive)
    monkeypatch.setattr(FTPStorage,'download',pause_then_act)
    env['worker'].execute(env['store'].job(job_id))
    assert env['store'].job(job_id)['status'] == expected
    assert (env['remote']/'file').exists()


def test_telegram_authorization_pause_resume_cancel_and_ambiguous_ids(environment,monkeypatch):
    env = environment
    bot = TelegramBot(env['store'],env['worker'])
    config = {'chat_id':'123','token':'123:token','enabled':True}
    messages = []
    monkeypatch.setattr(bot,'send',lambda config,text:messages.append(text))
    first = submit(env)
    def command(text,chat_id='123',kind='private'):
        bot.handle_message(config,{'chat':{'id':chat_id,'type':kind},'text':text})
    command('/pause','999')
    command('/cancel','123','group')
    assert not messages
    assert env['store'].job(first)['status'] == 'queued'
    command('/pause')
    assert env['store'].job(first)['status'] == 'paused'
    assert '일시정지' in messages[-1]
    command('/status')
    assert '⏸ 일시정지' in messages[-1] and first[:8] in messages[-1]
    command('/resume')
    assert env['store'].job(first)['status'] == 'queued'
    second = submit(env,['/other'])
    command('/cancel')
    assert '여러 개' in messages[-1]
    command('/pause '+first[:8])
    command('/cancel '+first[:8])
    assert env['store'].job(first)['status'] == 'cancelled'
    assert env['store'].job(second)['status'] == 'queued'
    command('/pause '+second[:8])
    command('/cancel')
    assert env['store'].job(second)['status'] == 'cancelled'
    command('/help')
    assert '/pause' in messages[-1] and '/resume' in messages[-1] and '/cancel' in messages[-1]
