from fastapi import APIRouter

from .layers import dch_layers_router
from .maps import dch_maps_router
from .styles import dch_styles_router
from .raster import dch_raster_router
from .tiles import dch_tiles_router
from .admin import dch_admin_router
from .navigation import dch_navigation_router
from .export import dch_export_router

dch_router = APIRouter(tags=["dch"])

dch_router.include_router(dch_layers_router)
dch_router.include_router(dch_maps_router)
dch_router.include_router(dch_styles_router)
dch_router.include_router(dch_raster_router)
dch_router.include_router(dch_admin_router)
dch_router.include_router(dch_navigation_router)
dch_router.include_router(dch_tiles_router)
dch_router.include_router(dch_export_router)