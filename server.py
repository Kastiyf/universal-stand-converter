import array
import gc
import json
import os
import re
import shutil
import struct
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openskp import SkpFile
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
SUPABASE_BUCKET = "models"

if not SUPABASE_URL:
    raise RuntimeError("Falta la variable de entorno SUPABASE_URL.")
if not SUPABASE_SECRET_KEY:
    raise RuntimeError("Falta la variable de entorno SUPABASE_SECRET_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

app = FastAPI(title="Universal Stand SKP Converter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def safe_name(value: str) -> str:
    value = Path(value or "model.skp").stem
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return value or "model"


def current_rss_mb() -> float:
    """
    Best-effort current RSS for Linux/Render.
    This is only diagnostic and never affects conversion.
    """
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as f:
            resident_pages = int(f.read().split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (resident_pages * page_size) / (1024 * 1024)
    except Exception:
        return 0.0


def _pad4(file_obj, offset: int) -> int:
    padding = (-offset) % 4
    if padding:
        file_obj.write(b"\x00" * padding)
        offset += padding
    return offset


def _write_buffer_view(
    file_obj,
    buffer_views,
    raw_data: bytes,
    offset: int,
    target: int | None = None,
) -> tuple[int, int]:
    """
    Append one binary bufferView to the temporary GLB BIN file.

    Returns:
        (buffer_view_index, new_offset)
    """
    offset = _pad4(file_obj, offset)
    start = offset
    file_obj.write(raw_data)
    offset += len(raw_data)

    view = {
        "buffer": 0,
        "byteOffset": start,
        "byteLength": len(raw_data),
    }
    if target is not None:
        view["target"] = target

    index = len(buffer_views)
    buffer_views.append(view)
    return index, offset


def _array_bytes(values: array.array) -> bytes:
    """
    Convert an array.array to little-endian bytes without creating Python
    lists of every vertex. OpenSKP runs on little-endian Render containers,
    but keep the conversion explicit for portability.
    """
    if values.itemsize <= 0:
        return values.tobytes()

    if os.sys.byteorder == "little":
        return values.tobytes()

    copied = array.array(values.typecode, values)
    copied.byteswap()
    return copied.tobytes()


def _scaled_positions_mm(values: array.array) -> tuple[array.array, list[float], list[float]]:
    """
    Copy only one primitive's position buffer and convert OpenSKP metres to
    the millimetres used by the existing GLB pipeline.

    This is intentionally per-primitive. The previous trimesh path expanded
    the complete primitive into Python tuples/lists, creating a much larger
    transient memory peak.
    """
    scaled = array.array("f", values)
    minimum = [float("inf"), float("inf"), float("inf")]
    maximum = [float("-inf"), float("-inf"), float("-inf")]

    for i, value in enumerate(scaled):
        value *= 1000.0
        scaled[i] = value

        axis = i % 3
        if value < minimum[axis]:
            minimum[axis] = value
        if value > maximum[axis]:
            maximum[axis] = value

    return scaled, minimum, maximum


def export_glb_optimized(scene_obj, output_path: str) -> str:
    """
    Write a GLB 2.0 directly from OpenSKP's Scene.

    Why this replaces trimesh:
      1. No Python list/tuple expansion for every vertex.
      2. No PIL decoding of textures into RGBA images.
      3. Texture bytes already extracted by OpenSKP are copied directly
         into the GLB as PNG/JPEG bufferViews.
      4. Geometry is written primitive-by-primitive to a temporary BIN file,
         so the complete GLB binary payload is never duplicated in RAM.
      5. Only one primitive's position array is temporarily copied for the
         metres -> millimetres conversion.

    The resulting file is standard glTF 2.0 / GLB and preserves:
      - geometry
      - normals
      - UVs
      - materials
      - texture images
      - material transparency
      - double-sided materials
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    buffer_views = []
    accessors = []
    meshes = []
    nodes = []
    materials = []
    images = []
    textures = []
    bin_size = 0

    # Build the GLB BIN into a temporary file. This avoids holding geometry
    # + images + the final GLB simultaneously in memory.
    bin_fd, bin_path = tempfile.mkstemp(
        prefix="universal-stand-glb-",
        suffix=".bin",
        dir=str(output.parent),
    )
    os.close(bin_fd)

    try:
        with open(bin_path, "wb") as binary:
            # Geometry.
            for primitive_index, prim in enumerate(scene_obj.glb_primitives):
                positions_mm, pos_min, pos_max = _scaled_positions_mm(prim.positions)
                position_view, bin_size = _write_buffer_view(
                    binary,
                    buffer_views,
                    _array_bytes(positions_mm),
                    bin_size,
                    target=34962,
                )

                normal_view, bin_size = _write_buffer_view(
                    binary,
                    buffer_views,
                    _array_bytes(prim.normals),
                    bin_size,
                    target=34962,
                )

                uv_view, bin_size = _write_buffer_view(
                    binary,
                    buffer_views,
                    _array_bytes(prim.uvs),
                    bin_size,
                    target=34962,
                )

                index_view, bin_size = _write_buffer_view(
                    binary,
                    buffer_views,
                    _array_bytes(prim.indices),
                    bin_size,
                    target=34963,
                )

                vertex_count = len(prim.positions) // 3
                index_count = len(prim.indices)

                position_accessor = len(accessors)
                accessors.append(
                    {
                        "bufferView": position_view,
                        "componentType": 5126,
                        "count": vertex_count,
                        "type": "VEC3",
                        "min": pos_min,
                        "max": pos_max,
                    }
                )

                normal_accessor = len(accessors)
                accessors.append(
                    {
                        "bufferView": normal_view,
                        "componentType": 5126,
                        "count": vertex_count,
                        "type": "VEC3",
                    }
                )

                uv_accessor = len(accessors)
                accessors.append(
                    {
                        "bufferView": uv_view,
                        "componentType": 5126,
                        "count": vertex_count,
                        "type": "VEC2",
                    }
                )

                index_accessor = len(accessors)
                accessors.append(
                    {
                        "bufferView": index_view,
                        "componentType": 5125,
                        "count": index_count,
                        "type": "SCALAR",
                    }
                )

                mesh_index = len(meshes)
                meshes.append(
                    {
                        "name": prim.geom_name,
                        "primitives": [
                            {
                                "attributes": {
                                    "POSITION": position_accessor,
                                    "NORMAL": normal_accessor,
                                    "TEXCOORD_0": uv_accessor,
                                },
                                "indices": index_accessor,
                                "material": prim.material_index,
                            }
                        ],
                    }
                )

                nodes.append(
                    {
                        "name": prim.geom_name,
                        "mesh": mesh_index,
                    }
                )

            # Raw texture bytes. OpenSKP already deduplicates identical
            # textures, so each image is written only once.
            for texture_index, texture in enumerate(scene_obj.textures):
                image_view, bin_size = _write_buffer_view(
                    binary,
                    buffer_views,
                    texture.data,
                    bin_size,
                    target=None,
                )

                images.append(
                    {
                        "bufferView": image_view,
                        "mimeType": texture.mime_type,
                        "name": texture.filename or f"texture_{texture_index}",
                    }
                )

                textures.append(
                    {
                        "source": texture_index,
                    }
                )

            # Flush before reading the BIN again for the final GLB.
            binary.flush()

        # Materials are tiny compared with geometry/images, so building this
        # JSON structure in memory is harmless and keeps the writer simple.
        for material in scene_obj.gltf_materials:
            pbr_source = material.get("pbrMetallicRoughness", {})
            pbr = {
                "baseColorFactor": list(
                    pbr_source.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
                ),
                "metallicFactor": float(pbr_source.get("metallicFactor", 0.0)),
                "roughnessFactor": float(pbr_source.get("roughnessFactor", 0.8)),
            }

            texture_ref = pbr_source.get("baseColorTexture")
            if texture_ref is not None:
                pbr["baseColorTexture"] = {
                    "index": int(texture_ref["index"])
                }

            output_material = {
                "pbrMetallicRoughness": pbr,
            }

            if material.get("doubleSided"):
                output_material["doubleSided"] = True

            if material.get("alphaMode"):
                output_material["alphaMode"] = material["alphaMode"]

            materials.append(output_material)

        gltf = {
            "asset": {
                "version": "2.0",
                "generator": "Universal Stand SKP Converter",
            },
            "scene": 0,
            "scenes": [
                {
                    "nodes": list(range(len(nodes))),
                }
            ],
            "buffers": [
                {
                    "byteLength": bin_size,
                }
            ],
            "bufferViews": buffer_views,
            "accessors": accessors,
            "materials": materials,
            "meshes": meshes,
            "nodes": nodes,
        }

        if images:
            gltf["images"] = images
            gltf["textures"] = textures

        json_bytes = json.dumps(
            gltf,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        # GLB JSON chunk must be padded with spaces to a 4-byte boundary.
        json_padding = (-len(json_bytes)) % 4
        if json_padding:
            json_bytes += b" " * json_padding

        bin_padding = (-bin_size) % 4
        total_length = (
            12
            + 8
            + len(json_bytes)
            + 8
            + bin_size
            + bin_padding
        )

        with open(output, "wb") as final_glb:
            final_glb.write(
                struct.pack(
                    "<III",
                    0x46546C67,  # "glTF"
                    2,
                    total_length,
                )
            )

            final_glb.write(
                struct.pack(
                    "<II",
                    len(json_bytes),
                    0x4E4F534A,  # "JSON"
                )
            )
            final_glb.write(json_bytes)

            final_glb.write(
                struct.pack(
                    "<II",
                    bin_size + bin_padding,
                    0x004E4942,  # "BIN\0"
                )
            )

            with open(bin_path, "rb") as binary:
                shutil.copyfileobj(binary, final_glb, length=1024 * 1024)

            if bin_padding:
                final_glb.write(b"\x00" * bin_padding)

        return str(output.resolve())

    finally:
        try:
            os.remove(bin_path)
        except FileNotFoundError:
            pass


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "converter": "OpenSKP",
        "storage": "Supabase",
        "glbExporter": "native-streaming"
    }


@app.post("/api/convert-skp")
def convert_skp(
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

    temporary_directory = tempfile.mkdtemp(prefix="universal-stand-")
    skp_path = Path(temporary_directory) / skp_name
    glb_path = Path(temporary_directory) / glb_name

    try:
        with skp_path.open("wb") as destination:
            shutil.copyfileobj(file.file, destination, length=1024 * 1024)

        print(
            f"[converter] upload complete rss={current_rss_mb():.1f}MB",
            flush=True,
        )

        # IMPORTANT MEMORY OPTIMIZATION:
        # Build the renderable scene directly. We intentionally do NOT call
        # SkpFile.parse() first, because build_scene() is the public OpenSKP
        # API designed to create the GLB-ready scene in one pass.
        #
        # This avoids keeping a separate parsed SkpModel alive while the
        # complete world-space scene is being baked.
        skp = SkpFile.open(str(skp_path))

        print(
            f"[converter] file opened rss={current_rss_mb():.1f}MB",
            flush=True,
        )

        scene_obj = skp.build_scene()

        # The scene now contains everything required for GLB:
        # baked geometry, normals, UVs, materials and deduplicated textures.
        del skp
        gc.collect()

        print(
            f"[converter] scene complete "
            f"rss={current_rss_mb():.1f}MB "
            f"primitives={len(scene_obj.glb_primitives)} "
            f"textures={len(scene_obj.textures)}",
            flush=True,
        )

        export_glb_optimized(scene_obj, str(glb_path))

        print(
            f"[converter] glb complete "
            f"rss={current_rss_mb():.1f}MB "
            f"size={glb_path.stat().st_size / (1024 * 1024):.2f}MB",
            flush=True,
        )

        if not glb_path.exists() or glb_path.stat().st_size == 0:
            raise HTTPException(
                status_code=500,
                detail="La conversión terminó sin generar un GLB válido.",
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

        return {
            "ok": True,
            "projectId": project_id,
            "modelName": glb_name,
            "modelPath": storage_path,
            "modelUrl": model_url,
            "storage": "supabase",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="No se pudo convertir o guardar el SKP: " f"{exc}",
        ) from exc
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)