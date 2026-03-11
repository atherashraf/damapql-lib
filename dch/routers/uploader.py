import os
import uuid
import shutil
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.damapql.settings import DCH_MEDIA_DIR
from app.damapql.utils.adapters.db import DBBase
from digitalarzengine.utils.singletons import da_logger

# Import your models and services

upload_router = APIRouter(prefix="/upload", tags=["Uploader"])

# Directory for storing chunks before assembly
CHUNK_STORAGE = os.path.join(DCH_MEDIA_DIR, "temp_chunks")
os.makedirs(CHUNK_STORAGE, exist_ok=True)


@upload_router.post("/initiate/")
async def initiate_upload(
        filename: str = Form(...),
        file_type: str = Form(...),  # 'vector' or 'raster'
):
    """Starts a session and returns a unique upload_id."""
    upload_id = str(uuid.uuid4())
    upload_dir = os.path.join(CHUNK_STORAGE, upload_id)
    os.makedirs(upload_dir, exist_ok=True)

    return {
        "upload_id": upload_id,
        "filename": filename,
        "status": "initialized"
    }


@upload_router.post("/chunk/")
async def upload_chunk(
        upload_id: str = Form(...),
        chunk_index: int = Form(...),
        file: UploadFile = File(...)
):
    """Receives and stores an individual binary chunk."""
    upload_dir = os.path.join(CHUNK_STORAGE, upload_id)
    if not os.path.exists(upload_dir):
        raise HTTPException(status_code=404, detail="Upload session expired or invalid")

    chunk_path = os.path.join(upload_dir, f"part_{chunk_index}")
    with open(chunk_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"msg": f"Chunk {chunk_index} received"}


@upload_router.post("/finalize/")
async def finalize_upload(
        upload_id: str = Form(...),
        total_chunks: int = Form(...),
        filename: str = Form(...),
        category_id: int = Form(...),
        title: Optional[str] = Form(None)
):
    """Assembles chunks and triggers the GIS processing pipeline."""
    upload_dir = os.path.join(CHUNK_STORAGE, upload_id)
    final_path = os.path.join(upload_dir, filename)

    # 1. Assemble Chunks
    try:
        with open(final_path, "wb") as final_file:
            for i in range(total_chunks):
                chunk_path = os.path.join(upload_dir, f"part_{i}")
                with open(chunk_path, "rb") as chunk_file:
                    final_file.write(chunk_file.read())
                os.remove(chunk_path)
    except Exception as e:
        da_logger.exception(f"File assembly failed: {e}")
        raise HTTPException(status_code=500, detail="Error reassembling file parts")

    # 2. Identify Data Type and Process
    ext = os.path.splitext(filename)[1].lower()

    try:
        if ext in ['.shp', '.geojson', '.gpkg', '.zip']:
            return await process_vector_upload(final_path, upload_id, filename, category_id, title or filename)
        elif ext in ['.tif', '.tiff', '.geotiff']:
            return await process_raster_upload(final_path, upload_id, filename, category_id, title or filename)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format")

    except Exception as e:
        da_logger.exception(f"GIS Processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Helper Processors ---

async def process_vector_upload(file_path, upload_id, filename, category_id, title):
    # Logic: Read with GeoPandas -> to_postgis -> Register LayerInfo
    # Note: If .shp, ensure all sidecar files were uploaded in the chunks!
    from app.damapql.settings import DATA_DB_KEY

    # 1. Load Data
    import geopandas as gpd
    gdf = gpd.read_file(file_path)

    # 2. Generate Table Name
    table_name = f"usr_{upload_id.replace('-', '_')[:12]}"
    engine = DBBase.get_engine(db_key=DATA_DB_KEY)

    # 3. Save to PostGIS
    gdf.to_postgis(table_name, con=engine, if_exists="replace", index=False)

    # 4. Register in LayerInfo / Accessibility
    # (Reuse your create_db_accessibility logic here)
    # ...

    return {"status": "success", "table": table_name, "type": "vector"}


async def process_raster_upload(file_path, upload_id, filename, category_id, title):
    # Logic: Move to /rasters/ -> Register as AccessibilityType.File
    dest_dir = os.path.join(DCH_MEDIA_DIR, "rasters", upload_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    shutil.move(file_path, dest_path)

    # Register metadata in LayerInfo
    # ...

    return {"status": "success", "path": dest_path, "type": "raster"}