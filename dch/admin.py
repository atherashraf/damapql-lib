from sqladmin import ModelView

from .admin_setup import get_dch_admin
from .models import *


dch_admin = get_dch_admin()
class LayerCategoryAdmin(ModelView, model=LayerCategory):
    icon = "fa-solid fa-layer-group"
    column_list = LayerCategory.get_fields()

dch_admin.add_view(LayerCategoryAdmin)


class LayerQualityLevelAdmin(ModelView, model=LayerQualityLevel):
    icon = "fa-solid fa-star"
    column_list = LayerQualityLevel.get_fields()

dch_admin.add_view(LayerQualityLevelAdmin)


class DBConnectionAdmin(ModelView, model=DBConnection, engine=LayerAccessibility.get_engine()):
    icon = "fa-solid fa-database"
    column_list = DBConnection.get_fields()

dch_admin.add_view(DBConnectionAdmin)


class LayerAccessibilityAdmin(ModelView, model=LayerAccessibility, engine=LayerAccessibility.get_engine()):
    icon = "fa-solid fa-universal-access"
    column_list = LayerAccessibility.get_fields()

dch_admin.add_view(LayerAccessibilityAdmin)


class LayerInfoAdmin(ModelView, model=LayerInfo, engine=LayerInfo.get_engine()):
    icon = "fa-solid fa-info-circle"
    column_list = LayerInfo.get_fields(skip_fields=[
        'layer_accessibility', 'extent', 'geom_type',
        'extent_3857', 'layer_access_id', 'data_model', 'model_type'
    ])
    column_searchable_list = ['title']

dch_admin.add_view(LayerInfoAdmin)


class MapInfoAdmin(ModelView, model=MapInfo, engine=MapInfo.get_engine()):
    icon = "fa-solid fa-map"
    column_list = MapInfo.get_fields()

dch_admin.add_view(MapInfoAdmin)
