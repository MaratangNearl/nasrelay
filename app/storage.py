"""Shared NAS and remote storage contract."""
from abc import ABC, abstractmethod
from contextlib import suppress
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS, error_perm
import os
from pathlib import Path, PurePosixPath
import posixpath
import ssl

from .file_dates import creation_time


def create_remote(profile):
    protocol = profile.get("protocol", "ftp")
    if protocol == "ftp":
        return FTPStorage(profile)
    from .remotes import SFTPStorage, WebDAVStorage
    if protocol == "sftp":
        return SFTPStorage(profile)
    if protocol == "webdav":
        return WebDAVStorage(profile)
    raise ValueError("지원하지 않는 프로토콜입니다.")


def clean_path(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00\\"):
        raise ValueError("올바르지 않은 경로입니다.")
    if ".." in value.split("/"):
        raise ValueError("상위 경로 접근은 허용되지 않습니다.")
    return "/" + str(PurePosixPath("/" + value.lstrip("/"))).lstrip("/")


def join(parent, name):
    if not name or name in (".", "..") or "/" in name or any(c in name for c in "\r\n\x00\\"):
        raise ValueError("지원하지 않는 파일 이름입니다.")
    return clean_path(posixpath.join(parent, name))


class Storage(ABC):
    @abstractmethod
    def list(self, path): ...

    @abstractmethod
    def stat(self, path): ...

    @abstractmethod
    def mkdir(self, path): ...

    @abstractmethod
    def remove(self, path): ...

    @abstractmethod
    def rmdir(self, path): ...

    def exists(self, path):
        try:
            self.stat(path)
            return True
        except FileNotFoundError:
            return False

    def close(self):
        pass


class NAS(Storage):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def local(self, path):
        path = clean_path(path)
        candidate = self.root / path.lstrip("/")
        # Reject symlinks, including links that happen to point inside the share.
        for part in [candidate, *candidate.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError("심볼릭 링크는 지원하지 않습니다.")
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("NAS 공유 폴더 밖에 접근할 수 없습니다.")
        return candidate

    def stat(self, path):
        item = self.local(path)
        info = item.stat()
        if not (item.is_dir() or item.is_file()):
            raise ValueError("일반 파일과 폴더만 지원합니다.")
        return {"name": item.name, "path": clean_path(path), "is_dir": item.is_dir(),
                "size": 0 if item.is_dir() else info.st_size,
                "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                "created": creation_time(item, info)}

    def list(self, path):
        return sorted([self.stat(join(path, child.name)) for child in self.local(path).iterdir()
                       if not child.is_symlink() and not child.name.startswith(".ftn-")],
                      key=lambda x: (not x["is_dir"], x["name"].casefold()))

    def mkdir(self, path):
        self.local(path).mkdir(parents=True, exist_ok=True)

    def remove(self, path):
        self.local(path).unlink(missing_ok=True)

    def rmdir(self, path):
        self.local(path).rmdir()


class FTPStorage(Storage):
    def __init__(self, profile):
        self.ftp = FTP_TLS(context=ssl.create_default_context(), timeout=30) if profile.get("tls") else FTP(timeout=30)
        try:
            self.ftp.encoding = profile.get("encoding", "utf-8")
            self.ftp.connect(profile["host"], profile.get("port", 21))
            self.ftp.login(profile["username"], profile["password"])
            if profile.get("tls"):
                self.ftp.prot_p()
            self.ftp.set_pasv(profile.get("passive", True))
            self.ftp.cwd(clean_path(profile.get("base_path", "/")))
            self.root = self.ftp.pwd().rstrip("/") or "/"
        except BaseException:
            self.ftp.close()
            raise

    def remote(self, path):
        return posixpath.join(self.root, clean_path(path).lstrip("/"))

    def cwd(self, path):
        self.ftp.cwd(self.remote(path))
        actual = self.ftp.pwd().rstrip("/") or "/"
        if self.root != "/" and actual != self.root and not actual.startswith(self.root + "/"):
            raise ValueError("FTP 기본 폴더 밖에 접근할 수 없습니다.")

    def list(self, path, include_internal=False):
        self.cwd(path)
        try:
            try:
                raw = list(self.ftp.mlsd(facts=['type', 'size', 'modify', 'create']))
            except error_perm as exc:
                if not str(exc).startswith(('500', '501', '502', '504')):
                    raise
                # Some servers support MLSD but reject optional fact selection.
                raw = list(self.ftp.mlsd())
            result = []
            for name, facts in raw:
                if facts.get("type") not in ("dir", "file") or name in (".", ".."):
                    continue
                if name.startswith(".ftn-") and not include_internal:
                    continue
                child = join(path, name)
                result.append({"name": name, "path": child, "is_dir": facts["type"] == "dir",
                               "size": 0 if facts["type"] == "dir" else int(facts.get("size", 0)),
                               "modified": facts.get("modify", ""), "created": facts.get("create", "")})
        except error_perm as exc:
            if not str(exc).startswith(("500", "501", "502", "504")):
                raise
            result = self._legacy_list(path, include_internal)
        return sorted(result, key=lambda x: (not x["is_dir"], x["name"].casefold()))

    def _legacy_list(self, path, include_internal=False):
        # NLST is intentionally followed by CWD/SIZE; no locale-dependent LIST parsing.
        names = self.ftp.nlst()
        result = []
        for raw in names:
            name = posixpath.basename(raw.rstrip("/"))
            if name in ("", ".", "..") or (name.startswith(".ftn-") and not include_internal):
                continue
            child = join(path, name)
            try:
                self.cwd(child)
            except error_perm:
                self.cwd(path)
                self.ftp.voidcmd("TYPE I")
                size = self.ftp.size(self.remote(child))
                if size is None:
                    raise ValueError("FTP 서버에서 파일 크기를 확인할 수 없습니다.")
                result.append({"name": name, "path": child, "is_dir": False, "size": size, "modified": "", "created": ""})
            else:
                result.append({"name": name, "path": child, "is_dir": True, "size": 0, "modified": "", "created": ""})
            self.cwd(path)
        return result

    def stat(self, path):
        path = clean_path(path)
        if path == "/":
            return {"name": "/", "path": "/", "is_dir": True, "size": 0}
        for entry in self.list(posixpath.dirname(path), include_internal=True):
            if entry["path"] == path:
                return entry
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    def mkdir(self, path):
        current = "/"
        for part in clean_path(path).strip("/").split("/"):
            if not part:
                continue
            current = join(current, part)
            try:
                self.cwd(current)
            except error_perm:
                self.ftp.mkd(self.remote(current))
                self.cwd(current)

    def remove(self, path):
        self.cwd(posixpath.dirname(clean_path(path)))
        try:
            self.ftp.delete(self.remote(path))
        except error_perm:
            if self.exists(path):
                raise

    def rmdir(self, path):
        self.ftp.rmd(self.remote(path))

    def download(self, path, callback):
        self.cwd(posixpath.dirname(clean_path(path)))
        self.ftp.retrbinary("RETR " + self.remote(path), callback, blocksize=256 * 1024)

    def upload(self, path, file, callback):
        self.cwd(posixpath.dirname(clean_path(path)))
        self.ftp.storbinary("STOR " + self.remote(path), file, blocksize=256 * 1024, callback=callback)

    def rename(self, source, destination):
        self.cwd(posixpath.dirname(clean_path(destination)))
        self.ftp.rename(self.remote(source), self.remote(destination))

    def close(self):
        with suppress(Exception):
            self.ftp.quit()
        self.ftp.close()
