"""SFTP and WebDAV implementations of the remote storage contract."""
import base64
import hashlib
import hmac
import os
import posixpath
import stat
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import httpx
import paramiko
from defusedxml import ElementTree

from .storage import Storage, clean_path, join

BLOCK_SIZE = 256 * 1024


class FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        if not hmac.compare_digest(actual, self.expected):
            raise ValueError('SFTP 호스트 키 지문이 일치하지 않습니다.')


class SFTPStorage(Storage):
    def __init__(self, profile):
        self.client = paramiko.SSHClient()
        self.sftp = None
        try:
            fingerprint = profile.get('host_key_sha256', '')
            if fingerprint:
                self.client.set_missing_host_key_policy(FingerprintPolicy(fingerprint))
            else:
                self.client.load_system_host_keys()
            self.client.connect(profile['host'], port=profile.get('port', 22), username=profile['username'],
                                password=profile['password'], timeout=30, banner_timeout=30, auth_timeout=30,
                                channel_timeout=30, allow_agent=False, look_for_keys=False)
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(30)
            self.root = self.sftp.normalize(clean_path(profile.get('base_path', '/'))).rstrip('/') or '/'
            if not stat.S_ISDIR(self.sftp.stat(self.root).st_mode):
                raise ValueError('SFTP 기본 경로가 폴더가 아닙니다.')
        except BaseException:
            self.close()
            raise

    def remote(self, path):
        # Reject links in every component, including links within the selected root.
        current = self.root
        parts = clean_path(path).strip('/').split('/')
        for index, part in enumerate(parts):
            if not part:
                continue
            current = posixpath.join(current, part)
            try:
                info = self.sftp.lstat(current)
            except FileNotFoundError:
                if index == len(parts) - 1:
                    return current
                raise
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('심볼릭 링크는 지원하지 않습니다.')
        return current

    @staticmethod
    def entry(path, info):
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('일반 파일과 폴더만 지원합니다.')
        return {'name': posixpath.basename(path) or '/', 'path': path, 'is_dir': stat.S_ISDIR(info.st_mode),
                'size': 0 if stat.S_ISDIR(info.st_mode) else info.st_size,
                'modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()}

    def stat(self, path):
        path = clean_path(path)
        return self.entry(path, self.sftp.lstat(self.remote(path)))

    def list(self, path):
        result = []
        for info in self.sftp.listdir_attr(self.remote(path)):
            if info.filename.startswith('.ftn-') or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            result.append(self.entry(join(path, info.filename), info))
        return sorted(result, key=lambda x: (not x['is_dir'], x['name'].casefold()))

    def mkdir(self, path):
        current = '/'
        for part in clean_path(path).strip('/').split('/'):
            if not part:
                continue
            current = join(current, part)
            try:
                info = self.stat(current)
            except FileNotFoundError:
                self.sftp.mkdir(self.remote(current))
            else:
                if not info['is_dir']:
                    raise FileExistsError(current)

    def remove(self, path):
        try:
            self.sftp.remove(self.remote(path))
        except FileNotFoundError:
            pass

    def rmdir(self, path):
        self.sftp.rmdir(self.remote(path))

    def download(self, path, callback):
        with self.sftp.open(self.remote(path), 'rb') as source:
            while data := source.read(BLOCK_SIZE):
                callback(data)

    def upload(self, path, file, callback):
        with self.sftp.open(self.remote(path), 'wb') as target:
            while data := file.read(BLOCK_SIZE):
                target.write(data)
                callback(data)

    def rename(self, source, destination):
        # Standard SFTP rename fails when the destination exists; posix_rename overwrites.
        self.sftp.rename(self.remote(source), self.remote(destination))

    def close(self):
        try:
            if self.sftp:
                self.sftp.close()
        finally:
            self.client.close()


class WebDAVStorage(Storage):
    PROPERTIES = b'''<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'''

    def __init__(self, profile):
        host = profile['host']
        host = f'[{host}]' if ':' in host and not host.startswith('[') else host
        self.origin = f"{'https' if profile.get('tls', True) else 'http'}://{host}:{profile.get('port', 443)}"
        self.root = clean_path(profile.get('base_path', '/')).rstrip('/')
        self.client = httpx.Client(auth=httpx.BasicAuth(profile['username'], profile['password']),
                                   timeout=30, verify=True, follow_redirects=False, trust_env=False)

    def url(self, path):
        return self.origin + quote(self.root + clean_path(path), safe='/')

    @staticmethod
    def check(response, multistatus=False):
        if response.status_code == 404:
            raise FileNotFoundError('WebDAV 파일을 찾을 수 없습니다.')
        if response.status_code == 412:
            raise FileExistsError('WebDAV 대상 파일이 이미 있습니다.')
        if not 200 <= response.status_code < 300 or (response.status_code == 207 and not multistatus):
            # Never include response bodies/URLs that may contain server-supplied secrets.
            raise ValueError(f'WebDAV 요청 실패 (HTTP {response.status_code})')

    def request(self, method, path, **kwargs):
        response = self.client.request(method, self.url(path), **kwargs)
        self.check(response)
        return response

    def properties(self, path, depth):
        # Bound XML input even when a server ignores Depth.
        url = self.url(path)
        if depth == 1 and not url.endswith('/'):
            url += '/'
        headers = {'Depth': str(depth), 'Content-Type': 'application/xml; charset=utf-8'}
        for attempt in range(2):
            with self.client.stream('PROPFIND', url, headers=headers, content=self.PROPERTIES) as response:
                if attempt == 0 and response.status_code in (301, 302, 307, 308):
                    location = response.headers.get('location', '')
                    expected = urlsplit(url).path.rstrip('/') + '/'
                    if location in (expected, self.origin + expected):
                        url = self.origin + expected
                        continue
                self.check(response, multistatus=True)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError('WebDAV 폴더 응답 크기 제한을 초과했습니다.')
            return self._properties(data, path)

    def _properties(self, data, path):
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('WebDAV 폴더 응답 크기 제한을 초과했습니다.')
        root = ElementTree.fromstring(data)
        if root.tag != '{DAV:}multistatus':
            raise ValueError('올바르지 않은 WebDAV 응답입니다.')
        entries = []
        for response in root.findall('{DAV:}response'):
            href = urlsplit(response.findtext('{DAV:}href', ''))
            origin = urlsplit(self.origin)
            if href.netloc and (href.scheme, href.netloc) != (origin.scheme, origin.netloc):
                raise ValueError('WebDAV 응답이 다른 서버를 가리킵니다.')
            remote = unquote(href.path).rstrip('/') or '/'
            if self.root and remote != self.root and not remote.startswith(self.root + '/'):
                raise ValueError('WebDAV 기본 폴더 밖에 접근할 수 없습니다.')
            child = clean_path(remote[len(self.root):] or '/')
            if child != clean_path(path) and posixpath.dirname(child) != clean_path(path):
                raise ValueError('WebDAV 응답에 하위 경로가 아닌 항목이 있습니다.')
            prop = None
            for propstat in response.findall('{DAV:}propstat'):
                status = propstat.findtext('{DAV:}status', '').split()
                if len(status) > 1 and status[1] == '200':
                    candidate = propstat.find('{DAV:}prop')
                    if candidate is not None and candidate.find('{DAV:}resourcetype') is not None:
                        prop = candidate
                        break
            if prop is None:
                raise ValueError('WebDAV 파일 속성을 확인할 수 없습니다.')
            is_dir = prop.find('{DAV:}resourcetype/{DAV:}collection') is not None
            size = 0 if is_dir else int(prop.findtext('{DAV:}getcontentlength', '-1'))
            if size < 0:
                raise ValueError('WebDAV 파일 크기를 확인할 수 없습니다.')
            entries.append({'name': posixpath.basename(child) or '/', 'path': child, 'is_dir': is_dir,
                            'size': size, 'modified': prop.findtext('{DAV:}getlastmodified', '')})
        return entries

    def stat(self, path):
        path = clean_path(path)
        for entry in self.properties(path, 0):
            if entry['path'] == path:
                return entry
        raise FileNotFoundError(path)

    def list(self, path):
        path = clean_path(path)
        entries = self.properties(path, 1)
        if not any(entry['path'] == path and entry['is_dir'] for entry in entries):
            raise ValueError('WebDAV 경로가 폴더가 아닙니다.')
        return sorted([entry for entry in entries if entry['path'] != path and not entry['name'].startswith('.ftn-')],
                      key=lambda x: (not x['is_dir'], x['name'].casefold()))

    def mkdir(self, path):
        current = '/'
        for part in clean_path(path).strip('/').split('/'):
            if not part:
                continue
            current = join(current, part)
            try:
                info = self.stat(current)
            except FileNotFoundError:
                self.request('MKCOL', current)
            else:
                if not info['is_dir']:
                    raise FileExistsError(current)

    def remove(self, path):
        try:
            self.request('DELETE', path)
        except FileNotFoundError:
            pass

    def rmdir(self, path):
        # DELETE on a WebDAV collection is recursive. Only remove empty folders.
        if any(entry['path'] != clean_path(path) for entry in self.properties(path, 1)):
            raise ValueError('비어 있지 않은 원본 폴더를 보존합니다.')
        self.request('DELETE', path)

    def download(self, path, callback):
        with self.client.stream('GET', self.url(path), headers={'Accept-Encoding': 'identity'}) as response:
            self.check(response)
            for chunk in response.iter_bytes(chunk_size=BLOCK_SIZE):
                callback(chunk)

    def upload(self, path, file, callback):
        def chunks():
            while data := file.read(BLOCK_SIZE):
                yield data
                callback(data)
        remaining = os.fstat(file.fileno()).st_size - file.tell()
        self.request('PUT', path, content=chunks(), headers={'Content-Length': str(remaining), 'Content-Type': 'application/octet-stream'})

    def rename(self, source, destination):
        self.request('MOVE', source, headers={'Destination': self.url(destination), 'Overwrite': 'F'})

    def close(self):
        self.client.close()
