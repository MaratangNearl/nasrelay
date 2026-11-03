"""Persistent administrator credentials and one-time second factors."""
import base64
import hashlib
import hmac
from io import BytesIO
import secrets
import threading
import time

from fastapi import HTTPException
import pyotp
import qrcode


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                           maxmem=64 * 1024 * 1024, dklen=32)
    return {"salt": salt, "value": value.hex()}


def password_matches(password, record):
    return hmac.compare_digest(password_hash(password, record["salt"])["value"], record["value"])


def recovery_hash(code):
    return hashlib.sha256(code.replace("-", "").replace(" ", "").upper().encode()).hexdigest()


class AuthService:
    def __init__(self, store, legacy_password="", legacy_database=False):
        self.store = store
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        with self.store.lock:
            if self.store.get("account") is None:
                legacy = bool(legacy_password and legacy_database)
                self.store.set("account", {"password": password_hash(legacy_password if legacy else "admin"),
                                          "must_change_password": True, "generation": 0,
                                          "totp_secret": None, "totp_last_counter": -1,
                                          "recovery_hashes": [], "pending_totp": None, "migrated": legacy})

    def limit(self, address):
        now = time.time()
        for key in list(self.attempts):
            if self.attempts[key][1] < now - 300:
                self.attempts.pop(key)
        count, start = self.attempts.get(address, (0, now))
        if count >= 10:
            raise HTTPException(429, "인증 시도가 많습니다. 5분 후 다시 시도하세요.")
        self.attempts[address] = (count + 1, start)

    def issue(self, account):
        current = time.time()
        self.sessions = {k:v for k,v in self.sessions.items() if v["expires"] > current and v["generation"] == account["generation"]}
        token = secrets.token_urlsafe(32)
        self.sessions[self.token_key(token)] = {"expires": current + 86400, "generation": account["generation"]}
        return token

    @staticmethod
    def token_key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authorize(self, token, allow_password_change=False):
        with self.lock:
            session = self.sessions.get(self.token_key(token)) if token else None
            account = self.store.get("account")
            if not session or session["expires"] < time.time() or session["generation"] != account["generation"]:
                raise HTTPException(401, "로그인이 필요합니다.")
            if account["must_change_password"] and not allow_password_change:
                raise HTTPException(403, "계속하려면 먼저 관리자 비밀번호를 변경하세요.")
            return account

    def status(self, token):
        account = self.authorize(token, allow_password_change=True)
        return {"username": "admin", "must_change_password": account["must_change_password"],
                "totp_enabled": bool(account["totp_secret"]), "recovery_codes_remaining": len(account["recovery_hashes"]),
                "migrated": account.get("migrated", False)}

    @staticmethod
    def matching_counter(secret, code, last_counter=-1):
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            return None
        totp = pyotp.TOTP(secret)
        current = int(time.time()) // totp.interval
        for counter in (current, current - 1, current + 1):
            if counter > last_counter and hmac.compare_digest(totp.at(counter * totp.interval), code):
                return counter
        return None

    def factor(self, account, code):
        if not account["totp_secret"]:
            return True
        counter = self.matching_counter(self.store.unseal(account["totp_secret"]), code, account["totp_last_counter"])
        if counter is not None:
            account["totp_last_counter"] = counter
            return True
        digest = recovery_hash(code)
        for index, value in enumerate(account["recovery_hashes"]):
            if hmac.compare_digest(digest, value):
                account["recovery_hashes"].pop(index)
                return True
        return False

    def login(self, username, password, code, address):
        with self.lock, self.store.lock:
            self.limit(address)
            account = self.store.get("account")
            valid_password = password_matches(password, account["password"])
            if username != "admin" or not valid_password:
                raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
            if account["totp_secret"] and not code:
                return {"requires_totp": True}, None
            if not self.factor(account, code):
                raise HTTPException(401, "인증 코드가 올바르지 않거나 이미 사용되었습니다. 새 코드 또는 복구 코드를 입력하세요.")
            self.store.set("account", account)
            self.attempts.pop(address, None)
            return {"ok": True, "must_change_password": account["must_change_password"]}, self.issue(account)

    def reauthenticate(self, account, password, code, address):
        self.limit(address)
        if not password_matches(password, account["password"]) or not self.factor(account, code):
            raise HTTPException(400, "현재 비밀번호 또는 인증 코드가 올바르지 않습니다. 사용하지 않은 새 코드를 입력하세요.")
        self.attempts.pop(address, None)

    def change_password(self, token, current_password, new_password, code, address):
        with self.lock, self.store.lock:
            account = self.authorize(token, allow_password_change=True)
            if len(new_password) < 12 or len(new_password) > 1024:
                raise ValueError("새 비밀번호는 12~1024자여야 합니다.")
            if new_password == current_password or new_password == "admin":
                raise ValueError("현재 비밀번호와 다른 새 비밀번호를 사용하세요.")
            self.reauthenticate(account, current_password, code, address)
            account.update(password=password_hash(new_password), must_change_password=False,
                           generation=account["generation"] + 1, pending_totp=None)
            self.store.set("account", account)
            return self.issue(account)

    def setup_totp(self, token, password, address):
        with self.lock, self.store.lock:
            account = self.authorize(token)
            if account["totp_secret"]:
                raise ValueError("TOTP가 이미 활성화되어 있습니다. 먼저 삭제하세요.")
            self.reauthenticate(account, password, "", address)
            secret = pyotp.random_base32()
            account["pending_totp"] = {"secret": self.store.seal(secret), "expires": time.time() + 600,
                                       "session": self.token_key(token)}
            self.store.set("account", account)
            uri = pyotp.TOTP(secret).provisioning_uri(name="admin", issuer_name="nasrelay")
            output = BytesIO()
            qrcode.make(uri).save(output, format="PNG")
            return {"secret": secret, "uri": uri, "qr_code": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(),
                    "expires_in": 600}

    def enable_totp(self, token, code, address):
        with self.lock, self.store.lock:
            account = self.authorize(token)
            self.limit(address)
            pending = account.get("pending_totp")
            if account["totp_secret"] or not pending or pending["expires"] < time.time() or pending["session"] != self.token_key(token):
                raise ValueError("TOTP 등록 요청이 만료되었습니다. QR 코드를 새로 생성하세요.")
            counter = self.matching_counter(self.store.unseal(pending["secret"]), code)
            if counter is None:
                raise ValueError("인증 코드가 올바르지 않습니다. 인증 앱과 NAS의 시간을 확인하세요.")
            codes = []
            for _ in range(5):
                value = secrets.token_hex(12).upper()
                codes.append("-".join(value[i:i+6] for i in range(0,24,6)))
            account.update(totp_secret=pending["secret"], totp_last_counter=counter, pending_totp=None,
                           recovery_hashes=[recovery_hash(value) for value in codes], generation=account["generation"] + 1)
            self.store.set("account", account)
            self.attempts.pop(address, None)
            return codes, self.issue(account)

    def disable_totp(self, token, password, code, address):
        with self.lock, self.store.lock:
            account = self.authorize(token)
            if not account["totp_secret"]:
                raise ValueError("TOTP가 활성화되어 있지 않습니다.")
            self.reauthenticate(account, password, code, address)
            account.update(totp_secret=None, totp_last_counter=-1, recovery_hashes=[], pending_totp=None,
                           generation=account["generation"] + 1)
            self.store.set("account", account)
            return self.issue(account)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self.token_key(token), None)
