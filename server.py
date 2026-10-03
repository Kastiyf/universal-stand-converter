import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openskp import SkpFile
from openskp.export import glb
from supabase import create_client, Client


# =====================================================
# CONFIGURACIÓN
# =====================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
SUPABASE_BUCKET = "models"


if not SUPABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno SUPABASE_URL."
    )


if not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "Falta la variable de entorno SUPABASE_SECRET_KEY."
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
# UTILIDADES
# =====================================================

def safe_name(value: str) -> str:
    value = Path(
        value or "model.skp"
    ).stem

    value = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "-",
        value
    ).strip("-")

    return value or "model"


# =====================================================
# HEALTH CHECK
# =====================================================

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "converter": "OpenSKP",
        "storage": "Supabase"
    }


# =====================================================
# CONVERSIÓN SKP → GLB
# =====================================================

@app.post("/api/convert-skp")
def convert_skp(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    project_slug: str = Form("")
):
    original_name = file.filename or "model.skp"

    # -------------------------------------------------
    # VALIDAR EXTENSIÓN
    # -------------------------------------------------

    if not original_name.lower().endswith(".skp"):
        raise HTTPException(
            status_code=400,
            detail="Solo se permiten archivos .SKP."
        )

    # -------------------------------------------------
    # VALIDAR PROYECTO
    # -------------------------------------------------

    if not project_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Falta el ID del proyecto."
        )

    # -------------------------------------------------
    # GENERAR NOMBRES
    # -------------------------------------------------

    token = uuid.uuid4().hex[:10]

    base_name = safe_name(
        project_slug or project_id
    )

    skp_name = f"{base_name}-{token}.skp"
    glb_name = f"{base_name}-{token}.glb"

    # -------------------------------------------------
    # CARPETA TEMPORAL
    # -------------------------------------------------

    temporary_directory = tempfile.mkdtemp(
        prefix="universal-stand-"
    )

    skp_path = (
        Path(temporary_directory) /
        skp_name
    )

    glb_path = (
        Path(temporary_directory) /
        glb_name
    )

    try:

        # =============================================
        # GUARDAR SKP TEMPORALMENTE
        # =============================================

        with skp_path.open("wb") as destination:
            shutil.copyfileobj(
                file.file,
                destination
            )

        # =============================================
        # ABRIR SKP
        # =============================================

        skp = SkpFile.open(
            str(skp_path)
        )

        # =============================================
        # PARSEAR SKP
        # =============================================

        skp.parse()

        # =============================================
        # EXPORTAR GLB
        # =============================================
        #
        # Conserva las texturas embebidas sin reducir
        # geometría ni calidad del modelo.
        #

        glb.export(
            skp,
            str(glb_path),
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
            raise HTTPException(
                status_code=500,
                detail=(
                    "La conversión terminó sin generar "
                    "un GLB válido."
                )
            )

        # =============================================
        # RUTA EN SUPABASE STORAGE
        # =============================================

        storage_path = (
            f"projects/{project_id}/{glb_name}"
        )

        # =============================================
        # SUBIR GLB DIRECTAMENTE DESDE DISCO
        # =============================================
        #
        # Evita cargar el GLB completo en RAM con
        # read() antes de enviarlo a Supabase.
        #

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
                        "upsert": "true"
                    }
                )
            )

        # =============================================
        # OBTENER URL PÚBLICA
        # =============================================

        model_url = (
            supabase.storage
            .from_(SUPABASE_BUCKET)
            .get_public_url(
                storage_path
            )
        )

        # =============================================
        # RESPUESTA
        # =============================================

        return {
            "ok": True,
            "projectId": project_id,
            "modelName": glb_name,
            "modelPath": storage_path,
            "modelUrl": model_url,
            "storage": "supabase"
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "No se pudo convertir o guardar el SKP: "
                f"{exc}"
            )
        ) from exc

    finally:

        # =============================================
        # ELIMINAR ARCHIVOS TEMPORALES
        # =============================================

        shutil.rmtree(
            temporary_directory,
            ignore_errors=True
        )


# =====================================================
# ROOT
# =====================================================

@app.get("/")
def root():
    return {
        "ok": True,
        "service": "Universal Stand SKP Converter",
        "converter": "OpenSKP",
        "storage": "Supabase"
    }
