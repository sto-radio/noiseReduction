# CAE DeepFilterNet3 cleaner

Servicio local para limpiar entrevistas con ruido de trafico/coche desde CAE de Digasystem usando el tipo de plugin `XAUDIO`.

Cadena de proceso:

```text
Audio entrevista con trafico/coche
-> ffmpeg WAV 48 kHz
-> DeepFilterNet3
-> ffmpeg loudnorm EBU R128
-> audio limpio
```

## Requisitos

- Docker con soporte Compose.
- Driver NVIDIA compatible con CUDA 12.x.
- NVIDIA Container Toolkit habilitado para Docker.
- GPU recomendada: RTX 3080. El i7-12700K queda como respaldo para `ffmpeg` y para partes CPU.

Comprueba la GPU dentro de Docker con:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## Arranque

PowerShell:

```powershell
Copy-Item .env.example .env
docker compose build
docker compose up -d
curl.exe http://localhost:8080/health
```

Bash:

```bash
cp .env.example .env
docker compose build
docker compose up -d
curl http://localhost:8080/health
```

El primer `build` instala PyTorch CUDA, DeepFilterNet y descarga el modelo `DeepFilterNet3` en la imagen.

## Configuracion recomendada en CAE

En `Options > Smart Audio`:

- `Type`: `XAUDIO`
- `Display Name`: `DeepFilterNet3 Cleaner`
- `API Url`: `http://IP_DEL_HOST_DOCKER:8080/api`
- `API Key`: vacio, salvo que CAE permita enviarlo explicitamente
- `API Config`: `DeepFilterNet3 interview traffic cleaner`

Si CAE corre en la misma maquina que Docker, prueba primero con `http://localhost:8080/api`. Si CAE corre en otro equipo, usa la IP o DNS del host Docker y deja abierto el puerto `8080`.

El endpoint XAUDIO que usa CAE es:

```text
POST /api/fileLoudnessNormalizer?outExt=.wav&options=[]-23[TP]-1[LRA]15[OFFSET]0
Form Data: input_file=(binary)
Response: application/octet-stream
```

## Prueba manual

```powershell
curl.exe --globoff `
  -F "input_file=@input/entrevista.wav" `
  "http://localhost:8080/api/fileLoudnessNormalizer?outExt=.wav&options=[]-23[TP]-1[LRA]15[OFFSET]0" `
  --output output/entrevista_limpia.wav
```

La API Auphonic anterior sigue disponible para pruebas o compatibilidad:

```powershell
curl.exe -H "Authorization: bearer change-me" `
  -F "preset=caeDeepFilterNet3Clean" `
  -F "title=entrevista_trafico" `
  -F "input_file=@input/entrevista.wav" `
  -F "action=start" `
  http://localhost:8080/api/simple/productions.json
```

## Carpetas

- `input/`: audios que quieras referenciar por nombre desde la API.
- `output/`: resultados finales, separados por UUID de produccion.
- `work/`: metadatos, temporales y trazas de cada produccion.

## Ajustes principales

Los valores viven en `.env` o en `docker-compose.yml`:

- `TARGET_LOUDNESS_LUFS=-18`: destino recomendable para entrevistas/voz en entorno broadcast interno. Cambialo a `-16` si quieres un resultado mas fuerte tipo podcast.
- `TRUE_PEAK_DBTP=-1.5`: techo de pico real.
- `LRA=11`: rango de loudness.
- `OUTPUT_FORMAT=wav`: tambien admite `mp3`, `flac`, `m4a` o `aac`.
- `DEEPFILTER_POSTFILTER=true`: refuerza la reduccion en secciones muy ruidosas.

## CLI sin CAE

```bash
docker compose run --rm noise-reduction \
  python3 scripts/process_audio.py /data/input/entrevista.wav --basename entrevista_limpia
```
