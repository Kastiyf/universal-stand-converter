import gc
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from supabase import Client, create_client

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = (
    os.environ.get("SUPABASE_SECRET_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
)
SUPABASE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET") or "models"
NATIVE_CONVERTER = os.environ.get(
    "NATIVE_CONVERTER",
    str(BASE_DIR / "bin" / "skp2glb"),
)

try:
    CONVERSION_TIMEOUT_SECONDS = int(
        os.environ.get("SKP_CONVERSION_TIMEOUT_SECONDS", "1800")
    )
except ValueError:
    CONVERSION_TIMEOUT_SECONDS = 1800

if not SUPABASE_URL:
    raise RuntimeError("Falta SUPABASE_URL.")

if not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "Falta SUPABASE_SECRET_KEY o SUPABASE_SERVICE_ROLE_KEY."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

app = FastAPI(title="Universal Stand SKP Converter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

conversion_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="skp-converter",
)

conversion_jobs = {}
conversion_jobs_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_job(project_id, original_name):
    job_id = uuid.uuid4().hex

    with conversion_jobs_lock:
        conversion_jobs[job_id] = {
            "id": job_id,
            "projectId": project_id,
            "fileName": original_name,
            "status": "queued",
            "stage": "queued",
            "message": "Conversión en cola...",
            "progress": 0,
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "result": None,
            "error": None,
        }

    return job_id


def update_job(job_id, **changes):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)

        if not job:
            return

        job.update(changes)
        job["updatedAt"] = now_iso()


def get_job(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)

        if not job:
            return None

        return dict(job)


def safe_name(value):
    value = Path(value or "model.skp").stem
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return value or "model"


def run_native_conversion(
    job_id,
    temporary_directory,
    skp_path,
    glb_path,
    project_id,
    project_slug,
    glb_name,
):
    started = datetime.now(timezone.utc)

    try:
        update_job(
            job_id,
            status="running",
            stage="converting",
            message="OpenSKP C++ está procesando el SKP...",
            progress=10,
        )

        converter = Path(NATIVE_CONVERTER)

        if not converter.exists():
            raise RuntimeError(
                f"No existe el conversor nativo: {converter}"
            )

        print(
            f"[SKP {job_id}] Ejecutando conversor nativo: {converter}",
            flush=True,
        )

        process = subprocess.run(
            [str(converter), str(skp_path), str(glb_path)],
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )

        if process.stdout:
            print(
                f"[SKP {job_id}] native stdout:\n{process.stdout}",
                flush=True,
            )

        if process.stderr:
            print(
                f"[SKP {job_id}] native stderr:\n{process.stderr}",
                flush=True,
            )

        if process.returncode != 0:
            detail = (
                process.stderr.strip()
                or process.stdout.strip()
                or f"El conversor terminó con código {process.returncode}."
            )

            raise RuntimeError(detail[-8000:])

        if not glb_path.exists() or glb_path.stat().st_size == 0:
            raise RuntimeError("OpenSKP no generó un GLB válido.")

        glb_size = glb_path.stat().st_size

        update_job(
            job_id,
            stage="uploading",
            message="GLB generado. Subiendo el modelo a Supabase...",
            progress=70,
            glbSize=glb_size,
        )

        storage_path = f"projects/{project_id}/{glb_name}"

        with glb_path.open("rb") as glb_file:
            (
                supabase.storage
                .from_(SUPABASE_BUCKET)
                .upload(
                    storage_path,
                    glb_file,
                    {
                        "content-type": "model/gltf-binary",
                        "cache-control": "31536000",
                        "upsert": "true",
                    },
                )
            )

        model_url = (
            supabase.storage
            .from_(SUPABASE_BUCKET)
            .get_public_url(storage_path)
        )

        update_job(
            job_id,
            stage="updating",
            message="Modelo subido. Actualizando el proyecto...",
            progress=85,
        )

        payload = {
            "modelo": model_url,
            "updated_at": now_iso(),
        }

        response = (
            supabase
            .table("projects")
            .update(payload)
            .eq("id", project_id)
            .execute()
        )

        project_updated = bool(response.data)

        if not project_updated and project_slug:
            response = (
                supabase
                .table("projects")
                .update(payload)
                .eq("slug", project_slug)
                .execute()
            )

            project_updated = bool(response.data)

        if not project_updated and project_slug:
            response = (
                supabase
                .table("projects")
                .upsert(
                    {
                        "id": project_id,
                        "slug": project_slug,
                        "modelo": model_url,
                    },
                    on_conflict="id",
                )
                .execute()
            )

            project_updated = bool(response.data)

        duration = (
            datetime.now(timezone.utc) - started
        ).total_seconds()

        result = {
            "ok": True,
            "projectId": project_id,
            "modelName": glb_name,
            "modelPath": storage_path,
            "modelUrl": model_url,
            "storage": "supabase",
            "projectUpdated": project_updated,
            "durationSeconds": round(duration, 2),
            "glbSize": glb_size,
            "converter": "OpenSKP C++ 1.3.0",
            "textures": True,
        }

        update_job(
            job_id,
            status="completed",
            stage="completed",
            message="Conversión terminada correctamente.",
            progress=100,
            result=result,
        )

        print(
            f"[SKP {job_id}] Conversión terminada "
            f"en {duration:.1f}s",
            flush=True,
        )

    except subprocess.TimeoutExpired:
        update_job(
            job_id,
            status="error",
            stage="error",
            message="La conversión superó el tiempo máximo.",
            progress=0,
            error=(
                f"El conversor superó "
                f"{CONVERSION_TIMEOUT_SECONDS} segundos."
            ),
        )

    except Exception as exc:
        print(
            f"[SKP {job_id}] ERROR: {exc}",
            flush=True,
        )

        update_job(
            job_id,
            status="error",
            stage="error",
            message="No se pudo convertir o guardar el SKP.",
            progress=0,
            error=str(exc),
        )

    finally:
        shutil.rmtree(
            temporary_directory,
            ignore_errors=True,
        )

        gc.collect()


@app.get("/api/health")
def health():
    with conversion_jobs_lock:
        active_jobs = sum(
            1
            for job in conversion_jobs.values()
            if job.get("status") in {"queued", "running"}
        )

    return {
        "ok": True,
        "converter": "OpenSKP C++",
        "converterPath": NATIVE_CONVERTER,
        "nativeConverterExists": Path(NATIVE_CONVERTER).exists(),
        "storage": "Supabase",
        "bucket": SUPABASE_BUCKET,
        "activeJobs": active_jobs,
        "conversionTimeoutSeconds": CONVERSION_TIMEOUT_SECONDS,
    }


@app.post("/api/convert-skp")
async def convert_skp(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    project_slug: str = Form(""),
):
    original_name = file.filename or "model.skp"

    if not original_name.lower().endswith(".skp"):
        raise HTTPException(
            status_code=400,
            detail="Solo se permiten archivos .SKP.",
        )

    if not project_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Falta el ID del proyecto.",
        )

    token = uuid.uuid4().hex[:10]
    base_name = safe_name(project_slug or project_id)

    skp_name = f"{base_name}-{token}.skp"
    glb_name = f"{base_name}-{token}.glb"

    temporary_directory = tempfile.mkdtemp(
        prefix="universal-stand-"
    )

    skp_path = Path(temporary_directory) / skp_name
    glb_path = Path(temporary_directory) / glb_name

    try:
        with skp_path.open("wb") as destination:
            shutil.copyfileobj(
                file.file,
                destination,
            )

        file_size = skp_path.stat().st_size

        if file_size == 0:
            raise HTTPException(
                status_code=400,
                detail="El archivo SKP está vacío.",
            )

        job_id = create_job(
            project_id,
            original_name,
        )

        update_job(
            job_id,
            fileSize=file_size,
            message=(
                "Archivo recibido. "
                "Preparando conversión nativa..."
            ),
        )

        conversion_executor.submit(
            run_native_conversion,
            job_id,
            temporary_directory,
            skp_path,
            glb_path,
            project_id,
            project_slug,
            glb_name,
        )

        return {
            "ok": True,
            "accepted": True,
            "jobId": job_id,
            "status": "queued",
            "message": (
                "El archivo fue recibido "
                "y la conversión comenzó."
            ),
            "fileName": original_name,
            "fileSize": file_size,
        }

    except HTTPException:
        shutil.rmtree(
            temporary_directory,
            ignore_errors=True,
        )
        raise

    except Exception as exc:
        shutil.rmtree(
            temporary_directory,
            ignore_errors=True,
        )

        raise HTTPException(
            status_code=422,
            detail=(
                f"No se pudo iniciar "
                f"la conversión: {exc}"
            ),
        ) from exc


@app.get("/api/convert-skp/status/{job_id}")
def conversion_status(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail=(
                "No existe esa conversión "
                "o el trabajo ya fue eliminado."
            ),
        )

    return job


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "universal-stand-converter",
        "converter": "OpenSKP C++",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "10000",
            )
        ),
        reload=False,
    )
