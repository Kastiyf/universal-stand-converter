import os
import re
import shutil
import tempfile
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import openskp
from openskp import SkpFile
from openskp.export import glb

from supabase import create_client, Client


# =====================================================
# CONFIGURACIÓN
# =====================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL")

SUPABASE_SECRET_KEY = (
    os.environ.get("SUPABASE_SECRET_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
)

SUPABASE_BUCKET = (
    os.environ.get("SUPABASE_STORAGE_BUCKET")
    or "models"
)

try:
    CONVERSION_TIMEOUT_SECONDS = int(
        os.environ.get(
            "SKP_CONVERSION_TIMEOUT_SECONDS",
            "1800"
        )
    )
except ValueError:
    CONVERSION_TIMEOUT_SECONDS = 1800


if not SUPABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno SUPABASE_URL."
    )


if not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "Falta la variable de entorno SUPABASE_SECRET_KEY "
        "o SUPABASE_SERVICE_ROLE_KEY."
    )


supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SECRET_KEY
)


# =====================================================
# APLICACIÓN
# =====================================================

app = FastAPI(
    title="Universal Stand SKP Converter"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# =====================================================
# COLA DE CONVERSIONES
# =====================================================

conversion_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="skp-converter"
)

conversion_jobs = {}

conversion_jobs_lock = threading.Lock()


# =====================================================
# UTILIDADES DE JOBS
# =====================================================

def create_job(
    project_id: str,
    original_name: str
) -> str:

    job_id = uuid.uuid4().hex

    with conversion_jobs_lock:

        conversion_jobs[job_id] = {

            "id":
                job_id,

            "projectId":
                project_id,

            "fileName":
                original_name,

            "status":
                "queued",

            "stage":
                "queued",

            "message":
                "Conversión en cola...",

            "progress":
                0,

            "createdAt":
                datetime
                    .now(timezone.utc)
                    .isoformat(),

            "updatedAt":
                datetime
                    .now(timezone.utc)
                    .isoformat(),

            "result":
                None,

            "error":
                None
        }

    return job_id


def update_job(
    job_id: str,
    **changes
):

    with conversion_jobs_lock:

        job = conversion_jobs.get(
            job_id
        )

        if not job:
            return

        job.update(
            changes
        )

        job["updatedAt"] = (
            datetime
                .now(timezone.utc)
                .isoformat()
        )


def get_job(
    job_id: str
):

    with conversion_jobs_lock:

        job = conversion_jobs.get(
            job_id
        )

        if not job:
            return None

        return dict(
            job
        )


# =====================================================
# UTILIDADES
# =====================================================

def safe_name(
    value: str
) -> str:

    value = Path(
        value or "model.skp"
    ).stem

    value = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "-",
        value
    ).strip("-")

    return (
        value
        or
        "model"
    )


# =====================================================
# TRABAJO REAL DE CONVERSIÓN
# =====================================================

def run_conversion_job(

    job_id: str,

    temporary_directory: str,

    skp_path: Path,

    glb_path: Path,

    project_id: str,

    project_slug: str,

    glb_name: str

):

    started_at = (
        datetime.now(
            timezone.utc
        )
    )

    try:

        # =============================================
        # PARSEAR SKP
        # =============================================

        update_job(

            job_id,

            status="running",

            stage="parsing",

            message=
                "Analizando el archivo SKP...",

            progress=10
        )


        print(

            f"[SKP {job_id}] "
            f"Abriendo {skp_path.name}",

            flush=True

        )


        skp = SkpFile.open(

            str(
                skp_path
            )

        )


        print(

            f"[SKP {job_id}] "
            f"Ejecutando parse()...",

            flush=True

        )


        skp.parse()

        # Diagnóstico liviano: NO construir build_scene() aquí.
        # build_scene() antes de export() duplica estructuras pesadas en RAM
        # y puede provocar OOM en Render Free (512 MB), especialmente con
        # texturas e imágenes incrustadas.
        try:
            import PIL
            print(
                f"[SKP {job_id}] OpenSKP: "
                f"{getattr(openskp, '__version__', 'desconocida')} | "
                f"Pillow: {getattr(PIL, '__version__', 'desconocida')}",
                flush=True
            )
        except Exception as diagnostic_error:
            print(
                f"[SKP {job_id}] No se pudo leer versión de dependencias: "
                f"{diagnostic_error}",
                flush=True
            )


        # =============================================
        # EXPORTAR GLB
        # =============================================

        update_job(

            job_id,

            stage="exporting",

            message=
                "Generando el modelo GLB...",

            progress=45
        )


        print(

            f"[SKP {job_id}] "
            f"Ejecutando export()...",

            flush=True

        )


        glb.export(

            skp,

            str(
                glb_path
            ),

            textures=True

        )


        # =============================================
        # VALIDAR GLB
        # =============================================

        if (

            not glb_path.exists()

            or

            glb_path.stat().st_size == 0

        ):

            raise RuntimeError(

                "La conversión terminó "
                "sin generar un GLB válido."

            )


        glb_size = (
            glb_path.stat().st_size
        )


        update_job(

            job_id,

            stage="uploading",

            message=
                "GLB generado. "
                "Subiendo el modelo...",

            progress=70,

            glbSize=
                glb_size

        )


        print(

            f"[SKP {job_id}] "
            f"GLB generado: "
            f"{glb_size} bytes",

            flush=True

        )


        # =============================================
        # RUTA SUPABASE
        # =============================================

        storage_path = (

            f"projects/"
            f"{project_id}/"
            f"{glb_name}"

        )


        # =============================================
        # LEER GLB
        # =============================================

        # Liberar el objeto SKP antes de cargar el GLB completo en memoria.
        # Esto reduce el pico de RAM durante la subida a Supabase.
        try:
            del skp
        except Exception:
            pass

        with glb_path.open(
            "rb"
        ) as glb_file:

            glb_data = (
                glb_file.read()
            )


        # =============================================
        # SUBIR GLB
        # =============================================

        supabase.storage \
            .from_(SUPABASE_BUCKET) \
            .upload(

                storage_path,

                glb_data,

                {

                    "content-type":
                        "model/gltf-binary",

                    "cache-control":
                        "31536000",

                    "upsert":
                        "true"

                }

            )


        # =============================================
        # URL PÚBLICA
        # =============================================

        model_url = (

            supabase.storage

            .from_(
                SUPABASE_BUCKET
            )

            .get_public_url(
                storage_path
            )

        )


        update_job(

            job_id,

            stage="updating",

            message=
                "Modelo subido. "
                "Actualizando el proyecto...",

            progress=85

        )


        # =============================================
        # ACTUALIZAR PROYECTO
        # =============================================

        project_updated = False


        update_payload = {

            "modelo":
                model_url,

            "updated_at":
                datetime
                    .now(
                        timezone.utc
                    )
                    .isoformat()

        }


        project_response = (

            supabase

            .table(
                "projects"
            )

            .update(
                update_payload
            )

            .eq(
                "id",
                project_id
            )

            .execute()

        )


        project_updated = bool(

            project_response.data

        )


        # =============================================
        # SEGUNDO INTENTO POR SLUG
        # =============================================

        if (

            not project_updated

            and

            project_slug

        ):

            project_response = (

                supabase

                .table(
                    "projects"
                )

                .update(
                    update_payload
                )

                .eq(
                    "slug",
                    project_slug
                )

                .execute()

            )


            project_updated = bool(

                project_response.data

            )


        # =============================================
        # CREAR PROYECTO SI NO EXISTE
        # =============================================

        if (

            not project_updated

            and

            project_slug

        ):

            project_response = (

                supabase

                .table(
                    "projects"
                )

                .upsert(

                    {

                        "id":
                            project_id,

                        "slug":
                            project_slug,

                        "modelo":
                            model_url

                    },

                    on_conflict="id"

                )

                .execute()

            )


            project_updated = bool(

                project_response.data

            )


        # =============================================
        # FINALIZAR
        # =============================================

        finished_at = (
            datetime.now(
                timezone.utc
            )
        )


        duration = (

            finished_at
            -
            started_at

        ).total_seconds()


        result = {

            "ok":
                True,

            "projectId":
                project_id,

            "modelName":
                glb_name,

            "modelPath":
                storage_path,

            "modelUrl":
                model_url,

            "storage":
                "supabase",

            "projectUpdated":
                project_updated,

            "durationSeconds":
                round(
                    duration,
                    2
                ),

            "glbSize":
                glb_size

        }


        update_job(

            job_id,

            status="completed",

            stage="completed",

            message=
                "Conversión terminada correctamente.",

            progress=100,

            result=result

        )


        print(

            f"[SKP {job_id}] "
            f"Conversión terminada "
            f"en {duration:.1f}s",

            flush=True

        )


    except Exception as exc:

        print(

            f"[SKP {job_id}] "
            f"ERROR: {exc}",

            flush=True

        )


        update_job(

            job_id,

            status="error",

            stage="error",

            message=
                "No se pudo convertir "
                "o guardar el SKP.",

            progress=0,

            error=str(
                exc
            )

        )


    finally:

        shutil.rmtree(

            temporary_directory,

            ignore_errors=True

        )


# =====================================================
# HEALTH CHECK
# =====================================================

@app.get(
    "/api/health"
)
def health():

    with conversion_jobs_lock:

        active_jobs = sum(

            1

            for job
            in conversion_jobs.values()

            if job.get(
                "status"
            )
            in {
                "queued",
                "running"
            }

        )


    return {

        "ok":
            True,

        "converter":
            "OpenSKP",

        "storage":
            "Supabase",

        "bucket":
            SUPABASE_BUCKET,

        "activeJobs":
            active_jobs,

        "conversionTimeoutSeconds":
            CONVERSION_TIMEOUT_SECONDS

    }


# =====================================================
# INICIAR CONVERSIÓN
# =====================================================

@app.post(
    "/api/convert-skp"
)
async def convert_skp(

    file: UploadFile =
        File(...),

    project_id: str =
        Form(...),

    project_slug: str =
        Form("")

):

    original_name = (

        file.filename

        or

        "model.skp"

    )


    # ================================================
    # VALIDAR EXTENSIÓN
    # ================================================

    if not original_name.lower().endswith(
        ".skp"
    ):

        raise HTTPException(

            status_code=400,

            detail=
                "Solo se permiten "
                "archivos .SKP."

        )


    # ================================================
    # VALIDAR PROYECTO
    # ================================================

    if not project_id.strip():

        raise HTTPException(

            status_code=400,

            detail=
                "Falta el ID del proyecto."

        )


    # ================================================
    # NOMBRES
    # ================================================

    token = (
        uuid.uuid4()
        .hex[:10]
    )


    base_name = safe_name(

        project_slug
        or
        project_id

    )


    skp_name = (

        f"{base_name}-"
        f"{token}.skp"

    )


    glb_name = (

        f"{base_name}-"
        f"{token}.glb"

    )


    # ================================================
    # TEMPORAL
    # ================================================

    temporary_directory = (
        tempfile.mkdtemp(
            prefix="universal-stand-"
        )
    )


    skp_path = (

        Path(
            temporary_directory
        )
        /
        skp_name

    )


    glb_path = (

        Path(
            temporary_directory
        )
        /
        glb_name

    )


    try:

        # ============================================
        # GUARDAR SKP
        # ============================================

        with skp_path.open(
            "wb"
        ) as destination:

            shutil.copyfileobj(

                file.file,

                destination

            )


        file_size = (
            skp_path.stat()
            .st_size
        )


        if file_size == 0:

            raise HTTPException(

                status_code=400,

                detail=
                    "El archivo SKP "
                    "está vacío."

            )


        # ============================================
        # CREAR JOB
        # ============================================

        job_id = create_job(

            project_id,

            original_name

        )


        update_job(

            job_id,

            fileSize=
                file_size,

            message=
                "Archivo recibido. "
                "Preparando conversión..."

        )


        # ============================================
        # EJECUTAR EN SEGUNDO PLANO
        # ============================================

        conversion_executor.submit(

            run_conversion_job,

            job_id,

            temporary_directory,

            skp_path,

            glb_path,

            project_id,

            project_slug,

            glb_name

        )


        # La petición HTTP termina inmediatamente.
        return {

            "ok":
                True,

            "accepted":
                True,

            "jobId":
                job_id,

            "status":
                "queued",

            "message":
                "El archivo fue recibido "
                "y la conversión comenzó.",

            "fileName":
                original_name,

            "fileSize":
                file_size

        }


    except HTTPException:

        shutil.rmtree(

            temporary_directory,

            ignore_errors=True

        )

        raise


    except Exception as exc:

        shutil.rmtree(

            temporary_directory,

            ignore_errors=True

        )

        raise HTTPException(

            status_code=422,

            detail=
                f"No se pudo iniciar "
                f"la conversión: {exc}"

        ) from exc


# =====================================================
# ESTADO DE CONVERSIÓN
# =====================================================

@app.get(
    "/api/convert-skp/status/{job_id}"
)
def conversion_status(
    job_id: str
):

    job = get_job(
        job_id
    )


    if not job:

        raise HTTPException(

            status_code=404,

            detail=
                "No existe esa conversión "
                "o el trabajo ya fue eliminado."

        )


    return job


# =====================================================
# ROOT
# =====================================================

@app.get(
    "/"
)
def root():

    return FileResponse(

        BASE_DIR
        /
        "index.html"

    )


app.mount(

    "/",

    StaticFiles(

        directory=
            str(
                BASE_DIR
            ),

        html=True

    ),

    name="frontend"

)


# =====================================================
# EJECUCIÓN DIRECTA
# =====================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(

        "server:app",

        host="127.0.0.1",

        port=8000,

        reload=False

    )