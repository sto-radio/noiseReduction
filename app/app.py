import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI()

WORKDIR = Path("/tmp/xaudio")
WORKDIR.mkdir(parents=True, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_cmd(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def find_deepfilter_output(job_dir: Path, normalized_input: Path) -> Path:
    wav_files = [
        p for p in job_dir.glob("*.wav")
        if p.name != normalized_input.name
    ]

    if not wav_files:
        raise FileNotFoundError("DeepFilterNet no generó ningún WAV")

    return max(wav_files, key=lambda p: p.stat().st_mtime)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/fileLoudnessNormalizer")
async def file_loudness_normalizer(
    input_file: UploadFile = File(...),
    outExt: str = Query(".wav"),
    options: str = Query("")
):
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    raw_input = job_dir / "input_original"
    normalized_input = job_dir / "input.wav"
    output_path = job_dir / "output.wav"

    with open(raw_input, "wb") as f:
        f.write(await input_file.read())

    # Convertimos a WAV 48 kHz PCM para DeepFilterNet
    run_cmd([
        "ffmpeg", "-y",
        "-i", str(raw_input),
        "-vn",
        "-ac", "1",
        "-ar", "48000",
        "-sample_fmt", "s16",
        "-c:a", "pcm_s16le",
        str(normalized_input)
    ])

    # Procesamiento DeepFilterNet
    run_cmd([
        "deepFilter",
        str(normalized_input),
        "--output-dir", str(job_dir)
    ])

    deepfilter_result = find_deepfilter_output(job_dir, normalized_input)
    shutil.move(str(deepfilter_result), str(output_path))

    return FileResponse(
        output_path,
        media_type="application/octet-stream",
        filename="output.wav",
        headers={
            "Content-Disposition": 'attachment; filename="output.wav"'
        }
    )