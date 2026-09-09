"""
Remove-Item "D:\PostgreSQL\17\data\postmaster.pid" -Force

DAMapQL Management Utilities

This module provides CLI-style management commands for DAMapQL-based FastAPI
projects. It supports application scaffolding, schema creation, additive schema
sync, fixture import/export, access-control setup, and security key generation.

Run commands using:

    python -m app.damapql.manage <command> [--key value]

Examples
--------
Show all available commands:

    python -m app.damapql.manage help

Create a dch databases
    python -m app.damapql.manage create_dch_databases
Create a new app scaffold:

    python -m app.damapql.manage startapp --app_label inventory

Create database tables:

    python -m app.damapql.manage migrate --app_label auth
    python -m app.damapql.manage migrate --app_label dch
     python -m app.damapql.manage sync_schema --app_label dch
     python -m app.damapql.manage sync_sequences --app_label dch
     python -m app.damapql.manage sync_sequences --app_label dch --table_names dch_map_info,dch_layer_info,dch_layer_accessibility

    python -m app.damapql.manage sync_schema --model_name WASAComplaint
    python -m app.damapql.manage sync_schema --table_name tbl_wasa_complaints

Add DB Connections:
    python -m app.damapql.manage add_db_connection
    python -m app.damapql.manage add_db_connection --db_key wasa_data
    python -m app.damapql.manage update_db_connection_localhost --ip 139.16.34.33
Load fixture data:

    python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layercategory.json
    python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layerqualitylevel.json

Add newly defined columns to existing tables:

    python -m app.damapql.manage sync_schema --app_label auth

Create security and access-control records:

    python -m app.damapql.manage createsuperuser
    python -m app.damapql.manage create_role --name app_manager --description "Full access to manage DCH layers and DB connections"
    python -m app.damapql.manage create_organization --name "Digital Arz"
    python -m app.damapql.manage create_department --organization_name "Digital Arz" --name "GeoAI development" --description "Core AI department"
    python -m app.damapql.manage create_app_user --role_name app_manager --organization_name "Digital Arz" --department_name "GeoAI development"

Export and import JSON data:

    python -m app.damapql.manage dumpdata --app_label auth --model User --output users_backup.json
    python -m app.damapql.manage loaddata --app_label auth --file users_backup.json

Generate security keys:

    python -m app.damapql.manage generate_crypto_key
    python -m app.damapql.manage generate_secret_key_hex --length 64

Add and reconcile Layer Info
    python -m app.damapql.manage create_layerinfo_from_app_models --app_label wasa
    python -m app.damapql.manage reconcile_layerinfo

        python -m app.damapql.manage reconcile_layerinfo \
            --layer_uuid <uuid>

        python -m app.damapql.manage reconcile_layerinfo \
            --table_name wasa_asset_disposal

        python -m app.damapql.manage reconcile_layerinfo \
            --check true

correct layer accessibility col names
    python -m app.damapql.manage sync_layer_accessibility_columns --table_name wasa_asset_disposal
refreh layer accessibility
    docker exec -it wasa_be python -m app.damapql.manage refresh_layer_accessibility --table_name wasa_asset_disposal --check true

"""
import binascii
import importlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from cryptography.fernet import Fernet
from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import Integer, Float, String, Boolean, JSON, text, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy_utils import ChoiceType

from app.damapql.auth.dtypes import EmailValidator
from app.damapql.auth.models import Profile, User, Role, Department, Organization
from app.damapql.cache.cache import redis_client, REDIS_NAMESPACE
from app.damapql.dch.api_enum import AccessibilityType
from app.damapql.dch.models import DBConnection, LayerInfo, LayerCategory, LayerQualityLevel, LayerAccessibility
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from app.damapql.settings import DCH_DATABASES
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.model_utils import ModelUtils
from app.damapql.utils.crypto import CryptoUtils
from app.damapql.utils.helper_scripts.table_schema_update import TableSchemaUpdater
from digitalarzengine.utils.singletons import da_logger
from settings import DA_APPS, BASE_DIR, DATABASES

# Project Imports

manage_router = APIRouter(tags=["manage"], responses={404: {"description": "Not found"}}, )


def create_dch_databases():
    """
    Create PostgreSQL databases defined in DCH_DATABASES.
    If a database already exists it is skipped.

    For newly created databases:
        - owner is set to configured user
        - PostGIS extensions are installed
    """

    for label, cfg in DCH_DATABASES.items():

        engine_name = str(cfg.get("engine", "")).lower()

        if engine_name != "postgresql":
            print(f"⏭️  Skipping '{label}' (engine={engine_name})")
            continue

        host = cfg.get("host")
        port = int(cfg.get("port") or 5432)
        db_name = cfg.get("name")
        db_user = cfg.get("user")
        db_password = cfg.get("password")

        if not all([host, db_name, db_user]):
            print(f"❌ Invalid configuration for '{label}'")
            continue

        print(f"\n🛠️  Checking database '{db_name}' ({label})")

        admin_url = URL.create(drivername="postgresql+psycopg2", username=db_user, password=db_password, host=host,
            port=port, database="postgres", )

        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

        try:
            with admin_engine.connect() as conn:

                # Check if database exists
                exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": db_name}).scalar()

                if exists:
                    print(f"⏭️  Database '{db_name}' already exists — skipping.")
                    continue

                # Create database
                conn.execute(text(f'CREATE DATABASE "{db_name}" OWNER "{db_user}"'))

                print(f"✅ Database '{db_name}' created with owner '{db_user}'")

        except Exception as e:
            print(f"❌ Failed creating '{db_name}': {e}")
            continue

        # Connect to new DB to install PostGIS
        db_url = URL.create(drivername="postgresql+psycopg2", username=db_user, password=db_password, host=host,
            port=port, database=db_name, )

        engine = create_engine(db_url, isolation_level="AUTOCOMMIT")

        try:
            with engine.connect() as conn:

                extensions = ["postgis", "postgis_topology", "postgis_raster"]

                for ext in extensions:
                    conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
                    print(f"✅ Extension '{ext}' installed")

            print(f"🎉 Database '{db_name}' ready.\n")

        except Exception as e:
            print(f"❌ Extension setup failed for '{db_name}': {e}")


def add_db_connection(db_key: Optional[str] = None):
    with DBConnection.managed_session() as session:
        source = DCH_DATABASES if db_key is None else DATABASES

        keys = source.keys() if db_key is None else [db_key]

        for key in keys:
            da_logger.info(f"working on db_key :{key}")
            if key not in source:
                da_logger.info(f"db_key {key} does not exist.")
                continue

            existing = session.query(DBConnection).filter_by(name=key).first()
            if existing:
                da_logger.info(f"Database Connection {key} already exists.")
                continue
            if key not in source:
                da_logger.info(f"Database Connection {key} does not exist.")
            if "cache" not in key:
                db_info = source[key]
                name = f"{key}_{db_info['host']}"
                if "password" in db_info:
                    db_info["password"] = CryptoUtils().encrypt_txt(db_info["password"])
                db_connection = DBConnection(name=name, params=db_info)
                db_connection.save(session=session)


def update_db_connection_localhost(ip: str):
    LOCALHOST_VALUES = {"localhost", "127.0.0.1", "::1"}

    with DBConnection.managed_session() as session:
        for conn in session.query(DBConnection).all():
            params = conn.params or {}
            host = params.get("host")

            if host in LOCALHOST_VALUES:
                conn.params = {**params, "host": ip}
                flag_modified(conn, "params")
                da_logger.info(f"Updated {conn.name}: {host} -> {ip}")

        session.commit()


# --- UTILS ---

def to_python_scalar(v: Any) -> Any:
    """Converts NumPy/Pandas/ChoiceType types to standard Python for JSON."""
    if isinstance(v, pd.Series):
        return to_python_scalar(v.iloc[0]) if not v.empty else None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.generic):
        return v.item()

    if hasattr(v, 'code'):
        return v.code

    if isinstance(v, list):
        return [to_python_scalar(item) for item in v]

    return v


def sqlalchemy_column_to_python_type(col) -> str:
    """Maps SQLAlchemy types to string names for Pydantic generation."""
    if isinstance(col.type, (Integer, Float)):
        return "number"
    elif isinstance(col.type, (String, ChoiceType)):
        return "str"
    elif isinstance(col.type, Boolean):
        return "bool"
    elif isinstance(col.type, (JSON, JSONB)):
        return "dict"
    elif hasattr(col.type, 'item_type'):
        return "list"
    return "Any"


def parse_params(argv: List[str]) -> Dict[str, Any]:
    """Parses CLI arguments into a dictionary."""
    params = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            key = arg[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                params[key] = argv[i + 1]
                i += 2
            else:
                params[key] = True
                i += 1
        else:
            i += 1
    return params


def _to_bool(value: Any) -> bool:
    """Convert CLI/string values to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


# --- APP SCAFFOLDING ---

def startapp(app_label: str):
    """Creates a new app directory with boilerplate files."""
    dir_path = os.path.join(BASE_DIR, app_label)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    file_names = ["__init__.py", "routers.py", "models.py", "admin.py"]
    for fname in file_names:
        with open(os.path.join(dir_path, fname), "w") as f:
            f.write(f'"""\n{fname} for {app_label} app\n"""\n')
    print(f"🚀 App '{app_label}' created at {dir_path}")


# --- SCHEMA & MIGRATIONS ---
def get_model_app_info(ModelCls):
    """
    Resolve the DA_APPS entry and BaseCls for a model class.

    Returns:
        app_label, app_config, BaseCls
    """
    model_module = getattr(ModelCls, "__module__", "")

    for app_label, app_config in DA_APPS.items():
        models_prefix = f'{app_config["path"]}.models'

        if not (model_module == models_prefix or model_module.startswith(f"{models_prefix}.")):
            continue

        configured_base = app_config.get("base")

        if configured_base is None:
            BaseCls = DBBase
        elif isinstance(configured_base, str):
            BaseCls = import_string(configured_base)
        else:
            BaseCls = configured_base

        return app_label, app_config, BaseCls

    raise ValueError(f"Could not resolve DA_APPS configuration for model "
                     f"'{ModelCls.__module__}.{ModelCls.__name__}'.")


def sync_schema(*, app_label: str | None = None, model_name: str | None = None, table_name: str | None = None,
        rename_map: dict | None = None, ):
    """
    Synchronize database schema using exactly one selector.

    Available selectors:
        app_label
            Synchronize all tables registered under the app Base.

        model_name
            Synchronize only the table mapped by the model.

        table_name
            Find the model by __tablename__ and synchronize its table.

    rename_map format:
        {
            "table_name": {
                "old_col_name": "new_col_name"
            }
        }

    Examples:
        sync_schema(app_label="wasa")

        sync_schema(model_name="WASAComplaint")

        sync_schema(table_name="tbl_wasa_complaints")

    CLI:
        python -m app.damapql.manage sync_schema --app_label wasa

        python -m app.damapql.manage sync_schema --model_name WASAComplaint

        python -m app.damapql.manage sync_schema --table_name tbl_wasa_complaints
    """
    rename_map = rename_map or {}

    selectors = {"app_label": app_label, "model_name": model_name, "table_name": table_name, }

    supplied_selectors = [selector_name for selector_name, selector_value in selectors.items() if
        selector_value is not None and str(selector_value).strip()]

    if not supplied_selectors:
        raise ValueError("At least one selector is required: "
                         "app_label, model_name, or table_name.")

    if len(supplied_selectors) > 1:
        raise ValueError("Only one selector can be supplied at a time. "
                         f"Received: {', '.join(supplied_selectors)}")

    selected_app_label: str
    ap: dict
    BaseCls = None
    selected_tables = []

    # =========================================================
    # Synchronize all models of one app
    # =========================================================
    if app_label:
        selected_app_label = app_label.strip()
        ap = DA_APPS.get(selected_app_label)

        if not ap:
            raise ValueError(f"App label '{selected_app_label}' not found.")

        importlib.import_module(f'{ap["path"]}.models')

        configured_base = ap.get("base")

        if configured_base is None:
            BaseCls = DBBase
        elif isinstance(configured_base, str):
            BaseCls = import_string(configured_base)
        else:
            BaseCls = configured_base

        selected_tables = list(BaseCls.metadata.sorted_tables)

    # =========================================================
    # Synchronize one model
    # =========================================================
    elif model_name:
        ModelCls = ModelUtils.get_model(model_name.strip())

        if ModelCls is None:
            raise ValueError(f"Model '{model_name}' not found.")

        (selected_app_label, ap, BaseCls,) = get_model_app_info(ModelCls)

        selected_tables = [ModelCls.__table__]

    # =========================================================
    # Synchronize one table by finding its model
    # =========================================================
    else:
        ModelCls = ModelUtils.get_model_by_tablename(table_name.strip())

        (selected_app_label, ap, BaseCls,) = get_model_app_info(ModelCls)

        selected_tables = [ModelCls.__table__]

    if BaseCls is None:
        raise RuntimeError("Could not resolve SQLAlchemy BaseCls.")

    if not selected_tables:
        raise ValueError("No SQLAlchemy tables were selected for synchronization.")

    db_key = ap.get("db_key")

    if not db_key:
        raise ValueError(f"App '{selected_app_label}' does not define db_key.")

    engine = BaseCls.get_engine(db_key=db_key)

    da_logger.info(f"Schema synchronization: "
                   f"app={selected_app_label}, "
                   f"base={BaseCls.__name__}, "
                   f"db_key={db_key}")

    updater = TableSchemaUpdater(engine=engine, logger=da_logger, )

    existing_table_names = set(updater.get_table_names())

    da_logger.info("Selected tables: "
                   f"{[table.fullname for table in selected_tables]}")

    created_tables = []
    synchronized_tables = []
    failed_tables = []

    for table_obj in selected_tables:
        table_name_value = table_obj.name

        try:
            if table_name_value not in existing_table_names:
                updater.create_table(table_obj)

                existing_table_names.add(table_name_value)
                created_tables.append(table_name_value)

                continue

            table_rename_map = rename_map.get(table_name_value, {}, )

            updater.sync_table(table_obj, rename_map=table_rename_map, )

            synchronized_tables.append(table_name_value)

        except Exception as exc:
            failed_tables.append((table_name_value, str(exc)))

            da_logger.exception(f"Schema synchronization failed for "
                                f"'{table_name_value}': {exc}")

    print("\n========================================")
    print("Schema Synchronization Summary")
    print("========================================")
    print(f"App          : {selected_app_label}")
    print(f"Base         : {BaseCls.__name__}")
    print(f"Database key : {db_key}")
    print(f"Created      : {len(created_tables)}")
    print(f"Synchronized : {len(synchronized_tables)}")
    print(f"Failed       : {len(failed_tables)}")

    if created_tables:
        print("\nCreated tables:")
        for created_table in created_tables:
            print(f"  - {created_table}")

    if synchronized_tables:
        print("\nSynchronized tables:")
        for synchronized_table in synchronized_tables:
            print(f"  - {synchronized_table}")

    if failed_tables:
        print("\nFailed tables:")
        for failed_table, error in failed_tables:
            print(f"  - {failed_table}: {error}")

        raise RuntimeError(f"Schema synchronization failed for "
                           f"{len(failed_tables)} table(s).")

    print(f"\n✅ Schema sync for "
          f"'{selected_app_label}' complete.")


def sync_sequences(app_label: str, table_names: str = None):
    """
    Sync PostgreSQL ID sequences with current MAX(id) values.

    Usage:
        python -m app.damapql.manage sync_sequences --app_label dch
        python -m app.damapql.manage sync_sequences --app_label auth
        python -m app.damapql.manage sync_sequences --app_label dch --table_names dch_map_info,dch_layer_info
    """
    ap = DA_APPS.get(app_label)
    if not ap:
        print(f"❌ App label '{app_label}' not found.")
        return

    importlib.import_module(f'{ap["path"]}.models')

    if "base" not in ap:
        BaseCls = DBBase
    else:
        BaseCls = import_string(ap["base"]) if isinstance(ap["base"], str) else ap["base"]

    engine = BaseCls.get_engine(db_key=ap["db_key"])

    requested_tables = None
    if table_names:
        requested_tables = {t.strip() for t in table_names.split(",") if t.strip()}

    synced = []
    skipped = []

    with BaseCls.managed_session(app_label=app_label) as session:
        for table in BaseCls.metadata.sorted_tables:
            table_name = table.name

            if requested_tables and table_name not in requested_tables:
                continue

            pk_cols = list(table.primary_key.columns)
            if len(pk_cols) != 1:
                skipped.append((table_name, "composite primary key"))
                continue

            pk_col = pk_cols[0]
            pk_name = pk_col.name

            # only handle integer/bigint autoincrement-style PKs
            try:
                python_type = pk_col.type.python_type
            except NotImplementedError:
                skipped.append((table_name, f"unsupported pk type for '{pk_name}'"))
                continue

            if python_type is not int:
                skipped.append((table_name, f"non-integer pk '{pk_name}'"))
                continue

            seq = session.execute(text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": table_name, "col": pk_name}).scalar()

            if not seq:
                skipped.append((table_name, f"no backing sequence for '{pk_name}'"))
                continue

            max_id = session.execute(text(f'SELECT COALESCE(MAX("{pk_name}"), 0) FROM "{table_name}"')).scalar() or 0

            # set sequence so next nextval() becomes max_id + 1
            session.execute(text("SELECT setval(:seq, :val, :is_called)"),
                {"seq": seq, "val": max_id, "is_called": True})

            synced.append((table_name, pk_name, max_id, seq))

        session.commit()

    print(f"✅ Sequence sync complete for app '{app_label}'")

    if synced:
        print("\nSynced tables:")
        for table_name, pk_name, max_id, seq in synced:
            print(f"  - {table_name}.{pk_name} -> max={max_id}, sequence={seq}")

    if skipped:
        print("\nSkipped tables:")
        for table_name, reason in skipped:
            print(f"  - {table_name}: {reason}")


def import_string(dotted_path: str):
    module_path, attr_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


def migrate(app_label: str):
    ap = DA_APPS.get(app_label)
    if not ap:
        raise Exception(f"the app {app_label} does not exist")
        return

    importlib.import_module(f'{ap["path"]}.models')

    if "base" not in ap:
        BaseCls = DBBase
    else:
        BaseCls = import_string(ap["base"]) if isinstance(ap["base"], str) else ap["base"]
    da_logger.info(f"{app_label} migrate using {BaseCls.__name__}")
    engine = BaseCls.get_engine(db_key=ap['db_key'])

    BaseCls.metadata.create_all(engine)
    print(f"✅ Migration for '{app_label}' complete.")


# --- DATA MANAGEMENT ---

def dumpdata(app_label: str, model: str, output: Optional[str] = None):
    """Dumps model records to a JSON file, resolving ChoiceTypes."""

    ap = DA_APPS.get(app_label)
    if output is None:
        output = Path(ap["path"].replace(".", "/")) / "fixtures" / f"{model}.json"
    module = importlib.import_module(f'{ap["path"]}.models')
    model_class = getattr(module, model)

    output_path = Path(output)

    # ✅ Step 1: Resolve path
    if not output_path.is_absolute():
        output_path = Path(BASE_DIR) / output_path

    output_path = output_path.resolve()

    # ✅ Step 2: Delete if exists
    if output_path.exists():
        print(f"⚠️ Deleting existing file: {output_path}")
        output_path.unlink()

    with DBBase.managed_session(app_label=app_label) as session:
        results = session.query(model_class).all()
        data = []
        pk_name = model_class.__mapper__.primary_key[0].name

        for obj in results:
            fields = {col.name: to_python_scalar(getattr(obj, col.name)) for col in model_class.__table__.columns if
                col.name != pk_name}
            data.append({"model": model, "pk": getattr(obj, pk_name), "fields": fields})

    # ✅ Step 3: Ensure directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ✅ Step 4: Write file
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"📤 Dumped {len(data)} records to {output_path}")


def loaddata(app_label: str, file: str):
    """
    Load records from a JSON fixture file and reset PostgreSQL SERIAL sequences.

    Behavior:
    - existing PK -> update
    - new PK -> insert
    """

    file_path = Path(file)

    if not file_path.is_absolute():
        file_path = Path(BASE_DIR) / file_path

    file_path = file_path.resolve()

    if not file_path.exists() or not file_path.is_file():
        print(f"❌ Fixture file not found: {file_path}")
        return

    ap = DA_APPS.get(app_label)
    if not ap:
        print(f"❌ App label '{app_label}' not found in DA_APPS.")
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        print(f"❌ Failed to read JSON file: {file_path}")
        raise

    module = importlib.import_module(f'{ap["path"]}.models')
    touched_models = set()

    with DBBase.managed_session(app_label=app_label) as session:
        try:
            for entry in records:
                model_name = entry["model"]
                pk_value = entry["pk"]
                fields = entry.get("fields", {})

                model_class = getattr(module, model_name)
                touched_models.add(model_class)

                pk_name = model_class.__mapper__.primary_key[0].name

                payload = dict(fields)
                payload[pk_name] = pk_value

                obj = model_class(**payload)

                # existing PK -> update, new PK -> insert
                session.merge(obj)

            session.commit()

            reset_pk_sequences(DBBase.get_engine(app_label=app_label), session, touched_models)

            print(f"📥 Loaded {len(records)} records from {file_path}")

        except Exception:
            session.rollback()
            print("❌ Data loading failed. Rolling back transaction.")
            raise


def reset_pk_sequences(engine, session, touched_models):
    """Fixes ID sequences to avoid PK collisions after bulk load."""
    dialect = engine.dialect.name
    if dialect != "postgresql": return

    for model_class in touched_models:
        table_name = model_class.__table__.name
        pk_name = model_class.__mapper__.primary_key[0].name
        seq_query = text("SELECT pg_get_serial_sequence(:tbl, :col)")
        seq = session.execute(seq_query, {"tbl": table_name, "col": pk_name}).scalar()

        if seq:
            max_id = session.execute(text(f"SELECT MAX({pk_name}) FROM {table_name}")).scalar() or 0
            session.execute(text("SELECT setval(:seq, :v)"), {"seq": seq, "v": max_id})
    session.commit()


def create_layerinfo_from_app_models(
    app_label: Optional[str] = None,
    model_name: Optional[str] = None,
    table_name: Optional[str] = None,
    layer_category_id: Optional[int] = None,
    overwrite: bool = True,
    include_non_spatial: bool = False,
):
    """
    Generate LayerInfo and LayerAccessibility entries from SQLAlchemy app models.
    Applies DEFAULT_EXTENT from settings if the table is empty or has no bounding box.

    CLI Examples:
        python -m app.damapql.manage create_layerinfo_from_app_models --app_label wasa
        python -m app.damapql.manage create_layerinfo_from_app_models --model_name WASAComplaint
        python -m app.damapql.manage create_layerinfo_from_app_models --table_name tbl_wasa_complaints
    """
    overwrite = _to_bool(overwrite)
    include_non_spatial = _to_bool(include_non_spatial)

    # --- Step 1: Validate and Resolve Selector ---
    selectors = {"app_label": app_label, "model_name": model_name, "table_name": table_name}
    supplied = [k for k, v in selectors.items() if v is not None and str(v).strip()]

    if not supplied:
        raise ValueError("At least one selector is required: app_label, model_name, or table_name.")
    if len(supplied) > 1:
        raise ValueError(f"Only one selector can be supplied at a time. Received: {', '.join(supplied)}")

    target_model_cls = None

    if model_name:
        target_model_cls = ModelUtils.get_model(model_name.strip())
        if not target_model_cls:
            raise ValueError(f"Model '{model_name}' not found.")
        app_label, _, _ = get_model_app_info(target_model_cls)

    elif table_name:
        target_model_cls = ModelUtils.get_model_by_tablename(table_name.strip())
        if not target_model_cls:
            raise ValueError(f"Table '{table_name}' mapped model not found.")
        app_label, _, _ = get_model_app_info(target_model_cls)

    service = LayerInfoService()

    with LayerInfo.managed_session() as session:
        # --- Step 2: Resolve Defaults ---
        if layer_category_id is None:
            qs = select(LayerCategory).where(LayerCategory.main == "Other")
            category = session.execute(qs).scalar_one_or_none()
            if category is None:
                raise ValueError("LayerCategory with main='Other' not found")
            layer_category_id = category.id

        qs = select(LayerQualityLevel).where(LayerQualityLevel.name == "Not Accessed")
        quality = session.execute(qs).scalar_one_or_none()
        if quality is None:
            raise ValueError("QualityLevel with name='Not Accessed' not found")

        # --- Step 3: Execute Generation ---
        result = service.save_app_layer_infos(
            db=session,
            app_label=app_label,
            layer_category_id=layer_category_id,
            quality_level_id=quality.id,
            user=None,
            overwrite=overwrite,
            include_non_spatial=include_non_spatial,
            target_model=target_model_cls,
        )

        session.flush()

        # --- Step 4: Apply Default Extent Fallback for Empty Tables ---
        # Query the layers that were created or updated under this app_label
        layer_query = select(LayerInfo).where(LayerInfo.app_label == app_label)
        if target_model_cls:
            layer_query = layer_query.where(LayerInfo.layer_name == target_model_cls.__tablename__)

        created_layers = session.execute(layer_query).scalars().all()

        fallback_count = 0
        for layer in created_layers:
            # Check if extent is missing, empty, or all zeros
            has_extent = layer.extent and len(layer.extent) == 4 and any(v != 0 for v in layer.extent)

            if not has_extent:
                srid = layer.srid or 3857
                native_ext, ext_3857 = DCHUtils.get_default_extents(target_srid=srid)

                layer.extent = native_ext
                layer.extent_3857 = ext_3857

                extra = dict(layer.extra_metadata or {})
                extra["is_default_extent"] = True
                layer.extra_metadata = extra

                flag_modified(layer, "extent")
                flag_modified(layer, "extent_3857")
                flag_modified(layer, "extra_metadata")

                fallback_count += 1
                da_logger.info(
                    f"Applied settings default extent to layer '{layer.layer_name}' "
                    f"(SRID={srid}): {native_ext}"
                )

        session.commit()

        if fallback_count > 0:
            print(f"🌍 Applied fallback extent from settings to {fallback_count} empty layer(s).")

    print(result)
    return result

"""
Usage:
    create_layerinfo_from_raster(
        title="LULC 2024",
        raster_path="media/rasters/lulc_2024.tif",
    )
    
    create_layerinfo_from_raster(
        title="DEM Lahore",
        raster_path=r"D:\gis_data\dem\lahore_dem.tif",
        temporal="static",
        data_model_type="RASTER",
        overwrite=True,
    )
"""


def create_layerinfo_from_raster(title: str, raster_path: str, layer_category_id=None, temporal: str = None,
        data_model_type: str = None, overwrite: bool = False, app_label: str = "dch", layer_name: str = None, ):
    service = LayerInfoService()

    with LayerInfo.managed_session() as session:
        if layer_category_id is None:
            qs = select(LayerCategory).where(LayerCategory.main == "Other")
            result = session.execute(qs)
            category = result.scalar_one_or_none()

            if category is None:
                raise ValueError("LayerCategory with main='Other' not found")

            layer_category_id = category.id

        qs = select(LayerQualityLevel).where(LayerQualityLevel.name == "Not Accessed")
        result = session.execute(qs)
        quality = result.scalar_one_or_none()

        if quality is None:
            raise ValueError("QualityLevel with name='Not Accessed' not found")

        obj, changed = service.save_raster_layer_info(db=session, title=title, raster_path=raster_path,
            layer_category_id=layer_category_id, temporal=temporal, data_model_type=data_model_type, user=None,
            overwrite=overwrite, app_label=app_label, layer_name=layer_name, )

        if getattr(obj, "quality_level_id", None) is None:
            obj.quality_level_id = quality.id

        session.commit()

    print({"msg": "Raster layer info created/updated" if changed else "Raster layer already exists",
        "uuid": getattr(obj, "uuid", None), "layer_name": getattr(obj, "layer_name", None),
        "title": getattr(obj, "title", None),
        "path": (obj.extra_metadata.get("cog_path") if getattr(obj, "extra_metadata", None) else raster_path), })
    return obj


def reconcile_layerinfo(layer_uuid: str = None, table_name: str = None, model_name: str = None, schema: str = "public",
        check: bool = False, ):
    """
    Reconcile LayerInfo and LayerAccessibility metadata against real DB tables.

    The actual database table is treated as the source of truth.
    Orphan LayerInfo records (where the underlying table does not exist)
    will be deleted from the database unless check=True.

    If a table exists but contains no rows or non-null geometries, default extents
    from DCHUtils.get_default_extents(...) are applied.

    Usage:
        python -m app.damapql.manage reconcile_layerinfo
        python -m app.damapql.manage reconcile_layerinfo --layer_uuid <uuid>
        python -m app.damapql.manage reconcile_layerinfo --table_name wasa_asset_disposal
        python -m app.damapql.manage reconcile_layerinfo --model_name CanalCommandArea
        python -m app.damapql.manage reconcile_layerinfo --check true
    """
    check = _to_bool(check)
    started_at = time.time()
    # ---------------------------------------------------------
    # Resolve model_name -> table_name
    # ---------------------------------------------------------
    resolved_model_name = None

    if model_name:
        ModelCls = ModelUtils.get_model(model_name.strip())

        if ModelCls is None:
            raise ValueError(f"Model '{model_name}' not found.")

        resolved_model_name = ModelCls.__name__
        model_table_name = ModelCls.__tablename__

        # Avoid ambiguous selectors
        if table_name and table_name != model_table_name:
            raise ValueError(
                f"Model '{model_name}' maps to table "
                f"'{model_table_name}', but --table_name '{table_name}' was supplied."
            )

        table_name = model_table_name

    def quote_ident(value: str) -> str:
        """Safely quote a PostgreSQL identifier."""
        return f'"{str(value).replace(chr(34), chr(34) * 2)}"'

    def normalize_geometry_type(value: str | None) -> str | None:
        """Normalize PostGIS geometry names for DAMap frontend usage."""
        if not value:
            return None

        raw = str(value).strip()
        if raw.upper().startswith("ST_"):
            raw = raw[3:]

        normalized_key = raw.replace("_", "").replace(" ", "").lower()
        geometry_map = {
            "point": "Point", "multipoint": "MultiPoint", "linestring": "LineString",
            "multilinestring": "MultiLineString", "polyline": "LineString", "multipolyline": "MultiLineString",
            "polygon": "Polygon", "multipolygon": "MultiPolygon", "geometry": "Geometry",
            "geometrycollection": "GeometryCollection", "circularstring": "CircularString",
            "compoundcurve": "CompoundCurve", "curvepolygon": "CurvePolygon", "multicurve": "MultiCurve",
            "multisurface": "MultiSurface", "triangle": "Triangle", "tin": "TIN",
            "polyhedralsurface": "PolyhedralSurface",
        }
        return geometry_map.get(normalized_key, raw)

    def normalize_geometry_types(values) -> list[str] | None:
        normalized = []
        for value in values or []:
            geometry_type = normalize_geometry_type(value)
            if geometry_type and geometry_type not in normalized:
                normalized.append(geometry_type)
        return normalized or None

    def values_different(old_value, new_value) -> bool:
        """Compare metadata values while handling tuples/lists consistently."""
        if isinstance(old_value, tuple):
            old_value = list(old_value)
        if isinstance(new_value, tuple):
            new_value = list(new_value)
        return old_value != new_value

    with LayerInfo.managed_session() as session:
        query = session.query(LayerInfo).options(selectinload(LayerInfo.layer_accessibility))

        if layer_uuid:
            query = query.filter(LayerInfo.uuid == layer_uuid)

        if table_name:
            query = query.join(LayerInfo.layer_accessibility).filter(
                LayerAccessibility.params.op("->>")("table_name") == table_name
            )

        layers = query.all()

        if not layers:
            print("❌ No matching LayerInfo records found.")
            return

        print("\n================================================")
        print("DAMap LayerInfo Metadata Reconciliation")
        print("================================================")
        print(f"Mode         : {'CHECK ONLY' if check else 'UPDATE'}")
        print(f"Schema       : {schema}")
        print(f"Layer UUID   : {layer_uuid or 'ALL'}")
        print(f"Table filter : {table_name or 'ALL'}")
        print(f"Layers found : {len(layers)}")
        print("================================================\n")

        checked_count = 0
        changed_count = 0
        updated_count = 0
        deleted_count = 0
        unchanged_count = 0
        skipped_count = 0
        failed_count = 0

        corrections = {
            "columns": 0, "geometry_column": 0, "primary_key": 0, "srid": 0,
            "geometry_type": 0, "extent": 0, "extent_3857": 0, "row_count": 0,
        }

        for layer in layers:
            access = layer.layer_accessibility

            print(f"Layer: {layer.title}")
            print(f"   UUID       : {layer.uuid}")
            print(f"   Layer name : {layer.layer_name}")

            # ---------------------------------------------------------
            # Validate accessibility
            # ---------------------------------------------------------
            if not access:
                skipped_count += 1
                print("   ⏭️ Skipped: LayerAccessibility is missing.\n")
                continue

            if not access.accessibility_type or access.accessibility_type.code != AccessibilityType.DB.name:
                skipped_count += 1
                print("   ⏭️ Skipped: layer is not database-backed.\n")
                continue

            actual_table_name = access.table_name
            connection_name = access.conn_name

            if not actual_table_name or not connection_name:
                skipped_count += 1
                print("   ⏭️ Skipped: table_name or conn_name is missing from LayerAccessibility.\n")
                continue

            db_conn = DBConnection.get_db_connection(con_name=connection_name)
            if not db_conn:
                skipped_count += 1
                print(f"   ❌ Skipped: DB connection '{connection_name}' was not found.\n")
                continue

            engine = db_conn.get_sqlalchemy_engine()
            safe_schema = quote_ident(schema)
            safe_table_name = quote_ident(actual_table_name)
            safe_table = f"{safe_schema}.{safe_table_name}"

            try:
                with engine.connect() as conn:
                    # -------------------------------------------------
                    # Read actual table columns
                    # -------------------------------------------------
                    column_rows = conn.execute(text("""
                        SELECT column_name, data_type, udt_name
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = :table_name
                        ORDER BY ordinal_position
                    """), {"schema": schema, "table_name": actual_table_name}).mappings().all()

                    # -------------------------------------------------
                    # Handle non-existent table (Delete Record)
                    # -------------------------------------------------
                    if not column_rows:
                        if check:
                            skipped_count += 1
                            print(f"   🗑️ Check Mode: Layer marked for deletion (table does not exist: {schema}.{actual_table_name})\n")
                            continue

                        try:
                            session.delete(layer)
                            session.commit()
                            deleted_count += 1
                            print(f"   🗑️ Deleted orphan LayerInfo record '{layer.layer_name}' ({layer.uuid}) because table {schema}.{actual_table_name} does not exist.\n")
                        except Exception as delete_error:
                            session.rollback()
                            failed_count += 1
                            print(f"   ❌ Failed to delete orphan LayerInfo record: {delete_error}\n")
                        continue

                    # -------------------------------------------------
                    # Read primary key columns
                    # -------------------------------------------------
                    pk_rows = conn.execute(text("""
                        SELECT kcu.column_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                         AND tc.table_schema = kcu.table_schema
                        WHERE tc.constraint_type = 'PRIMARY KEY'
                          AND tc.table_schema = :schema
                          AND tc.table_name = :table_name
                        ORDER BY kcu.ordinal_position
                    """), {"schema": schema, "table_name": actual_table_name}).mappings().all()

                    pk_cols = [row["column_name"] for row in pk_rows]

                    # -------------------------------------------------
                    # Detect geometry column
                    # -------------------------------------------------
                    geometry_columns = []
                    columns = []

                    for row in column_rows:
                        column_name = row["column_name"]
                        data_type = str(row["data_type"] or "").lower()
                        udt_name = str(row["udt_name"] or "").lower()

                        if udt_name in {"geometry", "geography"}:
                            geometry_columns.append(column_name)
                            continue

                        columns.append({
                            "name": column_name,
                            "d_type": ManageDataFrameUtils.getColGenericType(data_type),
                        })

                    old_params = dict(access.params or {})
                    old_geometry_column = old_params.get("g_col")

                    if old_geometry_column in geometry_columns:
                        geometry_column = old_geometry_column
                    else:
                        geometry_column = geometry_columns[0] if geometry_columns else None

                    # -------------------------------------------------
                    # Spatial metadata inspection
                    # -------------------------------------------------
                    srid = None
                    geometry_types = None
                    extent = None
                    extent_3857 = None
                    non_null_geometry_count = 0

                    row_count = int(conn.execute(text(f"SELECT COUNT(*) FROM {safe_table}")).scalar() or 0)

                    if geometry_column:
                        safe_geometry = quote_ident(geometry_column)

                        # Step A: Query PostGIS catalog view geometry_columns
                        catalog_metadata = conn.execute(text("""
                            SELECT srid, type
                            FROM geometry_columns
                            WHERE f_table_schema = :schema
                              AND f_table_name = :table_name
                              AND f_geometry_column = :geometry_column
                            LIMIT 1
                        """), {
                            "schema": schema,
                            "table_name": actual_table_name,
                            "geometry_column": geometry_column,
                        }).mappings().first()

                        if catalog_metadata:
                            catalog_srid = catalog_metadata.get("srid")
                            catalog_type = catalog_metadata.get("type")

                            if catalog_srid not in (None, 0):
                                srid = int(catalog_srid)
                            if catalog_type:
                                geometry_types = normalize_geometry_types([catalog_type])

                        # Step B: Inspect stored rows for actual geometry types and SRIDs
                        actual_metadata = conn.execute(text(f"""
                            SELECT
                                COUNT(*) AS geometry_count,
                                ARRAY_AGG(DISTINCT ST_GeometryType({safe_geometry})) AS geometry_types,
                                ARRAY_AGG(DISTINCT ST_SRID({safe_geometry})) AS srids
                            FROM {safe_table}
                            WHERE {safe_geometry} IS NOT NULL
                              AND NOT ST_IsEmpty({safe_geometry})
                        """)).mappings().first()

                        if actual_metadata:
                            non_null_geometry_count = int(actual_metadata.get("geometry_count") or 0)
                            actual_types = normalize_geometry_types(actual_metadata.get("geometry_types"))

                            if actual_types:
                                geometry_types = actual_types

                            actual_srids = [
                                int(value) for value in (actual_metadata.get("srids") or [])
                                if value not in (None, 0)
                            ]
                            if actual_srids:
                                srid = actual_srids[0]
                                if len(set(actual_srids)) > 1:
                                    print(f"   ⚠️ Mixed SRIDs found: {sorted(set(actual_srids))}")

                        # Step C: Native Extent from DB Features
                        extent_row = conn.execute(text(f"""
                            SELECT
                                ST_XMin(bounds)::float AS minx,
                                ST_YMin(bounds)::float AS miny,
                                ST_XMax(bounds)::float AS maxx,
                                ST_YMax(bounds)::float AS maxy
                            FROM (
                                SELECT ST_Extent({safe_geometry})::geometry AS bounds
                                FROM {safe_table}
                                WHERE {safe_geometry} IS NOT NULL
                                  AND NOT ST_IsEmpty({safe_geometry})
                            ) q
                            WHERE bounds IS NOT NULL
                        """)).mappings().first()

                        if extent_row:
                            extent = [
                                extent_row["minx"], extent_row["miny"],
                                extent_row["maxx"], extent_row["maxy"]
                            ]

                        # Step D: EPSG:3857 Extent from DB Features
                        if extent and srid and srid > 0:
                            if srid == 3857:
                                extent_3857 = list(extent)
                            else:
                                extent_3857_row = conn.execute(text(f"""
                                    SELECT
                                        ST_XMin(bounds)::float AS minx,
                                        ST_YMin(bounds)::float AS miny,
                                        ST_XMax(bounds)::float AS maxx,
                                        ST_YMax(bounds)::float AS maxy
                                    FROM (
                                        SELECT ST_Extent(ST_Transform({safe_geometry}, 3857))::geometry AS bounds
                                        FROM {safe_table}
                                        WHERE {safe_geometry} IS NOT NULL
                                          AND NOT ST_IsEmpty({safe_geometry})
                                    ) q
                                    WHERE bounds IS NOT NULL
                                """)).mappings().first()

                                if extent_3857_row:
                                    extent_3857 = [
                                        extent_3857_row["minx"], extent_3857_row["miny"],
                                        extent_3857_row["maxx"], extent_3857_row["maxy"]
                                    ]

                    # -------------------------------------------------
                    # Fallback to DCHUtils Default Extents if Empty
                    # -------------------------------------------------
                    is_default_extent = False
                    if not extent or non_null_geometry_count == 0 or row_count == 0:
                        resolved_srid = srid or layer.srid or 3857
                        extent, extent_3857 = DCHUtils.get_default_extents(target_srid=resolved_srid)
                        is_default_extent = True
                        if srid is None:
                            srid = resolved_srid
                        print(f"   ℹ️ Applied fallback extent from DCHUtils (SRID={resolved_srid}): {extent}")

                checked_count += 1

                # -----------------------------------------------------
                # Build corrected accessibility params and metadata
                # -----------------------------------------------------
                new_params = {
                    **old_params,
                    "conn_name": connection_name,
                    "table_name": actual_table_name,
                    "columns": columns,
                    "g_col": geometry_column,
                    "pk_cols": pk_cols,
                }

                old_extra_metadata = dict(layer.extra_metadata or {})
                new_extra_metadata = {
                    **old_extra_metadata,
                    "row_count": row_count,
                    "non_null_geometry_count": non_null_geometry_count,
                    "is_default_extent": is_default_extent,
                    "layerinfo_reconciled": True,
                    "layerinfo_reconciled_at": datetime.now(timezone.utc).isoformat(),
                    "layerinfo_reconciled_from": f"{schema}.{actual_table_name}",
                }

                field_changes = {}

                if values_different(old_params.get("columns"), columns):
                    field_changes["columns"] = (old_params.get("columns"), columns)
                    corrections["columns"] += 1

                if values_different(old_params.get("g_col"), geometry_column):
                    field_changes["geometry_column"] = (old_params.get("g_col"), geometry_column)
                    corrections["geometry_column"] += 1

                if values_different(old_params.get("pk_cols", []), pk_cols):
                    field_changes["primary_key"] = (old_params.get("pk_cols", []), pk_cols)
                    corrections["primary_key"] += 1

                if values_different(layer.srid, srid):
                    field_changes["srid"] = (layer.srid, srid)
                    corrections["srid"] += 1

                if values_different(layer.geom_type, geometry_types):
                    field_changes["geometry_type"] = (layer.geom_type, geometry_types)
                    corrections["geometry_type"] += 1

                if values_different(layer.extent, extent):
                    field_changes["extent"] = (layer.extent, extent)
                    corrections["extent"] += 1

                if values_different(layer.extent_3857, extent_3857):
                    field_changes["extent_3857"] = (layer.extent_3857, extent_3857)
                    corrections["extent_3857"] += 1

                if old_extra_metadata.get("row_count") != row_count:
                    field_changes["row_count"] = (old_extra_metadata.get("row_count"), row_count)
                    corrections["row_count"] += 1

                changed = bool(field_changes)

                print(f"   Table      : {schema}.{actual_table_name}")
                print(f"   Connection : {connection_name}")
                print(f"   Columns    : {len(columns)}")
                print(f"   PK         : {pk_cols}")
                print(f"   Geometry   : {geometry_column}")
                print(f"   Geom types : {geometry_types}")
                print(f"   SRID       : {srid}")
                print(f"   Rows       : {row_count}")
                print(f"   Spatial rows: {non_null_geometry_count}")

                if not changed:
                    unchanged_count += 1
                    print("   ✅ Metadata is already correct.\n")
                    continue

                changed_count += 1
                print("   Changes:")
                for field_name, (old_value, new_value) in field_changes.items():
                    print(f"      - {field_name}: {old_value} -> {new_value}")

                if check:
                    print("   🟡 Check mode: changes were not saved.\n")
                    continue

                # -----------------------------------------------------
                # Save reconciliation
                # -----------------------------------------------------
                try:
                    access.params = new_params
                    flag_modified(access, "params")

                    layer.srid = int(srid) if srid else None
                    layer.geom_type = geometry_types
                    layer.extent = extent
                    layer.extent_3857 = extent_3857
                    layer.extra_metadata = new_extra_metadata

                    flag_modified(layer, "geom_type")
                    flag_modified(layer, "extent")
                    flag_modified(layer, "extent_3857")
                    flag_modified(layer, "extra_metadata")

                    session.commit()
                    updated_count += 1
                    print("   ✅ Reconciliation saved.\n")

                except Exception as save_error:
                    session.rollback()
                    failed_count += 1
                    print(f"   ❌ Failed to save reconciliation: {save_error}\n")

            except Exception as error:
                session.rollback()
                failed_count += 1
                print(f"   ❌ Reconciliation failed: {error}\n")
                da_logger.exception(f"LayerInfo reconciliation failed for {layer.layer_name}: {error}")

        elapsed = time.time() - started_at

        summary = {
            "checked_layers": checked_count,
            "changed_layers": changed_count,
            "updated_layers": updated_count,
            "deleted_layers": deleted_count,
            "unchanged_layers": unchanged_count,
            "skipped_layers": skipped_count,
            "failed_layers": failed_count,
            "columns_corrected": corrections["columns"],
            "geometry_column_fixed": corrections["geometry_column"],
            "primary_keys_fixed": corrections["primary_key"],
            "srids_corrected": corrections["srid"],
            "geometry_types_fixed": corrections["geometry_type"],
            "extents_corrected": corrections["extent"],
            "extent_3857_fixed": corrections["extent_3857"],
            "row_counts_updated": corrections["row_count"],
            "elapsed_seconds": round(elapsed, 2),
        }
        print("\n================================================")
        print("LayerInfo Reconciliation Summary")
        print("================================================")
        print(f"Checked layers       : {summary['checked_layers']}")
        print(f"Changed layers       : {summary['changed_layers']}")
        print(f"Updated layers       : {summary['updated_layers']}")
        print(f"Deleted layers       : {summary['deleted_layers']}")
        print(f"Unchanged layers     : {summary['unchanged_layers']}")
        print(f"Skipped layers       : {summary['skipped_layers']}")
        print(f"Failed layers        : {summary['failed_layers']}")
        print("------------------------------------------------")
        print(f"Columns corrected    : {summary['columns_corrected']}")
        print(f"Geometry col fixed   : {summary['geometry_column_fixed']}")
        print(f"Primary keys fixed   : {summary['primary_keys_fixed']}")
        print(f"SRIDs corrected      : {summary['srids_corrected']}")
        print(f"Geometry types fixed : {summary['geometry_types_fixed']}")
        print(f"Extents corrected    : {summary['extents_corrected']}")
        print(f"3857 extents fixed   : {summary['extent_3857_fixed']}")
        print(f"Row counts updated   : {summary['row_counts_updated']}")
        print(f"Completed in         : {summary['elapsed_seconds']:.2f} seconds")
        if check:
            print("\nℹ️ Check mode was enabled. No database changes were saved.")
        return summary

def refresh_layer_accessibility(table_name: str = None, layer_uuid: str = None, check: bool = False,
        schema: str = "public", ):
    """
    Refresh DB-backed LayerAccessibility metadata from the real database table.
    Orphan LayerInfo records (where table does not exist) are deleted unless check=True.

    Usage:
        python -m app.damapql.manage refresh_layer_accessibility
        python -m app.damapql.manage refresh_layer_accessibility --table_name wasa_asset_disposal
        python -m app.damapql.manage refresh_layer_accessibility --layer_uuid <uuid>
        python -m app.damapql.manage refresh_layer_accessibility --table_name wasa_asset_disposal --check true
    """

    check = _to_bool(check)
    started_at = time.time()

    with LayerInfo.managed_session() as session:
        query = session.query(LayerInfo).options(selectinload(LayerInfo.layer_accessibility))

        if table_name:
            query = query.join(LayerInfo.layer_accessibility).filter(
                LayerAccessibility.params.op("->>")("table_name") == table_name)

        if layer_uuid:
            query = query.filter(LayerInfo.uuid == layer_uuid)

        layers = query.all()

        if not layers:
            print("❌ No matching layers found.")
            return

        print("\n=========================================")
        print("DAMap Layer Accessibility Refresh")
        print("=========================================")
        print(f"Mode        : {'CHECK ONLY' if check else 'UPDATE'}")
        print(f"Schema      : {schema}")
        print(f"Layers found: {len(layers)}\n")

        checked_count = 0
        updated_count = 0
        deleted_count = 0
        skipped_count = 0
        failed_count = 0

        total_added_cols = 0
        total_removed_cols = 0
        total_geom_fixed = 0
        total_pk_fixed = 0
        total_extent_updated = 0
        total_srid_updated = 0

        for layer in layers:
            access = layer.layer_accessibility

            if not access:
                skipped_count += 1
                print(f"⏭️  {layer.layer_name}: no accessibility")
                continue

            if (not access.accessibility_type or access.accessibility_type.code != AccessibilityType.DB.name):
                skipped_count += 1
                print(f"⏭️  {layer.layer_name}: not DB-backed")
                continue

            if not access.table_name or not access.conn_name:
                skipped_count += 1
                print(f"⏭️  {layer.layer_name}: missing table_name or conn_name")
                continue

            db_conn = DBConnection.get_db_connection(con_name=access.conn_name)
            if not db_conn:
                skipped_count += 1
                print(f"❌ {layer.layer_name}: DB connection not found: {access.conn_name}")
                continue

            engine = db_conn.get_sqlalchemy_engine()

            try:
                with engine.connect() as conn:
                    col_rows = (conn.execute(text("""
                                                  SELECT column_name,
                                                         data_type,
                                                         udt_name
                                                  FROM information_schema.columns
                                                  WHERE table_schema = :schema
                                                    AND table_name = :table_name
                                                  ORDER BY ordinal_position
                                                  """),
                        {"schema": schema, "table_name": access.table_name}, ).mappings().all())

                    # -------------------------------------------------
                    # Handle non-existent table (Delete Record)
                    # -------------------------------------------------
                    if not col_rows:
                        if check:
                            skipped_count += 1
                            print(
                                f"🗑️  {layer.layer_name}: marked for deletion (table not found: {schema}.{access.table_name})")
                            continue

                        try:
                            session.delete(layer)
                            session.commit()
                            deleted_count += 1
                            print(
                                f"🗑️  {layer.layer_name}: deleted orphan layer record (table not found: {schema}.{access.table_name})\n")
                        except Exception as del_err:
                            session.rollback()
                            failed_count += 1
                            print(f"❌ {layer.layer_name}: failed to delete orphan layer: {del_err}\n")
                        continue

                    pk_rows = (conn.execute(text("""
                                                 SELECT kcu.column_name
                                                 FROM information_schema.table_constraints tc
                                                          JOIN information_schema.key_column_usage kcu
                                                               ON tc.constraint_name = kcu.constraint_name
                                                                   AND tc.table_schema = kcu.table_schema
                                                 WHERE tc.constraint_type = 'PRIMARY KEY'
                                                   AND tc.table_schema = :schema
                                                   AND tc.table_name = :table_name
                                                 ORDER BY kcu.ordinal_position
                                                 """),
                        {"schema": schema, "table_name": access.table_name}, ).mappings().all())

                    geom_col = None
                    columns = []

                    for row in col_rows:
                        col_name = row["column_name"]
                        data_type = str(row["data_type"] or "").lower()
                        udt_name = str(row["udt_name"] or "").lower()

                        if udt_name == "geometry":
                            geom_col = col_name
                            continue

                        columns.append(
                            {"name": col_name, "d_type": ManageDataFrameUtils.getColGenericType(data_type), })

                    pk_cols = [r["column_name"] for r in pk_rows]

                    srid = None
                    geom_type = None
                    extent = None
                    extent_3857 = None
                    row_count = 0

                    if geom_col:
                        safe_geom = (f'"{geom_col.replace(chr(34), chr(34) * 2)}"')
                        safe_schema = schema.replace('"', '""')
                        safe_table_name = access.table_name.replace('"', '""')
                        safe_table = f'"{safe_schema}"."{safe_table_name}"'

                        meta = (conn.execute(text(f"""
                            SELECT 
                                COUNT(*) AS row_count,
                                ST_SRID({safe_geom}) AS srid,
                                GeometryType({safe_geom}) AS geom_type
                            FROM {safe_table}
                            WHERE {safe_geom} IS NOT NULL
                            GROUP BY ST_SRID({safe_geom}), GeometryType({safe_geom})
                            ORDER BY COUNT(*) DESC
                            LIMIT 1
                        """)).mappings().first())

                        if meta:
                            srid = meta["srid"]
                            geom_type = meta["geom_type"]
                            row_count = int(meta["row_count"] or 0)

                        ext = (conn.execute(text(f"""
                            SELECT 
                                ST_XMin(e)::float AS minx,
                                ST_YMin(e)::float AS miny,
                                ST_XMax(e)::float AS maxx,
                                ST_YMax(e)::float AS maxy
                            FROM (
                                SELECT ST_Extent({safe_geom})::geometry AS e
                                FROM {safe_table}
                                WHERE {safe_geom} IS NOT NULL
                            ) AS q
                            WHERE e IS NOT NULL
                        """)).mappings().first())

                        if ext:
                            extent = [ext["minx"], ext["miny"], ext["maxx"], ext["maxy"], ]

                        if extent and srid:
                            ext3857 = (conn.execute(text(f"""
                                SELECT 
                                    ST_XMin(e)::float AS minx,
                                    ST_YMin(e)::float AS miny,
                                    ST_XMax(e)::float AS maxx,
                                    ST_YMax(e)::float AS maxy
                                FROM (
                                    SELECT ST_Extent(
                                        ST_Transform({safe_geom}, 3857)
                                    )::geometry AS e
                                    FROM {safe_table}
                                    WHERE {safe_geom} IS NOT NULL
                                ) AS q
                                WHERE e IS NOT NULL
                            """)).mappings().first())

                            if ext3857:
                                extent_3857 = [ext3857["minx"], ext3857["miny"], ext3857["maxx"], ext3857["maxy"], ]

                old_params = access.params or {}

                old_cols = {c.get("name") for c in old_params.get("columns", []) if c.get("name")}

                new_cols = {c.get("name") for c in columns if c.get("name")}

                added_cols = sorted(new_cols - old_cols)
                removed_cols = sorted(old_cols - new_cols)

                old_geom = old_params.get("g_col")
                old_pk = old_params.get("pk_cols", [])

                old_geom_type = layer.geom_type
                new_geom_type = [geom_type] if geom_type else None

                columns_changed = old_params.get("columns") != columns
                geom_changed = old_geom != geom_col
                pk_changed = old_pk != pk_cols
                srid_changed = layer.srid != srid
                geom_type_changed = old_geom_type != new_geom_type
                extent_changed = layer.extent != extent
                extent_3857_changed = layer.extent_3857 != extent_3857

                changed = any(
                    [columns_changed, geom_changed, pk_changed, srid_changed, geom_type_changed, extent_changed,
                        extent_3857_changed, ])

                checked_count += 1

                status_icon = "🟡" if changed else "✅"

                print(f"{status_icon} {layer.layer_name}")
                print(f"   UUID         : {layer.uuid}")
                print(f"   Table        : {schema}.{access.table_name}")
                print(f"   Connection   : {access.conn_name}")
                print(f"   Columns      : {len(columns)}")
                print(f"   Geometry     : {geom_col}")
                print(f"   PK           : {pk_cols}")
                print(f"   SRID         : {srid}")
                print(f"   Geometry Type: {geom_type}")
                print(f"   Features     : {row_count}")

                if added_cols:
                    print(f"   Added cols   : {added_cols}")

                if removed_cols:
                    print(f"   Removed cols : {removed_cols}")

                if geom_changed:
                    print(f"   Geometry Fix : {old_geom} -> {geom_col}")

                if pk_changed:
                    print(f"   PK Fix       : {old_pk} -> {pk_cols}")

                if srid_changed:
                    print(f"   SRID Fix     : {layer.srid} -> {srid}")

                if geom_type_changed:
                    print(f"   Type Fix     : {old_geom_type} -> {new_geom_type}")

                if extent_changed:
                    print("   Extent       : updated")

                if extent_3857_changed:
                    print("   Extent 3857  : updated")

                if not check and changed:
                    try:
                        new_params = {**old_params, "conn_name": access.conn_name, "table_name": access.table_name,
                            "columns": columns, "g_col": geom_col, "pk_cols": pk_cols, }

                        access.params = new_params
                        flag_modified(access, "params")

                        if srid:
                            layer.srid = int(srid)

                        if geom_type:
                            layer.geom_type = [geom_type]

                        if extent:
                            layer.extent = extent
                            flag_modified(layer, "extent")

                        if extent_3857:
                            layer.extent_3857 = extent_3857
                            flag_modified(layer, "extent_3857")

                        extra = layer.extra_metadata or {}
                        extra["row_count"] = row_count
                        extra["accessibility_refreshed"] = True
                        extra["accessibility_refreshed_at"] = datetime.now(timezone.utc).isoformat()

                        layer.extra_metadata = extra
                        flag_modified(layer, "extra_metadata")

                        session.commit()

                        updated_count += 1
                        total_added_cols += len(added_cols)
                        total_removed_cols += len(removed_cols)

                        if geom_changed:
                            total_geom_fixed += 1

                        if pk_changed:
                            total_pk_fixed += 1

                        if srid_changed:
                            total_srid_updated += 1

                        if extent_changed or extent_3857_changed:
                            total_extent_updated += 1

                        print("   Saved        : yes")

                    except Exception as save_err:
                        session.rollback()
                        failed_count += 1
                        print(f"   Saved        : failed - {save_err}")

                elif check and changed:
                    total_added_cols += len(added_cols)
                    total_removed_cols += len(removed_cols)

                    if geom_changed:
                        total_geom_fixed += 1

                    if pk_changed:
                        total_pk_fixed += 1

                    if srid_changed:
                        total_srid_updated += 1

                    if extent_changed or extent_3857_changed:
                        total_extent_updated += 1

                print("")

            except Exception as e:
                session.rollback()
                failed_count += 1
                print(f"❌ {layer.layer_name}: refresh failed: {e}\n")

        elapsed = time.time() - started_at

        print("=========================================")
        print("Refresh Summary")
        print("=========================================")
        print(f"Checked layers  : {checked_count}")
        print(f"Updated layers  : {updated_count}")
        print(f"Deleted layers  : {deleted_count}")
        print(f"Skipped layers  : {skipped_count}")
        print(f"Failed layers   : {failed_count}")
        print(f"Columns added   : {total_added_cols}")
        print(f"Columns removed : {total_removed_cols}")
        print(f"Geometry fixed  : {total_geom_fixed}")
        print(f"PK fixed        : {total_pk_fixed}")
        print(f"SRID updated    : {total_srid_updated}")
        print(f"Extent updated  : {total_extent_updated}")
        print(f"Completed in    : {elapsed:.2f} sec")

        if check:
            print("\nℹ️ Check mode only. No database changes were saved.")


def sync_layer_accessibility_columns(layer_name: str = None, table_name: str = None):
    """
    Refresh LayerAccessibility.params['columns'] from the real DB table columns.

    Usage:
        python -m app.damapql.manage sync_layer_accessibility_columns --table_name wasa_asset_disposal
        python -m app.damapql.manage sync_layer_accessibility_columns
    """

    with LayerAccessibility.managed_session() as session:
        query = session.query(LayerAccessibility)

        if table_name:
            query = query.filter(LayerAccessibility.params.op("->>")("table_name") == table_name)

        access_list = query.all()

        for access in access_list:
            params = access.params or {}

            if not access.table_name or not access.conn_name:
                continue

            if access.accessibility_type.code != "DB":
                continue

            db_conn = DBConnection.get_db_connection(con_name=access.conn_name)
            if not db_conn:
                print(f"❌ DB connection not found: {access.conn_name}")
                continue

            engine = db_conn.get_sqlalchemy_engine()

            with engine.connect() as conn:
                rows = conn.execute(text("""
                                         SELECT column_name, udt_name, data_type
                                         FROM information_schema.columns
                                         WHERE table_schema = 'public'
                                           AND table_name = :table_name
                                         ORDER BY ordinal_position
                                         """), {"table_name": access.table_name}).mappings().all()

            cols = []
            g_col = None

            for row in rows:
                col_name = row["column_name"]
                data_type = str(row["data_type"]).lower()
                udt_name = str(row["udt_name"]).lower()

                if udt_name == "geometry" or data_type == "USER-DEFINED".lower():
                    g_col = col_name
                    continue

                cols.append({"name": col_name, "d_type": ManageDataFrameUtils.getColGenericType(data_type)})

            params["columns"] = cols
            params["g_col"] = g_col or params.get("g_col")

            access.params = params
            flag_modified(access, "params")

            print(f"✅ Synced {access.table_name}: "
                  f"{len(cols)} columns, geom={params.get('g_col')}")

        session.commit()


# --- AUTH, ROLES, ORGS & DEPTS ---

def createsuperuser():
    """Interactive CLI to create a staff/superuser profile and account."""
    print("\n--- Create Superuser ---")
    email = _prompt_valid_email()

    with DBBase.managed_session(app_label="auth") as session:
        profile = session.query(Profile).filter(Profile.email == email).first()

        if not profile:
            name = input("Full Name: ")
            contact = input("Contact Number: ")
            profile = Profile(name=name, email=email, contact=contact)
            session.add(profile)
            session.flush()

        existing_user = session.query(User).filter(User.profile_id == profile.id).first()
        if existing_user:
            print(f"❌ A user already exists for email '{email}'.")
            return

        username = _prompt_unique_username()
        password = input("Password: ")

        user = User(username=username, password=CryptoUtils().encrypt_txt(password), profile_id=profile.id,
            is_superuser=True, is_staff=True, is_active=True, is_verified=True, )
        session.add(user)
        session.commit()
        session.refresh(user)

        print(f"✅ Superuser '{username}' created successfully with ID={user.id}.")


def create_organization(name: str, description: str = None):
    """Creates an organization."""
    normalized_name = name.strip()

    with DBBase.managed_session(app_label="auth") as session:
        existing = session.query(Organization).filter(Organization.name == normalized_name).first()

        if existing:
            print(f"⚠️ Organization '{name}' already exists.")
            return

        organization = Organization(name=normalized_name, description=description)
        session.add(organization)
        session.commit()
        session.refresh(organization)

        print(f"✅ Organization '{name}' created with ID={organization.id}.")


def create_department(organization_name: str, name: str, description: str = None):
    """Creates an organizational department under an organization."""
    normalized_org = organization_name.strip()
    normalized_dept = name.strip().lower()

    with DBBase.managed_session(app_label="auth") as session:
        organization = session.query(Organization).filter(Organization.name == normalized_org).first()

        if not organization:
            print(f"❌ Organization '{organization_name}' does not exist.")
            return

        existing = session.query(Department).filter(Department.organization_id == organization.id,
                                                    Department.name == normalized_dept).first()

        if existing:
            print(f"⚠️ Department '{name}' already exists in organization '{organization_name}'.")
            return

        department = Department(name=normalized_dept, description=description, organization_id=organization.id, )
        session.add(department)
        session.commit()
        session.refresh(department)

        print(f"✅ Department '{name}' created under '{organization_name}' "
              f"with ID={department.id}.")


def create_role(name: str, description: str = None):
    """Creates a security role."""
    normalized_name = name.strip().lower()

    with DBBase.managed_session(app_label="auth") as session:
        existing = session.query(Role).filter(Role.name == normalized_name).first()
        if existing:
            print(f"⚠️ Role '{name}' already exists.")
            return

        role = Role(name=normalized_name, description=description)
        session.add(role)
        session.commit()
        session.refresh(role)

        print(f"✅ Role '{name}' created with ID={role.id}.")


def create_app_user(role_name: str, organization_name: str = None, department_name: str = None, is_staff: bool = True,
        is_superuser: bool = False, is_active: bool = True, is_verified: bool = True, ):
    """Interactive CLI to create an application user with optional organization and department."""
    print("\n--- Create App User ---")

    normalized_role = role_name.strip().lower()

    with DBBase.managed_session(app_label="auth") as session:
        role = session.query(Role).filter(Role.name == normalized_role).first()
        if not role:
            print(f"❌ Role '{role_name}' does not exist.")
            return

        organization = None
        if organization_name:
            organization = session.query(Organization).filter(Organization.name == organization_name.strip()).first()
            if not organization:
                print(f"❌ Organization '{organization_name}' does not exist.")
                return

        department = None
        if department_name:
            dept_query = session.query(Department).filter(Department.name == department_name.strip().lower())

            if organization:
                dept_query = dept_query.filter(Department.organization_id == organization.id)

            department = dept_query.first()

            if not department:
                if organization_name:
                    print(f"❌ Department '{department_name}' does not exist in organization '{organization_name}'.")
                else:
                    print(f"❌ Department '{department_name}' does not exist.")
                return

            if not organization:
                organization = session.query(Organization).filter(Organization.id == department.organization_id).first()

        email = _prompt_valid_email()
        profile = session.query(Profile).filter(Profile.email == email).first()

        if not profile:
            name = input("Full Name: ")
            contact = input("Contact Number: ")
            profile = Profile(name=name, email=email, contact=contact,
                organization_id=organization.id if organization else None,
                department_id=department.id if department else None, )
            session.add(profile)
            session.flush()
        else:
            existing_user = session.query(User).filter(User.profile_id == profile.id).first()
            if existing_user:
                print(f"❌ A user already exists for email '{email}'.")
                return

            if department and not organization:
                organization = session.query(Organization).filter(Organization.id == department.organization_id).first()

            if organization:
                profile.organization_id = organization.id
            if department:
                profile.department_id = department.id

        username = _prompt_unique_username()
        password = input("Password: ")

        user = User(username=username, password=CryptoUtils().encrypt_txt(password), profile_id=profile.id,
            is_superuser=_to_bool(is_superuser), is_staff=_to_bool(is_staff), is_active=_to_bool(is_active),
            is_verified=_to_bool(is_verified), )
        session.add(user)
        session.flush()

        user.roles.append(role)

        session.commit()
        session.refresh(user)

        print(f"✅ App user '{username}' created successfully "
              f"with role '{role_name}' and ID={user.id}.")


def _prompt_valid_email() -> str:
    while True:
        email = input("Email: ")
        try:
            EmailValidator(email=email)
            return email
        except ValidationError:
            print("❌ Invalid email format.")


def _prompt_unique_username() -> str:
    while True:
        username = input("Username: ")
        if User.is_user_exists(username):
            print("❌ Username taken.")
        else:
            return username


# def get_user_list():
#     with User.managed_session() as session:
#         users = session.query(User.username).all()
#         for user in users:
#             da_logger.info(user.username)

def get_user_password(username: str):
    user = User.get_user(username)
    password = CryptoUtils().decrypt_txt(user.password)
    da_logger.success(f"{username} password is {password}")


# --- SECURITY ---

def generate_crypto_key():
    print(Fernet.generate_key().decode())


def generate_secret_key_hex(length: int = 32):
    print(binascii.hexlify(os.urandom(int(length))).decode())


def encrypt_txt(txt: str, key: str = None):
    """Encrypt a string using CryptoUtils."""
    print(CryptoUtils().encrypt_txt(txt, key))


def decrypt_txt(txt: str, key: str = None):
    """Decrypt an encrypted string using CryptoUtils."""
    print(CryptoUtils().decrypt_txt(txt, key))


def clear_cache():
    """
    Clear all Redis cache entries belonging to the current application.

    Only keys under REDIS_NAMESPACE are removed.

    Examples:
        APP_NAME=wasa  -> deletes wasa:*
        APP_NAME=lbdc  -> deletes lbdc:*
        APP_NAME=pulse -> deletes pulse:*

    Usage:
        python -m app.damapql.manage clear_cache
    """

    if redis_client is None:
        print("❌ Redis client is not configured.")
        return

    namespace = REDIS_NAMESPACE.strip()

    if not namespace:
        raise RuntimeError("REDIS_NAMESPACE is empty. "
                           "Refusing to clear Redis cache.")

    pattern = f"{namespace}:*"

    print("\n========================================")
    print("Redis Application Cache Clear")
    print("========================================")
    print(f"Namespace : {namespace}")
    print(f"Pattern   : {pattern}")
    print("========================================")

    deleted_count = 0
    batch_size = 1000
    batch = []

    try:
        # SCAN is used instead of KEYS so Redis is not blocked.
        for key in redis_client.scan_iter(match=pattern, count=batch_size, ):
            batch.append(key)

            if len(batch) >= batch_size:
                deleted_count += redis_client.delete(*batch)
                batch.clear()

        if batch:
            deleted_count += redis_client.delete(*batch)

        print(f"\n✅ Cleared {deleted_count} Redis key(s) "
              f"from namespace '{namespace}'.")

    except Exception as err:
        da_logger.exception(f"[RedisCacheClearError] "
                            f"namespace={namespace}: {err}")

        print(f"\n❌ Failed to clear Redis cache "
              f"for namespace '{namespace}': {err}")

        raise


# --- SYSTEM ---

def help():
    """Displays all available management commands."""
    print("\n📖 --- DigitalArz Management CLI ---")
    print("Usage: python manage.py <command> [--key value]\n")
    print(f"{'Command':<25} {'Description'}")
    print("-" * 60)
    for name, func in globals().items():
        if callable(func) and not name.startswith("_") and name not in ['main', 'importlib', 'sys']:
            doc = (func.__doc__ or "No description").split('\n')[0]
            print(f"{name:<25} {doc}")
    print("\n")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ['help', '--help', '-h']:
        help()
        return

    command = sys.argv[1]
    params = parse_params(sys.argv[2:])
    cmd_func = globals().get(command)

    if not cmd_func or not callable(cmd_func):
        print(f"❌ Unknown command: {command}")
        return
    try:
        print(f"🛠️  Executing: {command}")
        cmd_func(**params)
    except Exception:
        print("💥 Execution failed:")
        traceback.print_exc()


if __name__ == "__main__":
    main()
