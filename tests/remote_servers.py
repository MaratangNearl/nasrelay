"""Local protocol servers used by integration tests; no external accounts required."""
import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import posixpath
import shutil
import socket
import threading
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

import paramiko


@contextmanager
def sftp_server(root):
    key = paramiko.RSAKey.generate(2048)
    class Auth(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('tester', 'test-password') else paramiko.AUTH_FAILED
        def get_allowed_auths(self, username):
            return 'password'
        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    class Files(paramiko.SFTPServerInterface):
        def local(self, path):
            return root / path.lstrip('/')
        def canonicalize(self, path):
            return posixpath.normpath('/' + path.lstrip('/'))
        def list_folder(self, path):
            try:
                result = []
                for child in self.local(path).iterdir():
                    info = paramiko.SFTPAttributes.from_stat(child.lstat())
                    info.filename = child.name
                    result.append(info)
                return result
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
        def open(self, path, flags, attr):
            try:
                fd = os.open(self.local(path), flags, 0o600)
                handle = paramiko.SFTPHandle(flags)
                mode = 'r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
                file = os.fdopen(fd, mode)
                if mode != 'wb': handle.readfile = file
                if mode != 'rb': handle.writefile = file
                return handle
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
        def operation(self, callback):
            try:
                callback()
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)
        def mkdir(self, path, attr):
            return self.operation(lambda: self.local(path).mkdir())
        def remove(self, path):
            return self.operation(lambda: self.local(path).unlink())
        def rmdir(self, path):
            return self.operation(lambda: self.local(path).rmdir())
        def rename(self, source, target):
            def publish():
                os.link(self.local(source), self.local(target))
                self.local(source).unlink()
            return self.operation(publish)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.1)
    stop = threading.Event()
    transports = []
    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(connection)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files)
            try:
                transport.start_server(server=Auth())
            except (paramiko.SSHException, EOFError, OSError):
                transport.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield {'protocol': 'sftp', 'host': '127.0.0.1', 'port': listener.getsockname()[1],
               'username': 'tester', 'password': 'test-password', 'base_path': '/', 'name': 'SFTP test',
               'host_key_sha256': 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')}
    finally:
        stop.set()
        listener.close()
        for transport in transports: transport.close()
        thread.join(timeout=2)


@contextmanager
def webdav_server(root, download_redirect=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def local(self):
            return root / unquote(urlsplit(self.path).path).lstrip('/')
        def reply(self, status, data=b'', **headers):
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            for name, value in headers.items(): self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def authorize(self):
            expected = 'Basic ' + base64.b64encode(b'tester:test-password').decode()
            if self.headers.get('Authorization') == expected:
                return True
            self.close_connection = True
            self.reply(401, Connection='close')
            return False
        def do_PROPFIND(self):
            if not self.authorize(): return
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            local = self.local()
            if not local.exists(): return self.reply(404)
            items = [local]
            if self.headers.get('Depth') == '1' and local.is_dir(): items += list(local.iterdir())
            xml = ET.Element('{DAV:}multistatus')
            for item in items:
                response = ET.SubElement(xml, '{DAV:}response')
                href = '/' if item == root else '/' + item.relative_to(root).as_posix()
                ET.SubElement(response, '{DAV:}href').text = quote(href + ('/' if item.is_dir() else ''), safe='/')
                propstat = ET.SubElement(response, '{DAV:}propstat')
                ET.SubElement(propstat, '{DAV:}status').text = 'HTTP/1.1 200 OK'
                prop = ET.SubElement(propstat, '{DAV:}prop')
                resource = ET.SubElement(prop, '{DAV:}resourcetype')
                if item.is_dir(): ET.SubElement(resource, '{DAV:}collection')
                ET.SubElement(prop, '{DAV:}getcontentlength').text = str(item.stat().st_size)
                ET.SubElement(prop, '{DAV:}getlastmodified').text = str(item.stat().st_mtime_ns)
            self.reply(207, ET.tostring(xml), **{'Content-Type': 'application/xml'})
        def do_GET(self):
            if not self.authorize(): return
            if not self.local().is_file(): return self.reply(404)
            if download_redirect is not None:
                return self.reply(302, Location=download_redirect)
            self.reply(200, self.local().read_bytes())
        def do_PUT(self):
            if not self.authorize(): return
            size = int(self.headers['Content-Length'])
            data = self.rfile.read(size)
            if len(data) != size:
                return self.reply(400)
            self.local().write_bytes(data)
            self.reply(201)
        def do_MKCOL(self):
            if not self.authorize(): return
            try: self.local().mkdir()
            except FileExistsError: return self.reply(405)
            self.reply(201)
        def do_DELETE(self):
            if not self.authorize(): return
            local = self.local()
            if not local.exists(): return self.reply(404)
            if local.is_dir(): shutil.rmtree(local)
            else: local.unlink()
            self.reply(204)
        def do_MOVE(self):
            if not self.authorize(): return
            destination = root / unquote(urlsplit(self.headers['Destination']).path).lstrip('/')
            if destination.exists() and self.headers.get('Overwrite') == 'F': return self.reply(412)
            self.local().rename(destination)
            self.reply(201)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'protocol': 'webdav', 'host': '127.0.0.1', 'port': server.server_port, 'tls': False,
               'username': 'tester', 'password': 'test-password', 'base_path': '/', 'name': 'WebDAV test'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
