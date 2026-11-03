import logging
import threading
import httpx

from .worker import control_job

log = logging.getLogger(__name__)


def size(value):
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:,.1f} {unit}"
        number /= 1024


class TelegramBot:
    def __init__(self, store, worker=None):
        self.store = store
        self.worker = worker
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="telegram-poller", daemon=True)
        self.notifier = threading.Thread(target=self.outbox, name="telegram-notifier", daemon=True)

    def config(self):
        value = self.store.get("telegram")
        return self.store.unseal(value) if value else {"token": "", "chat_id": "", "enabled": False}

    @staticmethod
    def call(config, method, payload, timeout=15):
        # Never log httpx exceptions: their URLs contain the bot token.
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(f"https://api.telegram.org/bot{config['token']}/{method}", json=payload)
                data = response.json()
            if response.status_code != 200 or not data.get("ok"):
                raise ValueError("텔레그램 요청 실패: 봇 토큰, Chat ID와 봇 대화 시작 여부를 확인하세요.")
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError):
            raise ValueError("텔레그램 연결 실패: 토큰, Chat ID, 네트워크를 확인하세요.") from None

    def send(self, config, message):
        self.call(config, "sendMessage", {"chat_id": config["chat_id"], "text": message[:4000]})

    def notify(self, job):
        config = self.config()
        if not config["enabled"]:
            return
        labels = {"succeeded": "✅ 전송 성공", "failed": "❌ 전송 실패", "cancelled": "⏹ 전송 취소"}
        message = (f"{labels[job['status']]}\n"
                   f"{job.get('profile_name') or '원격 서버'} · {job.get('protocol', 'ftp').upper()} · {'서버 → NAS' if job['direction'] == 'download' else 'NAS → 서버'} · "
                   f"{'이동' if job['mode'] == 'move' else '복사'}\n"
                   f"완료 파일: {job['completed_files']:,} / {job['total_files']:,}개\n"
                   f"완료 용량: {size(job['completed_bytes'])} / {size(job['total_bytes'])}\n"
                   f"작업: {job['id'][:8]}")
        if job.get("error"):
            message += "\n오류: " + job["error"][:1200]
        if job.get("warnings"):
            message += "\n" + "\n".join(job["warnings"])[:1000]
        self.send(config, message)

    def status(self):
        jobs = [j for j in reversed(self.store.jobs()) if j["status"] in ("running", "planning", "queued", "paused")]
        if not jobs:
            return "진행중인 작업 없음"
        lines = []
        for job in jobs[:10]:
            progress = min(100, job["transferred_bytes"] * 100 / job["total_bytes"]) if job["total_bytes"] else 0
            label = "⏸ 일시정지" if job["status"] == "paused" else "⏳ 일시정지 요청 중" if job.get("pause_requested") else "⏳ 대기" if job["status"] == "queued" else "🔄 전송 중"
            lines.append(f"{label} · {job['id'][:8]}\n"
                         f"{job.get('profile_name') or '원격 서버'} · {job.get('protocol', 'ftp').upper()} · {'서버 → NAS' if job['direction'] == 'download' else 'NAS → 서버'} · {progress:.1f}%\n"
                         f"{job['completed_files']:,}/{job['total_files']:,}개 · "
                         f"{size(job['transferred_bytes'])}/{size(job['total_bytes'])}\n{job['current_file']}")
        return "\n\n".join(lines)

    def handle_message(self, config, message):
        if (str(message.get("chat", {}).get("id")) != str(config["chat_id"])
                or message.get("chat", {}).get("type") != "private"):
            return
        parts = message.get("text", "").split()
        if not parts:
            return
        command = parts[0].split("@")[0]
        if command == "/status":
            reply = self.status()
        elif command in ("/pause", "/resume", "/cancel"):
            try:
                if len(parts) > 2:
                    raise ValueError("명령 뒤에는 작업 ID 하나만 입력하세요.")
                action, job_id = command[1:], parts[1] if len(parts) == 2 else None
                job = self.worker.control(action, job_id) if self.worker else control_job(self.store, action, job_id)
                labels = {"pause": "⏸ 일시정지 요청을 접수했습니다.", "resume": "▶️ 전송 재개를 요청했습니다.", "cancel": "⏹ 취소 요청을 접수했습니다."}
                reply = f"{labels[action]}\n작업: {job['id'][:8]}"
                if action == "pause":
                    reply += "\n완료 파일은 보존되며, 재개하면 미완료 파일을 처음부터 다시 전송합니다."
            except ValueError as exc:
                reply = str(exc)
        elif command in ("/start", "/help"):
            reply = ("nasrelay에 연결되었습니다.\n/status — 진행률·작업 ID\n"
                     "/pause [작업ID] — 일시정지\n/resume [작업ID] — 재개\n/cancel [작업ID] — 취소\n"
                     "작업이 여러 개면 /status에서 확인한 ID 8자 이상을 입력하세요.\n전송 완료·실패·취소 시 알림을 보냅니다.")
        else:
            return
        self.send(config, reply)

    def run(self):
        while not self.stop.is_set():
            config = self.config()
            if not config["enabled"]:
                self.stop.wait(2)
                continue
            try:
                offset = self.store.get("telegram_offset", 0)
                updates = self.call(config, "getUpdates", {"offset": offset, "timeout": 20,
                                                          "allowed_updates": ["message"]}, timeout=25)
                if self.config() != config:
                    continue
                for update in updates:
                    self.handle_message(config, update.get("message", {}))
                    self.store.set("telegram_offset", update["update_id"] + 1)
            except Exception:
                log.warning("Telegram polling failed; retrying in 5 seconds")
                self.stop.wait(5)

    def outbox(self):
        # Telegram latency cannot block the transfer worker.
        while not self.stop.is_set():
            job = next((j for j in reversed(self.store.jobs())
                        if j["status"] in ("succeeded", "failed", "cancelled") and not j.get("notified")), None)
            if not job:
                self.stop.wait(2)
                continue
            try:
                self.notify(job)
                with self.store.lock:
                    latest = self.store.job(job["id"])
                    if latest["status"] == job["status"] and latest["finished_at"] == job["finished_at"]:
                        self.store.patch(job["id"], notified=True)
            except Exception:
                log.warning("Telegram notification failed; will retry in 10 seconds")
                self.stop.wait(10)

    def start(self):
        self.thread.start()
        self.notifier.start()

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=30)
        self.notifier.join(timeout=20)
