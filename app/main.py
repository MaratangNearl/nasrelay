from contextlib import asynccontextmanager
import logging
import ipaddress
import os
import re
import uuid
from pathlib import Path
import threading
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__
from .storage import create_remote, NAS, clean_path, join
from .store import Store
from .security import AuthService
from .telegram import TelegramBot
from .worker import TransferWorker, public_job

STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(default="admin", max_length=80)
    password: str = Field(max_length=1024)
    code: str = Field(default="", max_length=100)


class Reauthentication(BaseModel):
    current_password: str = Field(max_length=1024)
    code: str = Field(default="", max_length=100)


class PasswordChange(Reauthentication):
    new_password: str = Field(min_length=12, max_length=1024)


class TOTPCode(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class Profile(BaseModel):
    id: str | None = Field(default=None, max_length=32)
    protocol: Literal["ftp", "sftp", "webdav"] = "ftp"
    host_key_sha256: str = Field(default="", max_length=100)
    name: str = Field(default="원격 서버", min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = Field(default="anonymous", min_length=1, max_length=256)
    password: str = Field(default="", max_length=1024)
    base_path: str = Field(default="/", max_length=2048)
    tls: bool = False
    passive: bool = True
    encoding: Literal["utf-8", "cp949", "euc-kr"] = "utf-8"

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, values):
        values = dict(values)
        protocol = values.get("protocol", "ftp")
        values.setdefault("tls", protocol == "webdav")
        values.setdefault("port", 22 if protocol == "sftp" else (443 if values["tls"] else 80) if protocol == "webdav" else 21)
        return values

    @field_validator("host_key_sha256")
    @classmethod
    def fingerprint(cls, value):
        if value and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", value):
            raise ValueError("SHA256: 형식의 SSH 호스트 키 지문을 입력하세요.")
        return value

    @field_validator("host", "username", "password")
    @classmethod
    def no_control(cls, value):
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("제어 문자는 허용되지 않습니다.")
        return value

    @field_validator("host")
    @classmethod
    def hostname(cls, value):
        if any(c in value for c in "/@?#%\\") or any(c.isspace() for c in value):
            raise ValueError("프로토콜 없이 호스트 이름 또는 IP를 입력하세요.")
        if ":" in value:
            ipaddress.IPv6Address(value.strip("[]"))
            return value.strip("[]")
        return value

    @field_validator("base_path")
    @classmethod
    def path(cls, value):
        return clean_path(value)


class Transfer(BaseModel):
    profile_id: str | None = None
    direction: Literal["download", "upload"]
    mode: Literal["copy", "move"] = "copy"
    sources: list[str] = Field(min_length=1, max_length=1000)
    destination: str = Field(default="/", max_length=2048)


class Folder(BaseModel):
    profile_id: str | None = None
    side: Literal["ftp", "nas"]
    parent: str
    name: str = Field(min_length=1, max_length=255)


class TelegramSettings(BaseModel):
    token: str = Field(default="", max_length=256)
    chat_id: str = Field(default="", max_length=40)
    enabled: bool = False

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if value and (":" not in value or any(c not in "0123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-" for c in value)):
            raise ValueError("봇 토큰 형식이 올바르지 않습니다.")
        return value

    @field_validator("chat_id")
    @classmethod
    def valid_chat(cls, value):
        if value and (not value.isdigit() or int(value) <= 0):
            raise ValueError("개인 대화의 양수 Chat ID를 입력하세요.")
        return value


def create_app(data_dir=None, nas_root=None, password=None, start_workers=True):
    data_dir = Path(data_dir or os.getenv("DATA_DIR", "/data"))
    nas_root = Path(nas_root or os.getenv("NAS_ROOT", "/nas"))
    password = password if password is not None else os.getenv("APP_PASSWORD", "")
    secure_cookie = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    connection_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app):
        legacy_database = (data_dir / "app.sqlite3").is_file()
        store = Store(data_dir)
        nas = NAS(nas_root)
        worker = TransferWorker(store, nas_root)
        bot = TelegramBot(store, worker)
        app.state.accounts = AuthService(store, legacy_password=password, legacy_database=legacy_database)
        app.state.store, app.state.nas = store, nas
        app.state.worker, app.state.bot = worker, bot
        # 'Connected' means a connection was tested this process lifetime.
        app.state.connections = set()
        app.state.connection_attempts = {}
        app.state.connection_errors = {}
        if start_workers:
            worker.start()
            bot.start()
        try:
            yield
        finally:
            if start_workers:
                worker.shutdown()
                bot.shutdown()
            # Do not close a DB still used by a slow network call during shutdown.
            if not worker.is_alive() and not any(thread.is_alive() for thread in (bot.thread, bot.notifier)):
                store.close()

    app = FastAPI(title="nasrelay", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def session_auth(request: Request):
        token = request.cookies.get("relay_session", "")
        app.state.accounts.authorize(token, allow_password_change=True)
        if request.method not in ("GET", "HEAD") and request.headers.get("X-Relay-Request") != "1":
            raise HTTPException(403, "요청 확인 헤더가 없습니다.")
        return token

    def auth(request: Request):
        token = session_auth(request)
        app.state.accounts.authorize(token)
        return token

    def address(request):
        return request.client.host if request.client else "unknown"

    def set_session(response, token):
        response.set_cookie("relay_session", token, httponly=True, samesite="strict", secure=secure_cookie, max_age=86400)

    def profile(profile_id=None):
        profile_id = profile_id or app.state.store.get("selected_profile")
        if not profile_id:
            raise ValueError("서버 프로필을 먼저 설정하세요.")
        return app.state.store.profile(profile_id)

    def public_profile(selected):
        return {**{k: v for k, v in selected.items() if k != "password"},
                "has_password": bool(selected.get("password")),
                "connected": selected["id"] in app.state.connections,
                "connection_error": app.state.connection_errors.get(selected["id"], "")}

    def values(body, old=None):
        selected = body.model_dump(exclude={"id"})
        if not selected["password"] and old and all(selected.get(k) == old.get(k, "ftp" if k == "protocol" else None)
                                                   for k in ("protocol", "host", "port", "username")):
            selected["password"] = old["password"]
        return selected

    def remote(profile_id=None):
        with connection_lock:
            selected = profile(profile_id)
            if selected["id"] not in app.state.connections:
                raise ValueError("서버를 먼저 연결하세요.")
        return create_remote(selected)

    def connect_profile(profile_id):
        with connection_lock:
            selected = profile(profile_id)
            app.state.connections.discard(profile_id)
            attempt = object()
            app.state.connection_attempts[profile_id] = attempt
            app.state.connection_errors.pop(profile_id, None)
        # Slow network requests must not block other server connections or browsing.
        server = None
        try:
            server = create_remote(selected)
            server.list("/")
        except Exception:
            with connection_lock:
                if app.state.connection_attempts.get(profile_id) is attempt:
                    app.state.connection_errors[profile_id] = "서버 연결에 실패했습니다. 연결 설정을 확인하세요."
            raise
        finally:
            if server:
                server.close()
        with connection_lock:
            if profile(profile_id) != selected or app.state.connection_attempts.get(profile_id) is not attempt:
                raise ValueError("연결 확인 중 프로필이 변경되었습니다. 다시 연결하세요.")
            app.state.connections.add(profile_id)
            app.state.store.set("selected_profile", profile_id)
        return {"connected": True, "profile_id": profile_id, "message": "서버에 연결되었습니다."}

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "프로필 또는 작업을 찾을 수 없습니다."}, status_code=404)

    @app.exception_handler(Exception)
    async def operational_error(request, exc):
        # No tracebacks or request bodies: credentials may occur in third-party exceptions.
        log.warning("Request failed: %s", type(exc).__name__)
        return JSONResponse({"detail": f"작업 실패: {str(exc)[:500]}"}, status_code=400)

    @app.middleware("http")
    async def security(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        result, token = app.state.accounts.login(body.username, body.password, body.code, address(request))
        if token:
            set_session(response, token)
        return result

    @app.post("/api/logout", dependencies=[Depends(session_auth)])
    def logout(request: Request, response: Response):
        app.state.accounts.logout(request.cookies.get("relay_session", ""))
        response.delete_cookie("relay_session")
        return {"ok": True}

    @app.get("/api/auth/me")
    def account_state(token: str = Depends(session_auth)):
        return app.state.accounts.status(token)

    @app.post("/api/auth/password")
    def change_password(body: PasswordChange, request: Request, response: Response, token: str = Depends(session_auth)):
        renewed = app.state.accounts.change_password(token, body.current_password, body.new_password, body.code, address(request))
        set_session(response, renewed)
        return {"ok": True}

    @app.post("/api/auth/totp/setup")
    def totp_setup(body: Reauthentication, request: Request, token: str = Depends(auth)):
        return app.state.accounts.setup_totp(token, body.current_password, address(request))

    @app.post("/api/auth/totp/enable")
    def totp_enable(body: TOTPCode, request: Request, response: Response, token: str = Depends(auth)):
        codes, renewed = app.state.accounts.enable_totp(token, body.code, address(request))
        set_session(response, renewed)
        return {"ok": True, "recovery_codes": codes}

    @app.delete("/api/auth/totp")
    def totp_delete(body: Reauthentication, request: Request, response: Response, token: str = Depends(auth)):
        renewed = app.state.accounts.disable_totp(token, body.current_password, body.code, address(request))
        set_session(response, renewed)
        return {"ok": True}

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state():
        with connection_lock:
            profiles = [public_profile(p) for p in app.state.store.profiles()]
            selected_id = app.state.store.get("selected_profile")
            selected = next((p for p in profiles if p["id"] == selected_id), None)
            config = app.state.bot.config()
            return {"version": __version__, "connected": bool(selected and selected["connected"]), "profile": selected,
                    "profiles": profiles, "selected_profile_id": selected_id,
                    "telegram": {"enabled": config["enabled"], "chat_id": config["chat_id"], "has_token": bool(config["token"])},
                    "jobs": [public_job(j) for j in app.state.store.jobs()[:100]]}

    @app.get("/api/profiles", dependencies=[Depends(auth)])
    def list_profiles():
        with connection_lock:
            return {"profiles": [public_profile(p) for p in app.state.store.profiles()],
                    "selected_profile_id": app.state.store.get("selected_profile")}

    @app.post("/api/profiles", status_code=201, dependencies=[Depends(auth)])
    def add_profile(body: Profile):
        with connection_lock:
            selected = dict(values(body), id=uuid.uuid4().hex)
            app.state.store.save_profile(selected)
            if not app.state.store.get("selected_profile"):
                app.state.store.set("selected_profile", selected["id"])
            return public_profile(selected)

    @app.put("/api/profiles/{profile_id}", dependencies=[Depends(auth)])
    def edit_profile(profile_id: str, body: Profile):
        with connection_lock:
            old = profile(profile_id)
            selected = dict(values(body, old), id=profile_id)
            if any(selected.get(k) != old.get(k) for k in selected if k not in ("name", "id")):
                app.state.connections.discard(profile_id)
                app.state.connection_attempts.pop(profile_id, None)
                app.state.connection_errors.pop(profile_id, None)
            app.state.store.save_profile(selected)
            return public_profile(selected)

    @app.delete("/api/profiles/{profile_id}", dependencies=[Depends(auth)])
    def delete_profile(profile_id: str):
        with connection_lock:
            app.state.store.delete_profile(profile_id)
            app.state.connections.discard(profile_id)
            app.state.connection_attempts.pop(profile_id, None)
            app.state.connection_errors.pop(profile_id, None)
        return {"ok": True, "message": "프로필을 삭제했습니다. 등록된 전송 작업은 계속 실행됩니다."}

    @app.post("/api/profiles/{profile_id}/select", dependencies=[Depends(auth)])
    def select_profile(profile_id: str):
        with connection_lock:
            selected = profile(profile_id)
            app.state.store.set("selected_profile", profile_id)
            return public_profile(selected)

    @app.post("/api/profiles/{profile_id}/connect", dependencies=[Depends(auth)])
    def connect_saved_profile(profile_id: str):
        return connect_profile(profile_id)

    @app.post("/api/connection", dependencies=[Depends(auth)])
    def connect(body: Profile):
        # Keep the original save-and-connect endpoint for existing clients.
        with connection_lock:
            selected_id = body.id or app.state.store.get("selected_profile")
            old = profile(selected_id) if selected_id else None
            selected = dict(values(body, old), id=selected_id or uuid.uuid4().hex)
            app.state.connections.discard(selected["id"])
            app.state.store.save_profile(selected)
            app.state.store.set("selected_profile", selected["id"])
        return connect_profile(selected["id"])

    @app.delete("/api/connection", dependencies=[Depends(auth)])
    def disconnect(profile_id: str | None = None):
        with connection_lock:
            selected = profile(profile_id)
            app.state.connections.discard(selected["id"])
            app.state.connection_attempts.pop(selected["id"], None)
            app.state.connection_errors.pop(selected["id"], None)
        return {"connected": False, "message": "연결이 해제되었습니다. 등록된 전송 작업은 계속 실행됩니다."}

    @app.get("/api/files/{side}", dependencies=[Depends(auth)])
    def files(side: Literal["ftp", "nas"], path: str = "/", profile_id: str | None = None):
        path = clean_path(path)
        storage = remote(profile_id) if side == "ftp" else app.state.nas
        try:
            return {"path": path, "entries": storage.list(path)}
        finally:
            if side == "ftp":
                storage.close()

    @app.post("/api/folders", dependencies=[Depends(auth)])
    def folder(body: Folder):
        storage = remote(body.profile_id) if body.side == "ftp" else app.state.nas
        try:
            target = join(body.parent, body.name)
            if storage.exists(target):
                raise ValueError("이미 존재하는 이름입니다.")
            storage.mkdir(target)
            return {"path": target}
        finally:
            if body.side == "ftp":
                storage.close()

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(auth)])
    def submit(body: Transfer):
        with connection_lock:
            selected = profile(body.profile_id)
            if selected["id"] not in app.state.connections:
                raise ValueError("서버를 먼저 연결하세요.")
        return app.state.worker.submit(selected, body.direction, body.mode, body.sources, body.destination)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def get_job(job_id: str):
        return public_job(app.state.store.job(job_id))

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(auth)])
    def retry(job_id: str):
        with app.state.store.lock:
            job = app.state.store.job(job_id)
            if job["status"] not in ("failed", "cancelled"):
                raise ValueError("실패 또는 취소된 작업만 재시도할 수 있습니다.")
            for item in job["files"]:
                item["error"] = ""
            app.state.store.patch(job_id, files=job["files"], status="queued", cancel_requested=False, pause_requested=False,
                                  error="", finished_at=None, notified=False)
        app.state.worker.wake.set()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/{action}", dependencies=[Depends(auth)])
    def control(job_id: str, action: Literal["pause", "resume", "cancel"]):
        return app.state.worker.control(action, job_id)

    def telegram_config(body):
        config = body.model_dump()
        old = app.state.bot.config()
        if not config["token"]:
            config["token"] = old["token"]
        if config["enabled"] and (not config["token"] or not config["chat_id"]):
            raise ValueError("봇 토큰과 개인 Chat ID를 모두 입력하세요.")
        return config

    @app.put("/api/telegram", dependencies=[Depends(auth)])
    def telegram_save(body: TelegramSettings):
        config = telegram_config(body)
        if config["token"] != app.state.bot.config()["token"]:
            app.state.store.set("telegram_offset", 0)
        app.state.store.set("telegram", app.state.store.seal(config))
        return {"ok": True}

    @app.post("/api/telegram/test", dependencies=[Depends(auth)])
    def telegram_test(body: TelegramSettings):
        config = telegram_config(body)
        if not config["token"] or not config["chat_id"]:
            raise ValueError("봇 토큰과 Chat ID를 입력하세요.")
        app.state.bot.send(config, "✅ nasrelay 테스트 알림\n전송 알림을 받을 준비가 되었습니다.")
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
