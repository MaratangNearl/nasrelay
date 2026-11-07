from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from app.remotes import WebDAVStorage
from app.store import Store
from app.worker import TransferWorker
from remote_servers import webdav_server


@contextmanager
def cdn_server(handler):
    class CDN(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): handler(self)
    server = ThreadingHTTPServer(('127.0.0.1', 0), CDN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def wait_for(store, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = store.job(job_id)
        if result['status'] in ('succeeded', 'failed', 'cancelled'):
            return result
        time.sleep(.02)
    pytest.fail('Transfer did not finish')


@pytest.mark.parametrize('mode', ['copy', 'move'])
@pytest.mark.parametrize('trailing_slash', [False, True])
def test_queued_recursive_chinese_files_with_depth_zero_404_and_fresh_links(tmp_path, monkeypatch, mode, trailing_slash):
    root, nas = tmp_path / 'remote', tmp_path / 'nas'
    folder = 'My Pack/漢字 資料夾'
    (root / folder / '子目錄/空資料夾').mkdir(parents=True)
    nas.mkdir()
    payloads = {
        '/hold.bin': b'hold',
        f'/{folder}/電影 100% #1 [修復]?.mp4': os.urandom(600000),
        f'/{folder}/子目錄/繁體简体字幕.srt': '繁體 中文 한글 %E4 #?'.encode(),
    }
    for path, data in payloads.items():
        (root / path.lstrip('/')).write_bytes(data)
    generation = ['old']
    downloads, resolved = [], []
    entered, release = threading.Event(), threading.Event()
    original = WebDAVStorage.download

    def hold(self, path, callback):
        if path == '/hold.bin':
            entered.set()
            assert release.wait(5)
        return original(self, path, callback)

    monkeypatch.setattr(WebDAVStorage, 'download', hold)

    def serve(request):
        assert request.headers.get('Authorization') is None
        query = parse_qs(urlsplit(request.path).query)
        downloads.append(query['token'][0])
        assert query['token'] == [generation[0]]
        data = payloads[query['file'][0]]
        request.send_response(200)
        request.send_header('Content-Length', str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    store = Store(tmp_path / 'queue')
    worker = TransferWorker(store, nas)
    try:
        with cdn_server(serve) as cdn:
            def redirect(path):
                source = unquote(urlsplit(path).path)
                resolved.append(source)
                return f'{cdn}/signed?file={quote(source, safe="")}&token={generation[0]}'
            with webdav_server(root, download_redirect=redirect, missing_depth_zero=True,
                               collection_slash=trailing_slash) as profile:
                first = worker.submit(profile, 'download', 'copy', ['/hold.bin'], '/')['id']
                worker.start()
                assert entered.wait(3)
                second = worker.submit(profile, 'download', mode, ['/' + folder], '/')['id']
                assert store.job(second)['status'] == 'queued'
                assert not store.job(second)['planned']
                generation[0] = 'fresh-after-queue'
                release.set()
                assert wait_for(store, first)['status'] == 'succeeded'
                result = wait_for(store, second)
                assert result['status'] == 'succeeded', result['error']
                assert result['completed_files'] == 2
                for path, data in payloads.items():
                    if path == '/hold.bin': continue
                    relative = path.removeprefix('/My Pack/')
                    assert (nas / relative).read_bytes() == data
                assert (nas / '漢字 資料夾/子目錄/空資料夾').is_dir()
                assert (root / folder).exists() == (mode == 'copy')
                assert downloads == ['fresh-after-queue'] * 3
                assert set(resolved) == set(payloads)
                assert not list(nas.rglob('.ftn-*'))
    finally:
        release.set()
        if worker.thread.is_alive(): worker.shutdown()
        store.close()


@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_interrupted_chinese_download_and_expired_link_retry_without_corrupting_file(tmp_path, mode):
    root, nas = tmp_path / 'remote', tmp_path / 'nas'
    root.mkdir(); nas.mkdir()
    source = root / '漢字電影.mp4'
    payload = os.urandom(800000)
    source.write_bytes(payload)
    attempts, links = [], []

    def serve(request):
        attempts.append(request.path)
        assert request.headers.get('Authorization') is None
        assert source.exists()  # Move must preserve the source until verification.
        assert not (nas / source.name).exists()
        if len(attempts) == 2:
            request.send_response(403)
            request.send_header('Content-Length', '0')
            request.end_headers()
            return
        request.send_response(200)
        request.send_header('Content-Length', str(len(payload)))
        request.end_headers()
        if len(attempts) == 1:
            request.wfile.write(payload[:300000])
            request.close_connection = True
        else:
            request.wfile.write(payload)

    store = Store(tmp_path / 'queue')
    worker = TransferWorker(store, nas)
    try:
        with cdn_server(serve) as cdn:
            def redirect(path):
                links.append(path)
                return f'{cdn}/file?token={len(links)}'
            with webdav_server(root, download_redirect=redirect) as profile:
                job_id = worker.submit(profile, 'download', mode, ['/' + source.name], '/')['id']
                worker.execute(store.job(job_id))
                result = store.job(job_id)
                assert result['status'] == 'succeeded', result['error']
                assert (nas / source.name).read_bytes() == payload
                assert source.exists() == (mode == 'copy')
                assert len(links) == len(attempts) == 3
                assert attempts == ['/file?token=1','/file?token=2','/file?token=3']
                assert not list(nas.glob('.ftn-*'))
    finally:
        store.close()


def test_failed_redirect_retries_are_bounded_and_move_keeps_original(tmp_path):
    root, nas = tmp_path / 'remote', tmp_path / 'nas'
    root.mkdir(); nas.mkdir()
    source = root / '漢字.mp4'
    source.write_bytes(b'keep original')
    attempts = []

    def serve(request):
        attempts.append(request.path)
        request.send_response(404)
        request.send_header('Content-Length', '0')
        request.end_headers()

    store = Store(tmp_path / 'queue')
    worker = TransferWorker(store, nas)
    try:
        with cdn_server(serve) as cdn, webdav_server(root, download_redirect=cdn + '/missing') as profile:
            job_id = worker.submit(profile, 'download', 'move', ['/' + source.name], '/')['id']
            worker.execute(store.job(job_id))
            result = store.job(job_id)
            assert result['status'] == 'failed'
            assert 'HTTP 404' in result['error']
            assert len(attempts) == 3
            assert source.read_bytes() == b'keep original'
            assert list(nas.iterdir()) == []
            assert store.transfer_rates.current(job_id) == 0
    finally:
        store.close()
