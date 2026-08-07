import json
from dataclasses import dataclass
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.sqltypes import JSON

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.routers.admin_dependencies import (
    require_dch_admin,
)
from app.damapql.utils.adapters.model_utils import ModelUtils
from digitalarzengine.utils.singletons import da_logger


dch_admin_model_router = APIRouter(
    prefix="/admin/models",
    tags=["dch admin models"],
)


# ---------------------------------------------------------------------
# Model access policy
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class AdminModelPolicy:
    model_name: str
    create_fields: frozenset[str]
    update_fields: frozenset[str]
    allow_delete: bool = False


ADMIN_MODEL_POLICIES: dict[str, AdminModelPolicy] = {
    "layercategory": AdminModelPolicy(
        model_name="LayerCategory",
        create_fields=frozenset({
            "main",
            "sub",
        }),
        update_fields=frozenset({
            "main",
            "sub",
        }),
        allow_delete=True,
    ),

    # Add additional models explicitly:
    #
    # "layerqualitylevel": AdminModelPolicy(
    #     model_name="LayerQualityLevel",
    #     create_fields=frozenset({
    #         "level",
    #         "name",
    #         "description",
    #     }),
    #     update_fields=frozenset({
    #         "level",
    #         "name",
    #         "description",
    #     }),
    #     allow_delete=True,
    # ),
}


# ---------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------
class ModelRowPK(BaseModel):
    colName: str = Field(min_length=1)
    colValue: Any


class UpdateModelRowPayload(BaseModel):
    model_name: str = Field(min_length=1)
    pk: ModelRowPK
    rowData: dict[str, Any]


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------
def get_model_policy(model_name: str) -> AdminModelPolicy:
    normalized_name = model_name.strip().lower()
    policy = ADMIN_MODEL_POLICIES.get(normalized_name)

    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Generic model administration is not allowed "
                f"for model '{model_name}'."
            ),
        )

    return policy


def get_allowed_model(
    model_name: str,
) -> tuple[type, AdminModelPolicy]:
    policy = get_model_policy(model_name)
    model_class = ModelUtils.get_model(policy.model_name)

    if model_class is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{policy.model_name}' was not found.",
        )

    return model_class, policy


def get_single_primary_key_column(model_class: type):
    mapper = inspect(model_class)
    primary_key_columns = list(mapper.primary_key)

    if len(primary_key_columns) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Generic administration currently supports only "
                f"models with one primary-key column. "
                f"Model '{model_class.__name__}' has "
                f"{len(primary_key_columns)} primary-key columns."
            ),
        )

    return primary_key_columns[0]


def coerce_column_value(column, value: Any) -> Any:
    if value is None:
        return None

    # Convert choice labels to their stored database values.
    if hasattr(column.type, "choices"):
        choice_map = {
            str(label): db_value
            for db_value, label in column.type.choices
        }
        value = choice_map.get(str(value), value)

    # Normalize values such as {"code": "V"}.
    if isinstance(value, dict) and "code" in value:
        value = value["code"]

    # Decode JSON strings for JSON columns.
    if isinstance(column.type, JSON) and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON value for '{column.key}'.",
            ) from error

    # Keep dictionaries only for JSON fields.
    if isinstance(value, dict) and not isinstance(column.type, JSON):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Field '{column.key}' does not accept "
                f"an object value."
            ),
        )

    try:
        python_type = column.type.python_type

        if python_type is bool and isinstance(value, str):
            return value.strip().lower() in {
                "true",
                "1",
                "yes",
                "y",
            }

        if not isinstance(value, python_type):
            return python_type(value)

    except (AttributeError, TypeError, ValueError):
        pass

    return value


def prepare_row_data(
    model_class: type,
    row_data: dict[str, Any],
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(row_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rowData must be an object.",
        )

    submitted_fields = set(row_data.keys())
    invalid_fields = submitted_fields - allowed_fields

    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The following fields cannot be modified: "
                f"{', '.join(sorted(invalid_fields))}"
            ),
        )

    mapper = inspect(model_class)
    prepared: dict[str, Any] = {}

    for field_name, value in row_data.items():
        if field_name not in mapper.columns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid model field: '{field_name}'.",
            )

        column = mapper.columns[field_name]
        prepared[field_name] = coerce_column_value(
            column,
            value,
        )

    return prepared


def get_error_detail(error: IntegrityError) -> str:
    error_text = str(error.orig).lower()

    if (
        "unique constraint" in error_text
        or "duplicate key" in error_text
    ):
        return "A record with the same values already exists."

    if "foreign key constraint" in error_text:
        return (
            "This record cannot be changed or deleted because "
            "it is referenced by another record."
        )

    return "The database rejected this operation."


# ---------------------------------------------------------------------
# Add model row
# ---------------------------------------------------------------------
@dch_admin_model_router.post(
    "/add-row/",
    summary="Add Model Row",
)
async def add_model_row(
    request: Request,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    """
    Add a row to an explicitly allowed model.

    Expects multipart form data:

        model_name = LayerCategory
        main = Agriculture
        sub = Crops
    """
    require_dch_admin(current_user)

    form = await request.form()
    requested_model_name = form.get("model_name")

    if not requested_model_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model_name is required.",
        )

    model_class, policy = get_allowed_model(
        str(requested_model_name)
    )

    row_data = {
        key: form.get(key)
        for key in form.keys()
        if key != "model_name"
    }

    if not row_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No model fields were provided.",
        )

    prepared_data = prepare_row_data(
        model_class=model_class,
        row_data=row_data,
        allowed_fields=policy.create_fields,
    )

    try:
        obj = ModelUtils.add_model_obj(
            model_name=policy.model_name,
            row=prepared_data,
        )

        if obj is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to add model row.",
            )

        # Kept lowercase to match your existing frontend check.
        return {
            "payload": {
                "status": "success",
                "msg": "row added",
            }
        }

    except HTTPException:
        raise

    except IntegrityError as error:
        da_logger.warning(
            "Could not add %s: %s",
            policy.model_name,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=get_error_detail(error),
        ) from error

    except Exception as error:
        da_logger.exception(
            "Failed to add model row for %s: %s",
            policy.model_name,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add model row.",
        ) from error


# ---------------------------------------------------------------------
# Update model row
# ---------------------------------------------------------------------
@dch_admin_model_router.post(
    "/update-row/",
    summary="Update Model Row",
)
def update_model_row(
    payload: UpdateModelRowPayload,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    require_dch_admin(current_user)

    model_class, policy = get_allowed_model(
        payload.model_name
    )

    primary_key_column = get_single_primary_key_column(
        model_class
    )

    if payload.pk.colName != primary_key_column.key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid primary-key column. Expected "
                f"'{primary_key_column.key}'."
            ),
        )

    prepared_data = prepare_row_data(
        model_class=model_class,
        row_data=payload.rowData,
        allowed_fields=policy.update_fields,
    )

    primary_key_value = coerce_column_value(
        primary_key_column,
        payload.pk.colValue,
    )

    try:
        with model_class.managed_session() as session:
            obj = session.get(
                model_class,
                primary_key_value,
            )

            if obj is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Model row was not found.",
                )

            for field_name, value in prepared_data.items():
                setattr(obj, field_name, value)

            session.commit()

        return {
            "payload": {
                "status": "success",
                "msg": "row updated",
                "model_name": policy.model_name,
                "pk": {
                    "colName": primary_key_column.key,
                    "colValue": primary_key_value,
                },
                "updated": prepared_data,
            }
        }

    except HTTPException:
        raise

    except IntegrityError as error:
        da_logger.warning(
            "Could not update %s: %s",
            policy.model_name,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=get_error_detail(error),
        ) from error

    except Exception as error:
        da_logger.exception(
            "Failed to update %s: %s",
            policy.model_name,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update model row.",
        ) from error


# ---------------------------------------------------------------------
# Delete model row
# ---------------------------------------------------------------------
@dch_admin_model_router.delete(
    "/{model_name}/{pk_value}/",
    summary="Delete Model Row",
)
def delete_model_row(
    model_name: str,
    pk_value: str,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    """
    Delete one model row through its actual primary key.

    Example:

        DELETE /admin/models/LayerCategory/4/
    """
    require_dch_admin(current_user)

    model_class, policy = get_allowed_model(model_name)

    if not policy.allow_delete:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Deletion is not allowed for "
                f"model '{policy.model_name}'."
            ),
        )

    primary_key_column = get_single_primary_key_column(
        model_class
    )

    converted_pk_value = coerce_column_value(
        primary_key_column,
        pk_value,
    )

    try:
        result = ModelUtils.delete_model_obj(
            model_name=policy.model_name,
            pk={
                primary_key_column.key: converted_pk_value,
            },
        )

        return {"payload": result}

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except IntegrityError as error:
        da_logger.warning(
            "Could not delete %s with PK %s: %s",
            policy.model_name,
            converted_pk_value,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=get_error_detail(error),
        ) from error

    except Exception as error:
        da_logger.exception(
            "Failed to delete %s with PK %s: %s",
            policy.model_name,
            converted_pk_value,
            error,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete model row.",
        ) from error