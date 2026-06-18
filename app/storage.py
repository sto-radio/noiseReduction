import json
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import Settings


STATUS = {
    0: "File Upload",
    1: "Waiting",
    2: "Error",
    3: "Done",
    4: "Audio Processing",
    5: "Audio Encoding",
    10: "Production Not Started Yet",
}


class JobStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.work_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = self._new_id()
        now = utc_now()
        title = payload.get("title") or payload.get("metadata", {}).get("title") or job_id
        job = {
            "uuid": job_id,
            "status": 10,
            "status_string": STATUS[10],
            "creation_time": now,
            "change_time": now,
            "preset": payload.get("preset") or "caeDeepFilterNet3Clean",
            "metadata": payload.get("metadata") or {"title": title},
            "output_basename": payload.get("output_basename") or title,
            "output_files": payload.get("output_files") or [],
            "input_file": "",
            "input_path": "",
            "output_path": "",
            "webhook": payload.get("webhook") or "",
            "error_status": None,
            "error_message": "",
            "warning_status": None,
            "warning_message": "",
            "statistics": {},
            "pipeline": {},
        }
        self.save(job)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_file(job_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, job: dict[str, Any]) -> None:
        job["change_time"] = utc_now()
        job_dir = self.job_dir(job["uuid"])
        job_dir.mkdir(parents=True, exist_ok=True)
        path = self.job_file(job["uuid"])
        tmp_path = path.with_suffix(".tmp")
        with self._lock:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(job, handle, indent=2, sort_keys=True)
            tmp_path.replace(path)

    def update_status(self, job: dict[str, Any], status: int, error_message: str = "") -> dict[str, Any]:
        job["status"] = status
        job["status_string"] = STATUS.get(status, str(status))
        job["error_status"] = status if status == 2 else None
        job["error_message"] = error_message
        self.save(job)
        return job

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        for path in self.root.glob("*/job.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    jobs.append(json.load(handle))
            except json.JSONDecodeError:
                continue
        return sorted(jobs, key=lambda item: item.get("creation_time", ""), reverse=True)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def process_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "process"

    def output_dir(self, job_id: str) -> Path:
        return self.settings.output_dir / job_id

    def job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def _new_id(self) -> str:
        alphabet = string.ascii_letters + string.digits
        while True:
            job_id = "".join(secrets.choice(alphabet) for _ in range(22))
            if not self.job_file(job_id).exists():
                return job_id


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
