import threading
from pathlib import Path

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.servers import FTPServer

from app.store import Store
from app.worker import TransferWorker


@pytest.fixture
def environment(tmp_path):
    remote_root = tmp_path / 'remote'
    remote_root.mkdir()
    nas_root = tmp_path / 'nas'
    nas_root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user('tester', 'test-password', str(remote_root), perm='elradfmwMT')
    handler = type('TestFTPHandler', (FTPHandler,), {'authorizer': authorizer})
    loop = IOLoop()
    server = FTPServer(('127.0.0.1', 0), handler, ioloop=loop)
    thread = threading.Thread(target=lambda: server.serve_forever(timeout=0.05, handle_exit=False), daemon=True)
    thread.start()
    profile = {'name': 'Test', 'host': '127.0.0.1', 'port': server.socket.getsockname()[1],
               'username': 'tester', 'password': 'test-password', 'base_path': '/', 'passive': True}
    store = Store(tmp_path / 'data')
    worker = TransferWorker(store, nas_root)
    yield {'remote': remote_root, 'nas': nas_root, 'store': store, 'worker': worker,
           'profile': profile, 'tmp': tmp_path}
    worker.stop.set()
    worker.wake.set()
    if worker.thread.is_alive():
        worker.thread.join(timeout=5)
    store.close()
    server.close_all()
    loop.close()
    thread.join(timeout=2)
