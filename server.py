from pathlib import Path
import re
import shutil
import uuid

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile
)

from fastapi.middleware.cors import (
    CORSMiddleware
)

from fastapi.staticfiles import (
    StaticFiles
)

from openskp import SkpFile
from openskp.export import glb


BASE_DIR = Path(__file__).resolve().parent

STORAGE_DIR = BASE_DIR / "storage"

SKP_DIR = STORAGE_DIR / "skp"

GLB_DIR = STORAGE_DIR / "glb"


SKP_DIR.mkdir(
    parents=True,
    exist_ok=True
)

GLB_DIR.mkdir(
    parents=True,
    exist_ok=True
)


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


@app.get(
    "/api/health"
)
def health():

    return {
        "ok": True,
        "converter": "OpenSKP"
    }


@app.post(
    "/api/convert-skp"
)
async def convert_skp(
    request: Request,
    file: UploadFile = File(...),
    project_id: str = Form(...),
    project_slug: str = Form("")
):

    original_name = (
        file.filename
        or
        "model.skp"
    )

    if not original_name.lower().endswith(
        ".skp"
    ):

        raise HTTPException(
            status_code=400,
            detail="Solo se permiten archivos .SKP."
        )

    if not project_id.strip():

        raise HTTPException(
            status_code=400,
            detail="Falta el ID del proyecto."
        )

    token = uuid.uuid4().hex[:10]

    base_name = safe_name(
        project_slug
        or
        project_id
    )

    skp_name = (
        f"{base_name}-{token}.skp"
    )

    glb_name = (
        f"{base_name}-{token}.glb"
    )

    skp_path = (
        SKP_DIR / skp_name
    )

    glb_path = (
        GLB_DIR / glb_name
    )

    try:

        with skp_path.open(
            "wb"
        ) as destination:

            shutil.copyfileobj(
                file.file,
                destination
            )

        skp = SkpFile.open(
            str(skp_path)
        )

        skp.parse()

        glb.export(
            skp,
            str(glb_path)
        )

    except Exception as exc:

        if glb_path.exists():

            glb_path.unlink()

        raise HTTPException(
            status_code=422,
            detail=(
                "No se pudo convertir el SKP: "
                f"{exc}"
            )
        ) from exc

    if (
        not glb_path.exists()
        or
        glb_path.stat().st_size == 0
    ):

        raise HTTPException(
            status_code=500,
            detail=(
                "La conversión terminó "
                "sin generar un GLB válido."
            )
        )

    base_url = str(
        request.base_url
    ).rstrip("/")

    return {
        "ok": True,
        "projectId": project_id,
        "modelName": glb_name,
        "modelUrl": (
            f"{base_url}/storage/glb/"
            f"{glb_name}"
        ),
        "skpUrl": (
            f"{base_url}/storage/skp/"
            f"{skp_name}"
        )
    }


app.mount(
    "/storage",
    StaticFiles(
        directory=str(
            STORAGE_DIR
        )
    ),
    name="storage"
)


app.mount(
    "/",
    StaticFiles(
        directory=str(
            BASE_DIR
        ),
        html=True
    ),
    name="frontend"
)