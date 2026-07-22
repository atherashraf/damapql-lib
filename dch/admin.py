from sqladmin import Admin, ModelView

from .models import (
    DBConnection,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
    LayerQualityLevel,
    MapInfo,
)


class LayerCategoryAdmin(ModelView, model=LayerCategory):
    name = "Layer Category"
    name_plural = "Layer Categories"
    icon = "fa-solid fa-layer-group"
    category = "DCH"

    column_list = LayerCategory.get_fields()


class LayerQualityLevelAdmin(
    ModelView,
    model=LayerQualityLevel,
):
    name = "Layer Quality Level"
    name_plural = "Layer Quality Levels"
    icon = "fa-solid fa-star"
    category = "DCH"

    column_list = LayerQualityLevel.get_fields()


class DBConnectionAdmin(ModelView, model=DBConnection):
    name = "Database Connection"
    name_plural = "Database Connections"
    icon = "fa-solid fa-database"
    category = "DCH"

    column_list = DBConnection.get_fields()


class LayerAccessibilityAdmin(
    ModelView,
    model=LayerAccessibility,
):
    name = "Layer Accessibility"
    name_plural = "Layer Accessibility"
    icon = "fa-solid fa-universal-access"
    category = "DCH"

    column_list = LayerAccessibility.get_fields()


class LayerInfoAdmin(ModelView, model=LayerInfo):
    name = "Layer Information"
    name_plural = "Layer Information"
    icon = "fa-solid fa-circle-info"
    category = "DCH"

    column_list = LayerInfo.get_fields(
        skip_fields=[
            "layer_accessibility",
            "extent",
            "geom_type",
            "extent_3857",
            "layer_access_id",
            "data_model",
            "model_type",
        ]
    )

    column_searchable_list = [
        LayerInfo.title,
    ]


class MapInfoAdmin(ModelView, model=MapInfo):
    name = "Map Information"
    name_plural = "Map Information"
    icon = "fa-solid fa-map"
    category = "DCH"

    column_list = MapInfo.get_fields()


DCH_ADMIN_VIEWS = (
    LayerCategoryAdmin,
    LayerQualityLevelAdmin,
    DBConnectionAdmin,
    LayerAccessibilityAdmin,
    LayerInfoAdmin,
    MapInfoAdmin,
)


def register_dch_admin_views(admin: Admin) -> None:
    """Register all DCH model views with SQLAdmin."""

    for admin_view in DCH_ADMIN_VIEWS:
        admin.add_view(admin_view)