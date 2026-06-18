import asyncio
import base64
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from .audio_pipeline import (
    PipelineError,
    md5_file,
    process_audio,
    reset_directory,
    safe_filename,
    size_string,
)
from .settings import Settings, get_settings
from .storage import STATUS, JobStore

settings = get_settings()
store = JobStore(settings)
app = FastAPI(title="CAE DeepFilterNet3 Cleaner", version="0.1.0")


def auth_dependency(
    authorization: str | None = Header(default=None),
    bearer_token: str | None = Query(default=None),
) -> None:
    expected = settings.api_token.strip()
    if not expected:
        return
    candidates = []
    if bearer_token:
        candidates.append(bearer_token)
    if authorization:
        candidates.append(authorization)
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            candidates.append(value.strip())
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(value).decode("utf-8")
            except Exception:
                decoded = ""
            username, _, password = decoded.partition(":")
            candidates.extend([username, password])
    if expected not in {candidate.strip() for candidate in candidates if candidate}:
        raise HTTPException(status_code=401, detail="Invalid API token")


def api_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {
            "status_code": status_code,
            "form_errors": {},
            "error_code": None,
            "error_message": "",
            "data": data,
        },
        status_code=status_code,
    )


def api_error(message: str, status_code: int = 400, error_code: str = "error") -> JSONResponse:
    return JSONResponse(
        {
            "status_code": status_code,
            "form_errors": {},
            "error_code": error_code,
            "error_message": message,
            "data": None,
        },
        status_code=status_code,
    )


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "CAE DeepFilterNet3 Cleaner",
        "status": "ok",
        "api": "/api",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": settings.deepfilter_model,
        "target_loudness_lufs": settings.target_loudness_lufs,
        "output_format": settings.output_format,
    }


@app.get("/api/user.json", dependencies=[Depends(auth_dependency)])
def user() -> JSONResponse:
    return api_response(
        {
            "username": "local-cae-cleaner",
            "email": "local@localhost",
            "credits": 999999.0,
            "onetime_credits": 999999.0,
            "recurring_credits": 0.0,
            "notification_email": False,
            "error_email": False,
            "warning_email": False,
        }
    )


@app.get("/api/me.json", dependencies=[Depends(auth_dependency)])
def me() -> JSONResponse:
    return user()


@app.get("/api/info/production_status.json", dependencies=[Depends(auth_dependency)])
def production_status() -> JSONResponse:
    return api_response({str(key): value for key, value in STATUS.items()})


@app.get("/api/info/output_files.json", dependencies=[Depends(auth_dependency)])
def output_files_info() -> JSONResponse:
    return api_response(output_files_data())


@app.get("/api/info/algorithms.json", dependencies=[Depends(auth_dependency)])
def algorithms_info() -> JSONResponse:
    return api_response(algorithms_data())


@app.get("/api/info.json", dependencies=[Depends(auth_dependency)])
def info() -> JSONResponse:
    return api_response(
        {
            "production_status": {str(key): value for key, value in STATUS.items()},
            "output_files": output_files_data(),
            "algorithms": algorithms_data(),
            "service_types": {},
        }
    )


@app.get("/api/presets.json", dependencies=[Depends(auth_dependency)])
def list_presets(
    uuids_only: int = Query(default=0),
    minimal_data: int = Query(default=0),
) -> JSONResponse:
    presets = load_presets()
    if uuids_only:
        return api_response([preset["uuid"] for preset in presets])
    if minimal_data:
        return api_response(
            [
                {
                    "uuid": preset["uuid"],
                    "preset_name": preset["name"],
                    "metadata": {"title": preset["name"]},
                    "is_multitrack": False,
                }
                for preset in presets
            ]
        )
    return api_response([preset_to_auphonic(preset) for preset in presets])


@app.get("/api/preset/{preset_id}.json", dependencies=[Depends(auth_dependency)])
def get_preset(preset_id: str) -> JSONResponse:
    preset = find_preset(preset_id)
    if preset is None:
        return api_error("Preset not found", 404, "not_found")
    return api_response(preset_to_auphonic(preset))


@app.get("/api/productions.json", dependencies=[Depends(auth_dependency)])
def list_productions(
    limit: int = Query(default=10),
    offset: int = Query(default=0),
    uuids_only: int = Query(default=0),
    minimal_data: int = Query(default=0),
) -> JSONResponse:
    jobs = store.list()[offset : offset + max(1, min(limit, 100))]
    if uuids_only:
        return api_response([job["uuid"] for job in jobs])
    if minimal_data:
        return api_response([job_to_minimal(job) for job in jobs])
    return api_response([job_to_auphonic(job) for job in jobs])


@app.post("/api/productions.json", dependencies=[Depends(auth_dependency)])
async def create_production(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    payload = await request.json()
    metadata = payload.get("metadata") or {}
    job = store.create(
        {
            "preset": payload.get("preset"),
            "metadata": metadata,
            "title": metadata.get("title"),
            "output_basename": payload.get("output_basename"),
            "output_files": payload.get("output_files"),
            "webhook": payload.get("webhook"),
        }
    )
    if payload.get("input_file"):
        await attach_input(job, payload["input_file"])
    if str(payload.get("action", "")).lower() == "start":
        enqueue_processing(job["uuid"], background_tasks)
    return api_response(job_to_auphonic(store.get(job["uuid"]) or job))


@app.post("/api/simple/productions.json", dependencies=[Depends(auth_dependency)])
async def simple_production(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    form = await request.form()
    title = str(form.get("title") or "")
    metadata = {"title": title} if title else {}
    job = store.create(
        {
            "preset": form.get("preset"),
            "metadata": metadata,
            "title": title,
            "output_basename": form.get("output_basename") or title,
            "webhook": form.get("webhook"),
        }
    )
    input_value = form.get("input_file")
    if input_value:
        await attach_input(job, input_value)
    if str(form.get("action", "")).lower() == "start":
        enqueue_processing(job["uuid"], background_tasks)
    return api_response(job_to_auphonic(store.get(job["uuid"]) or job))


@app.get("/api/production/{job_id}.json", dependencies=[Depends(auth_dependency)])
def get_production(job_id: str) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        return api_error("Production not found", 404, "not_found")
    return api_response(job_to_auphonic(job))


@app.post("/api/production/{job_id}.json", dependencies=[Depends(auth_dependency)])
async def update_production(job_id: str, request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        return api_error("Production not found", 404, "not_found")
    payload = await request.json()
    for key in ("metadata", "output_files", "output_basename", "webhook"):
        if key in payload:
            job[key] = payload[key]
    if payload.get("input_file"):
        await attach_input(job, payload["input_file"])
    store.save(job)
    if str(payload.get("action", "")).lower() == "start":
        enqueue_processing(job_id, background_tasks)
    return api_response(job_to_auphonic(store.get(job_id) or job))


@app.post("/api/production/{job_id}/upload.json", dependencies=[Depends(auth_dependency)])
async def upload_production_input(job_id: str, request: Request) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        return api_error("Production not found", 404, "not_found")
    form = await request.form()
    input_value = form.get("input_file")
    if not input_value:
        return api_error("Missing input_file", 400, "missing_input_file")
    await attach_input(job, input_value)
    return api_response(job_to_auphonic(store.get(job_id) or job))


@app.post("/api/production/{job_id}/start.json", dependencies=[Depends(auth_dependency)])
def start_production(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        return api_error("Production not found", 404, "not_found")
    enqueue_processing(job_id, background_tasks)
    return api_response(job_to_auphonic(store.get(job_id) or job))


@app.get("/api/production/{job_id}/status.json", dependencies=[Depends(auth_dependency)])
def production_status_query(job_id: str) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        return api_error("Production not found", 404, "not_found")
    return api_response({"status": job["status"], "status_string": job["status_string"]})


@app.get("/api/download/audio-result/{job_id}/{filename}", dependencies=[Depends(auth_dependency)])
def download_result(job_id: str, filename: str) -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production not found")
    output_path = Path(job.get("output_path") or "")
    if not output_path.exists() or output_path.name != Path(filename).name:
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(output_path, filename=output_path.name)


def enqueue_processing(job_id: str, background_tasks: BackgroundTasks) -> None:
    job = store.get(job_id)
    if job is None:
        return
    if not job.get("input_path"):
        store.update_status(job, 2, "No input file attached")
        return
    store.update_status(job, 1)
    background_tasks.add_task(process_job, job_id)


def process_job(job_id: str) -> None:
    job = store.get(job_id)
    if job is None:
        return
    store.update_status(job, 4)
    try:
        input_path = Path(job["input_path"])
        output_dir = store.output_dir(job_id)
        process_dir = store.process_dir(job_id)
        reset_directory(process_dir)
        output_basename = job.get("output_basename") or input_path.stem
        result = process_audio(input_path, output_dir, process_dir, output_basename, settings)
        output_path = result.output_path
        job["output_path"] = str(output_path)
        job["input_file"] = input_path.name
        job["output_files"] = [output_file_payload(job_id, output_path)]
        job["pipeline"] = {
            "input": result.input_info,
            "output": result.output_info,
            "loudnorm": result.loudnorm,
            "commands": [
                {
                    "args": command.args,
                    "elapsed_seconds": command.elapsed_seconds,
                    "stderr_tail": command.stderr[-2000:],
                }
                for command in result.commands
            ],
        }
        job["statistics"] = {
            "levels": {
                "output": {
                    "loudness": [settings.target_loudness_lufs, "LUFS"],
                    "peak": [settings.true_peak_dbtp, "dBTP"],
                }
            }
        }
        store.update_status(job, 3)
    except Exception as exc:
        message = str(exc)
        if not isinstance(exc, PipelineError):
            message = f"Unexpected processing error: {message}"
        store.update_status(job, 2, message)
    finally:
        latest = store.get(job_id)
        if latest and latest.get("webhook"):
            notify_webhook(latest)


async def attach_input(job: dict[str, Any], input_value: Any) -> Path:
    input_dir = store.input_dir(job["uuid"])
    input_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(input_value, StarletteUploadFile):
        path = await save_upload(input_value, input_dir)
    else:
        path = await asyncio.to_thread(materialize_external_input, str(input_value), input_dir)
    job["input_file"] = path.name
    job["input_path"] = str(path)
    store.update_status(job, 10)
    return path


async def save_upload(upload: StarletteUploadFile, target_dir: Path) -> Path:
    filename = safe_filename(Path(upload.filename or "input.wav").name)
    target = target_dir / filename
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Upload too large")
            handle.write(chunk)
    return target


def materialize_external_input(value: str, target_dir: Path) -> Path:
    value = value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Empty input_file")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        filename = safe_filename(Path(parsed.path).name or "input_audio")
        target = target_dir / filename
        download_file(value, target)
        return target

    source = resolve_local_input(value)
    target = target_dir / safe_filename(source.name)
    if source.stat().st_size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Input file too large")
    shutil.copyfile(source, target)
    return target


def resolve_local_input(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = settings.input_dir / value
    candidate = candidate.resolve()
    allowed_roots = [settings.data_root.resolve(), settings.input_dir.resolve()]
    if not any(is_relative_to(candidate, root) for root in allowed_roots):
        raise HTTPException(status_code=400, detail="Local input path is outside the mounted data directory")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Input file not found: {value}")
    return candidate


def download_file(url: str, target: Path) -> None:
    max_bytes = settings.max_upload_mb * 1024 * 1024
    downloaded = 0
    with requests.get(url, stream=True, timeout=settings.request_timeout_seconds) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Downloaded input too large")
                handle.write(chunk)


def notify_webhook(job: dict[str, Any]) -> None:
    try:
        requests.post(
            job["webhook"],
            json=json.loads(api_response(job_to_auphonic(job)).body.decode("utf-8")),
            timeout=settings.request_timeout_seconds,
        )
    except requests.RequestException:
        pass


def job_to_minimal(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": job["status"],
        "status_string": job["status_string"],
        "uuid": job["uuid"],
        "is_multitrack": False,
        "creation_time": job["creation_time"],
        "metadata": job.get("metadata") or {},
        "output_files": job.get("output_files") or [],
    }


def job_to_auphonic(job: dict[str, Any]) -> dict[str, Any]:
    input_info = (job.get("pipeline") or {}).get("input") or {}
    output_info = (job.get("pipeline") or {}).get("output") or {}
    return {
        "status": job["status"],
        "status_string": job["status_string"],
        "uuid": job["uuid"],
        "output_basename": job.get("output_basename") or "",
        "output_files": job.get("output_files") or [],
        "outgoing_services": [],
        "chapters": [],
        "metadata": job.get("metadata") or {},
        "algorithms": {
            "filtering": True,
            "normloudness": True,
            "denoise": True,
            "leveler": False,
            "loudnesstarget": settings.target_loudness_lufs,
            "denoiseamount": 100,
        },
        "input_file": job.get("input_file") or "",
        "length": output_info.get("length") or input_info.get("length"),
        "length_timestring": output_info.get("length_timestring") or input_info.get("length_timestring"),
        "channels": output_info.get("channels") or input_info.get("channels"),
        "samplerate": output_info.get("samplerate") or input_info.get("samplerate"),
        "bitrate": output_info.get("bitrate") or input_info.get("bitrate"),
        "format": output_info.get("format") or input_info.get("format"),
        "has_video": False,
        "creation_time": job["creation_time"],
        "change_time": job["change_time"],
        "start_allowed": bool(job.get("input_path")) and job["status"] not in {3, 4, 5},
        "change_allowed": job["status"] not in {4, 5},
        "status_page": f"{settings.public_base_url.rstrip('/')}/api/production/{job['uuid']}.json",
        "edit_page": f"{settings.public_base_url.rstrip('/')}/api/production/{job['uuid']}.json",
        "error_status": job.get("error_status"),
        "error_message": job.get("error_message") or "",
        "warning_status": job.get("warning_status"),
        "warning_message": job.get("warning_message") or "",
        "webhook": job.get("webhook") or "",
        "is_multitrack": False,
        "statistics": job.get("statistics") or {},
    }


def output_file_payload(job_id: str, path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    ending = path.suffix.lstrip(".")
    output_format = "aac" if ending == "m4a" else ending
    return {
        "format": output_format,
        "ending": ending,
        "suffix": "",
        "filename": path.name,
        "split_on_chapters": False,
        "bitrate": None,
        "mono_mixdown": settings.mono_mixdown,
        "size": size,
        "size_string": size_string(size),
        "download_url": (
            f"{settings.public_base_url.rstrip('/')}/api/download/audio-result/"
            f"{job_id}/{path.name}"
        ),
        "outgoing_services": [],
        "checksum": md5_file(path),
    }


def load_presets() -> list[dict[str, Any]]:
    path = Path("config/presets.json")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def output_files_data() -> dict[str, Any]:
    return {
        "wav": {
            "type": "lossless",
            "display_name": "WAV 16-bit PCM",
            "endings": ["wav"],
        },
        "flac": {
            "type": "lossless",
            "display_name": "FLAC",
            "endings": ["flac"],
        },
        "mp3": {
            "type": "lossy",
            "display_name": "MP3",
            "default_bitrate": settings.mp3_bitrate.rstrip("k"),
            "endings": ["mp3"],
        },
        "aac": {
            "type": "lossy",
            "display_name": "AAC (M4A)",
            "default_bitrate": settings.aac_bitrate.rstrip("k"),
            "endings": ["m4a"],
        },
    }


def algorithms_data() -> dict[str, Any]:
    return {
        "deepfilternet": {
            "display_name": "DeepFilterNet3 speech enhancement",
            "enabled": True,
        },
        "normloudness": {
            "display_name": "EBU R128 loudness normalization",
            "enabled": True,
            "target": settings.target_loudness_lufs,
            "true_peak": settings.true_peak_dbtp,
            "lra": settings.lra,
        },
    }


def find_preset(preset_id: str) -> dict[str, Any] | None:
    for preset in load_presets():
        if preset["uuid"] == preset_id or preset["name"] == preset_id:
            return preset
    return None


def preset_to_auphonic(preset: dict[str, Any]) -> dict[str, Any]:
    output_format = preset.get("output_format") or settings.output_format
    return {
        "uuid": preset["uuid"],
        "preset_name": preset["name"],
        "metadata": {"title": preset["name"]},
        "output_basename": "",
        "outgoing_services": [],
        "output_files": [
            {
                "format": output_format,
                "ending": output_format,
                "filename": "",
                "suffix": "",
                "split_on_chapters": False,
                "mono_mixdown": settings.mono_mixdown,
                "size": None,
                "size_string": "",
                "download_url": None,
                "outgoing_services": [],
            }
        ],
        "algorithms": {
            "filtering": True,
            "normloudness": True,
            "denoise": True,
            "leveler": False,
            "loudnesstarget": preset.get("target_loudness_lufs", settings.target_loudness_lufs),
            "denoiseamount": 100,
        },
        "creation_time": "2026-06-18T00:00:00Z",
        "is_multitrack": False,
        "webhook": "",
        "description": preset.get("description", ""),
    }


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
