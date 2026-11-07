import ctypes
from datetime import datetime
from ftplib import error_perm
from types import SimpleNamespace

from app import file_dates
from app.remotes import WebDAVStorage
from app.storage import FTPStorage, NAS


def test_creation_time_uses_birthtime_and_never_ctime(monkeypatch):
    monkeypatch.setattr(file_dates, '_statx', None)
    assert file_dates.creation_time('/file', SimpleNamespace(st_ctime=1700000000)) == ''
    assert file_dates.creation_time('/file', SimpleNamespace(st_birthtime=0)) == '1970-01-01T00:00:00+00:00'


def test_linux_creation_time_checks_support_mask(monkeypatch):
    def statx(directory, path, flags, mask, buffer):
        assert (directory, flags, mask) == (-100, 0x100, 0x800)
        result = ctypes.cast(buffer, ctypes.POINTER(file_dates._Statx)).contents
        result.mask = 0x800
        result.born.seconds = 1700000000
        return 0

    monkeypatch.setattr(file_dates, '_statx', statx)
    assert ctypes.sizeof(file_dates._Statx) == 256
    assert file_dates.creation_time('/한글', SimpleNamespace()) == '2023-11-14T22:13:20+00:00'
    for result in (0, -1):
        monkeypatch.setattr(file_dates, '_statx', lambda *args: result)
        assert file_dates.creation_time('/file', SimpleNamespace()) == ''


def test_nas_listing_preserves_modified_and_reads_creation(tmp_path, monkeypatch):
    import os
    path = tmp_path / 'file.txt'
    path.write_text('hello')
    os.utime(path, (1600000000, 1600000000))
    monkeypatch.setattr('app.storage.creation_time', lambda item, info: '2020-01-01T00:00:00+00:00')
    entry = NAS(tmp_path).list('/')[0]
    assert entry['created'] == '2020-01-01T00:00:00+00:00'
    assert datetime.fromisoformat(entry['modified']).timestamp() == 1600000000


def test_ftp_requests_and_returns_creation_fact():
    class FTP:
        def cwd(self, path): pass
        def pwd(self): return '/'
        def mlsd(self, facts):
            assert 'create' in facts
            return [('file', {'type': 'file', 'size': '5', 'modify': '20260102030405', 'create': '20250102030405.123'})]

    storage = FTPStorage.__new__(FTPStorage)
    storage.ftp, storage.root = FTP(), '/'
    entry = storage.list('/')[0]
    assert entry['modified'] == '20260102030405'
    assert entry['created'] == '20250102030405.123'


def test_ftp_keeps_mlsd_dates_when_optional_facts_are_rejected():
    class FTP:
        def cwd(self, path): pass
        def pwd(self): return '/'
        def mlsd(self, facts=None):
            if facts:
                raise error_perm('501 Unsupported fact')
            return [('file', {'type': 'file', 'modify': '20260102030405'})]

    storage = FTPStorage.__new__(FTPStorage)
    storage.ftp, storage.root = FTP(), '/'
    entry = storage.list('/')[0]
    assert entry['modified'] == '20260102030405'
    assert entry['created'] == ''


def test_webdav_creationdate_in_separate_successful_propstat():
    storage = WebDAVStorage({'host': 'example.com', 'username': 'user', 'password': 'secret'})
    data = b'''<d:multistatus xmlns:d="DAV:"><d:response><d:href>/file</d:href>
      <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>5</d:getcontentlength>
      <d:getlastmodified>Fri, 02 Jan 2026 03:04:05 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      <d:propstat><d:prop><d:creationdate>2025-01-02T03:04:05Z</d:creationdate></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
    try:
        assert b'creationdate' in storage.PROPERTIES
        assert storage._properties(data, '/')[0]['created'] == '2025-01-02T03:04:05Z'
        missing = data.replace(b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>',
                               b'<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>')
        assert storage._properties(missing, '/')[0]['created'] == ''
    finally:
        storage.close()
