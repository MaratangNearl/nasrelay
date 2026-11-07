import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.remotes import WebDAVStorage
from app.store import Store
from app.worker import TransferWorker
from remote_servers import webdav_server


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('destination', ['/direct/movie.mp4?token=a%2Fb', 'https://cdn.example/movie.mp4?token=a%2Fb'])
def test_download_follows_redirect_without_leaking_credentials(status, destination):
    requests = []
    payload = os.urandom(600000)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={'Location': destination})
        return httpx.Response(200, content=payload)

    storage = WebDAVStorage({'host': 'dav.example', 'username': 'user', 'password': 'secret'})
    storage.client.close()
    storage.client = httpx.Client(auth=('user', 'secret'), transport=httpx.MockTransport(handler))
    received = bytearray()
    try:
        storage.download('/My Pack/movie.mp4', received.extend)
        assert received == payload
        assert requests[0].url.path == '/My Pack/movie.mp4'
        assert requests[0].headers['Authorization'].startswith('Basic ')
        assert requests[1].url.query == b'token=a%2Fb'
        assert requests[1].headers['Accept-Encoding'] == 'identity'
        if destination.startswith('https://cdn.'):
            assert 'Authorization' not in requests[1].headers
        else:
            assert requests[1].headers['Authorization'] == requests[0].headers['Authorization']
    finally:
        storage.close()


def test_redirect_loop_is_bounded():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': '/loop?token=secret'})

    storage = WebDAVStorage({'host': 'dav.example', 'username': 'user', 'password': 'secret'})
    storage.client.close()
    storage.client = httpx.Client(transport=httpx.MockTransport(handler), max_redirects=3)
    try:
        with pytest.raises(httpx.TooManyRedirects) as error:
            storage.download('/file', lambda chunk: pytest.fail('Unexpected file content'))
        assert len(calls) == 4
        assert 'secret' not in str(error.value)
    finally:
        storage.close()


def test_redirected_download_checks_final_error_and_keeps_dav_writes_local():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == 'cdn.example':
            return httpx.Response(403, text='private signed URL')
        return httpx.Response(302, headers={'Location': 'https://cdn.example/file?token=secret'})

    storage = WebDAVStorage({'host': 'dav.example', 'username': 'user', 'password': 'secret'})
    storage.client.close()
    storage.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match=r'^WebDAV 요청 실패 \(HTTP 403\)$'):
            storage.download('/file', lambda chunk: pytest.fail('Unexpected error body'))
        requests.clear()
        with pytest.raises(ValueError, match='HTTP 302'):
            storage.remove('/file')
        assert len(requests) == 1
        assert requests[0].method == 'DELETE'
    finally:
        storage.close()


@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_worker_copies_webdav_file_via_external_redirect(tmp_path, mode):
    payload = os.urandom(650000)
    downloads = []

    class CDN(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            downloads.append((self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    cdn = ThreadingHTTPServer(('127.0.0.1', 0), CDN)
    thread = threading.Thread(target=cdn.serve_forever, daemon=True)
    thread.start()
    remote, nas = tmp_path / 'remote', tmp_path / 'nas'
    (remote / 'My Pack').mkdir(parents=True)
    nas.mkdir()
    source = remote / 'My Pack/movie.mp4'
    source.write_bytes(payload)
    store = Store(tmp_path / 'queue')
    try:
        target = f'http://127.0.0.1:{cdn.server_port}/signed/movie.mp4?token=a%2Fb'
        with webdav_server(remote, download_redirect=target) as profile:
            worker = TransferWorker(store, nas)
            job_id = worker.submit(profile, 'download', mode, ['/My Pack/movie.mp4'], '/')['id']
            worker.execute(store.job(job_id))
            job = store.job(job_id)
            assert job['status'] == 'succeeded', job['error']
            assert job['completed_files'] == 1
            assert (nas / 'movie.mp4').read_bytes() == payload
            assert source.exists() == (mode == 'copy')
            assert downloads == [('/signed/movie.mp4?token=a%2Fb', None)]
    finally:
        store.close()
        cdn.shutdown()
        cdn.server_close()
        thread.join(timeout=2)
