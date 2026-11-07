import logging
import os
import posixpath
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone

from .storage import create_remote, NAS, RetryableTransferError, clean_path, join

log = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


def public_job(job, rates=None):
    fields = ("id", "direction", "mode", "sources", "destination", "status", "created_at", "finished_at",
              "total_files", "total_bytes", "completed_files", "completed_bytes", "transferred_bytes",
              "current_file", "error", "cancel_requested", "pause_requested", "warnings",
              "profile_id", "profile_name", "protocol")
    result = {k: job.get(k) for k in fields}
    total = job.get("total_bytes", 0)
    result["progress"] = min(100, round(job.get("transferred_bytes", 0) * 100 / total, 1)) if total else (100 if job["status"] == "succeeded" else 0)
    result["failed_files"] = sum(bool(x.get("error")) for x in job.get("files", []))
    result["speed_bytes_per_second"] = rates.current(job['id']) if rates and job['status'] == 'running' else 0.0
    return result


class Interrupted(Exception):
    pass


class Paused(Interrupted):
    pass


def control_job(store, action, job_id=None):
    with store.lock:
        jobs = [job for job in store.jobs() if job["status"] not in TERMINAL]
        if job_id:
            if len(job_id) < 8:
                raise ValueError("작업 ID는 8자 이상 입력하세요. /status에서 확인할 수 있습니다.")
            matches = [job for job in jobs if job["id"].startswith(job_id)]
            if len(matches) != 1:
                raise ValueError("진행 중인 작업을 찾을 수 없거나 ID가 모호합니다. /status를 확인하세요.")
            job = matches[0]
        else:
            eligible = jobs if action == "cancel" else [job for job in jobs if (job["status"] == "paused" or bool(job.get("pause_requested"))) == (action == "resume")]
            running = [job for job in eligible if job["status"] in ("running", "planning")]
            if action != "resume" and running:
                eligible = running
            if len(eligible) != 1:
                raise ValueError("대상 작업이 없거나 여러 개입니다. /status에서 ID를 확인해 명령 뒤에 입력하세요.")
            job = eligible[0]
        if action == "pause":
            if job.get("cancel_requested") or job["status"] == "paused":
                raise ValueError("이미 정지되었거나 취소 중인 작업입니다.")
            changes = {"pause_requested": True}
            if job["status"] == "queued":
                changes["status"] = "paused"
        elif action == "resume":
            if job.get("cancel_requested") or not (job["status"] == "paused" or job.get("pause_requested")):
                raise ValueError("일시정지한 작업만 재개할 수 있습니다.")
            changes = {"pause_requested": False}
            if job["status"] == "paused":
                changes["status"] = "queued"
        elif action == "cancel":
            changes = {"cancel_requested": True, "pause_requested": False}
            if job["status"] in ("paused", "queued"):
                changes.update(status="cancelled", finished_at=now(), current_file="")
        else:
            raise ValueError("지원하지 않는 작업 명령입니다.")
        return public_job(store.patch(job["id"], **changes), store.transfer_rates)


class TransferWorker:
    def __init__(self, store, nas_root):
        self.store = store
        self.nas = NAS(nas_root)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="transfer-worker", daemon=True)
        self.active = {}
        self.active_lock = threading.RLock()
        self.max_parallel = 4

    def start(self):
        for job in self.store.jobs():
            if job["status"] in ("running", "planning"):
                self.store.patch(job["id"], status="paused" if job.get("pause_requested") else "queued", current_file="재시작 후 복구 대기")
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=40)

    def is_alive(self):
        with self.active_lock:
            return self.thread.is_alive() or any(item[0].is_alive() for item in self.active.values())

    def submit(self, profile, direction, mode, sources, destination):
        normalized = sorted(set(clean_path(p) for p in sources), key=lambda p: (len(p), p))
        if not normalized or "/" in normalized:
            raise ValueError("최상위 폴더 대신 그 안의 파일 또는 폴더를 선택하세요.")
        # A folder already includes all its descendants.
        selected = []
        for path in normalized:
            if not any(path.startswith(parent + "/") for parent in selected):
                selected.append(path)
        if sum(j["status"] not in TERMINAL for j in self.store.jobs()) >= 100:
            raise ValueError("대기열이 가득 찼습니다. 작업 완료 후 다시 시도하세요.")
        job = {"id": uuid.uuid4().hex, "profile": self.store.seal(profile), "direction": direction,
               "profile_id": profile.get("id"), "profile_name": profile.get("name", "원격 서버"),
               "protocol": profile.get("protocol", "ftp"), "mode": mode, "sources": selected, "destination": clean_path(destination),
               "status": "queued", "created_at": now(), "finished_at": None, "files": [], "directories": [],
               "planned": False, "total_files": 0, "total_bytes": 0, "completed_files": 0,
               "completed_bytes": 0, "transferred_bytes": 0, "current_file": "", "error": "",
               "cancel_requested": False, "pause_requested": False, "warnings": [], "notified": False}
        self.store.add_job(job)
        self.wake.set()
        return public_job(job)

    def control(self, action, job_id=None):
        result = control_job(self.store, action, job_id)
        self.wake.set()
        return result

    def checkpoint(self, job_id):
        job = self.store.job(job_id)
        if self.stop.is_set() or job["cancel_requested"]:
            raise Interrupted()
        if job.get("pause_requested"):
            raise Paused()

    def resources(self, job):
        profile = self.store.unseal(job["profile"])
        server = (profile.get("protocol", "ftp"), profile["host"], profile.get("port", 21),
                  profile["username"], profile.get("base_path", "/"))
        nas_paths = job["sources"] if job["direction"] == "upload" else [
            join(job["destination"], posixpath.basename(path)) for path in job["sources"]]
        return server, nas_paths

    @staticmethod
    def overlap(left, right):
        return any(a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
                   for a in left for b in right)

    def run_job(self, job):
        try:
            self.execute(job)
        finally:
            self.wake.set()

    def run(self):
        while True:
            with self.active_lock:
                self.active = {key: item for key, item in self.active.items() if item[0].is_alive()}
                if self.stop.is_set() and not self.active:
                    return
            if not self.stop.is_set():
                try:
                    for job in reversed(self.store.jobs()):
                        with self.active_lock:
                            if len(self.active) >= self.max_parallel:
                                break
                            if job["status"] != "queued" or job["id"] in self.active:
                                continue
                            server, paths = self.resources(job)
                            if any(server == item[1] or self.overlap(paths, item[2]) for item in self.active.values()):
                                continue
                            thread = threading.Thread(target=self.run_job, args=(job,), name=f"transfer-{job['id'][:8]}", daemon=True)
                            self.active[job["id"]] = (thread, server, paths)
                            thread.start()
                except Exception:
                    log.exception("Worker iteration failed")
            self.wake.wait(0.2)
            self.wake.clear()

    def plan(self, job, source):
        files, directories = [], []
        seen = set()

        def walk(path, target, depth=0, info=None):
            self.checkpoint(job["id"])
            if depth > 64 or len(seen) >= 100000:
                raise ValueError("폴더 깊이 또는 파일 수 제한을 초과했습니다.")
            if path in seen:
                raise ValueError("중복 또는 순환 경로입니다.")
            seen.add(path)
            if info is None:
                info = source.stat(path)
            if info["is_dir"]:
                directories.append({"source": path, "target": target})
                for entry in source.list(path):
                    walk(entry["path"], join(target, entry["name"]), depth + 1, entry)
            else:
                files.append({"source": path, "target": target, "size": info["size"],
                              "modified": info.get("modified", ""), "phase": "pending", "error": ""})

        for path in job["sources"]:
            walk(path, join(job["destination"], posixpath.basename(path)))
        return self.store.patch(job["id"], files=files, directories=directories, planned=True,
                                total_files=len(files), total_bytes=sum(f["size"] for f in files))

    @staticmethod
    def unchanged(info, item):
        if info["is_dir"] or info["size"] != item["size"] or (item["modified"] and info.get("modified") != item["modified"]):
            raise ValueError("전송 도중 원본이 변경되었습니다. 원본을 보존합니다.")

    def execute(self, job):
        remote = None
        job_id = job["id"]
        tracking = False
        try:
            with self.store.lock:
                job = self.store.job(job_id)
                if job["status"] != "queued":
                    return
                self.checkpoint(job_id)
                self.store.patch(job_id, status="planning" if not job["planned"] else "running")
            remote = create_remote(self.store.unseal(job["profile"]))
            source, destination = (remote, self.nas) if job["direction"] == "download" else (self.nas, remote)
            if not job["planned"]:
                job = self.plan(job, source)
            self.store.patch(job_id, status="running")
            self.store.transfer_rates.start(job_id)
            tracking = True
            for directory in job["directories"]:
                self.checkpoint(job_id)
                destination.mkdir(directory["target"])
            for index, item in enumerate(job["files"]):
                self.checkpoint(job_id)
                if item["phase"] == "done" or item.get("error"):
                    continue
                self.store.patch(job_id, current_file=item["source"])
                try:
                    for attempt in range(3):
                        try:
                            self.transfer(job, index, source, destination, remote)
                            break
                        except RetryableTransferError:
                            if attempt == 2:
                                raise
                            self.checkpoint(job_id)
                            remote.close()
                            self.stop.wait(0.5 * (attempt + 1))
                            self.checkpoint(job_id)
                            remote = create_remote(self.store.unseal(job["profile"]))
                            source, destination = (remote, self.nas) if job["direction"] == "download" else (self.nas, remote)
                except Interrupted:
                    raise
                except Exception as exc:
                    item["error"] = str(exc)[:600]
                    self.store.patch(job_id, files=job["files"])
                    # A data connection error may invalidate the control connection.
                    remote.close()
                    remote = create_remote(self.store.unseal(job["profile"]))
                    source, destination = (remote, self.nas) if job["direction"] == "download" else (self.nas, remote)
            errors = [f"{f['source']}: {f['error']}" for f in job["files"] if f.get("error")]
            warnings = []
            if job["mode"] == "move" and not errors:
                for directory in reversed(job["directories"]):
                    self.checkpoint(job_id)
                    try:
                        source.rmdir(directory["source"])
                    except FileNotFoundError:
                        pass
                    except Exception:
                        warnings.append(f"원본 폴더를 보존했습니다: {directory['source']}")
            with self.store.lock:
                self.checkpoint(job_id)
                self.store.patch(job_id, status="failed" if errors else "succeeded", error="\n".join(errors)[:4000],
                                 warnings=warnings, finished_at=now(), current_file="")
        except Paused:
            with self.store.lock:
                latest = self.store.job(job_id)
                if latest["cancel_requested"]:
                    self.store.patch(job_id, status="cancelled", finished_at=now(), current_file="")
                else:
                    retained = sum(item["size"] for item in latest["files"] if item["phase"] in ("done", "copied", "staged"))
                    self.store.patch(job_id, status="paused" if latest.get("pause_requested") else "queued", transferred_bytes=retained)
        except Interrupted:
            if self.stop.is_set():
                self.store.patch(job_id, status="paused" if self.store.job(job_id).get("pause_requested") else "queued", current_file="재시작 후 복구 대기")
            else:
                self.store.patch(job_id, status="cancelled", finished_at=now(), current_file="")
        except Exception as exc:
            self.store.patch(job_id, status="failed", error=str(exc)[:4000], finished_at=now(), current_file="")
        finally:
            if tracking:
                self.store.transfer_rates.stop(job_id)
            if remote:
                remote.close()

    def transfer(self, job, index, source, destination, remote):
        item = job["files"][index]
        job_id = job["id"]
        temp = join(posixpath.dirname(item["target"]), f".ftn-{job_id}-{index}.part")
        direction = job["direction"]

        def save():
            done = [f for f in job["files"] if f["phase"] == "done"]
            return self.store.patch(job_id, files=job["files"], completed_files=len(done),
                                    completed_bytes=sum(f["size"] for f in done))

        if item["phase"] == "pending":
            self.unchanged(source.stat(item["source"]), item)
            destination.mkdir(posixpath.dirname(item["target"]))
            if destination.exists(item["target"]):
                raise FileExistsError(f"대상 파일이 이미 있습니다: {item['target']}")
            base = sum(f["size"] for f in job["files"] if f["phase"] in ("done", "copied", "staged"))
            transferred, last = 0, 0.0
            self.store.patch(job_id, transferred_bytes=base)

            def progress(data):
                nonlocal transferred, last
                self.checkpoint(job_id)
                transferred += len(data)
                self.store.transfer_rates.record(job_id, len(data))
                if time.monotonic() - last > 0.5:
                    self.store.patch(job_id, transferred_bytes=base + transferred)
                    last = time.monotonic()

            try:
                if direction == "download":
                    fd, item['destination_mode'] = self.nas.create_download_temp(temp)
                    with os.fdopen(fd, "wb") as output:
                        def receive(data):
                            output.write(data)
                            progress(data)
                        remote.download(item["source"], receive)
                        output.flush()
                        os.fsync(output.fileno())
                else:
                    with self.nas.local(item["source"]).open("rb") as input_file:
                        remote.upload(temp, input_file, progress)
                if destination.stat(temp)["size"] != item["size"] or transferred != item["size"]:
                    raise ValueError("전송 크기가 원본과 다릅니다. 원본을 보존합니다.")
                self.unchanged(source.stat(item["source"]), item)
                item["phase"] = "staged"
                save()  # Durable before publication; recovery can identify a completed rename.
                self.store.patch(job_id, transferred_bytes=base + transferred)
            except BaseException:
                with suppress(Exception):
                    destination.remove(temp)
                raise

        if item["phase"] == "staged":
            self.checkpoint(job_id)
            if destination.exists(temp):
                if destination.stat(temp)["size"] != item["size"]:
                    raise ValueError("임시 파일 크기 검증 실패")
                if direction == "download":
                    local_temp, target = self.nas.local(temp), self.nas.local(item["target"])
                    published = target.exists()
                    if published:
                        if not os.path.samefile(local_temp, target):
                            raise FileExistsError(f"대상 파일이 이미 있습니다: {item['target']}")
                    self.nas.finish_download_permissions(temp, item.get('destination_mode'))
                    if not published:
                        os.link(local_temp, target)  # Atomic, never replaces an existing NAS file.
                    local_temp.unlink()
                    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                else:
                    if destination.exists(item["target"]):
                        raise FileExistsError(f"대상 파일이 이미 있습니다: {item['target']}")
                    remote.rename(temp, item["target"])
            if destination.stat(item["target"])["size"] != item["size"]:
                raise ValueError("최종 파일 크기 검증 실패")
            item["phase"] = "copied"
            save()

        if item["phase"] == "copied":
            self.checkpoint(job_id)
            if destination.stat(item["target"])["size"] != item["size"]:
                raise ValueError("원본 삭제 전 대상 파일 검증 실패")
            if job["mode"] == "move":
                try:
                    info = source.stat(item["source"])
                except FileNotFoundError:
                    pass  # Recovery after a successful delete, before persisting done.
                else:
                    self.unchanged(info, item)
                    source.remove(item["source"])
            item["phase"] = "done"
            result = save()
            self.store.patch(job_id, transferred_bytes=max(result["transferred_bytes"], result["completed_bytes"]))
