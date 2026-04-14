"""
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

Add Layer Info
    python -m app.damapql.manage create_layerinfo_from_app_models --app_label wasa
"""
import os
import sys
import json
import binascii
import traceback
import importlib
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional

from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import Integer, Float, String, Boolean, JSON, text, inspect, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy_utils import ChoiceType
from sqlalchemy.engine import URL
from sqlalchemy.orm.attributes import flag_modified

from cryptography.fernet import Fernet

from app.damapql.dch.models import DBConnection, LayerInfo, LayerCategory, LayerQualityLevel
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.utils.adapters.db import DBBase, metadata_obj
# Project Imports

from app.damapql.utils.crypto import CryptoUtils
from app.damapql.auth.models import Profile, User, Role, Department, Organization
from app.damapql.auth.dtypes import EmailValidator
from app.damapql.settings import DCH_DATABASES
from app.damapql.utils.helper_scripts.table_schema_update import TableSchemaUpdater
from settings import DA_APPS, BASE_DIR, DATABASES

manage_router = APIRouter(
    tags=["manage"],
    responses={404: {"description": "Not found"}},
)


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

        admin_url = URL.create(
            drivername="postgresql+psycopg2",
            username=db_user,
            password=db_password,
            host=host,
            port=port,
            database="postgres",
        )

        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

        try:
            with admin_engine.connect() as conn:

                # Check if database exists
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"),
                    {"name": db_name}
                ).scalar()

                if exists:
                    print(f"⏭️  Database '{db_name}' already exists — skipping.")
                    continue

                # Create database
                conn.execute(
                    text(f'CREATE DATABASE "{db_name}" OWNER "{db_user}"')
                )

                print(f"✅ Database '{db_name}' created with owner '{db_user}'")

        except Exception as e:
            print(f"❌ Failed creating '{db_name}': {e}")
            continue

        # Connect to new DB to install PostGIS
        db_url = URL.create(
            drivername="postgresql+psycopg2",
            username=db_user,
            password=db_password,
            host=host,
            port=port,
            database=db_name,
        )

        engine = create_engine(db_url, isolation_level="AUTOCOMMIT")

        try:
            with engine.connect() as conn:

                extensions = [
                    "postgis",
                    "postgis_topology",
                    "postgis_raster"
                ]

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

def sync_schema(app_label: str, rename_map: dict | None = None):
    """
    rename_map format:
    {
        "table_name": {
            "old_col_name": "new_col_name"
        }
    }
    """
    rename_map = rename_map or {}

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
    da_logger.info(engine)

    updater = TableSchemaUpdater(engine=engine, logger=da_logger)

    app_tables = [t for _, t in BaseCls.metadata.tables.items()]
    da_logger.info(f"table_names {app_tables}")

    # for table_obj in app_tables:
    #     table_rename_map = rename_map.get(table_obj.name, {})
    #     updater.sync_table(table_obj, rename_map=table_rename_map)
    existing_table_names = updater.get_table_names()
    for table_obj in app_tables:
        table_name = table_obj.name

        # 1. Check if the table needs to be created from scratch
        if table_name not in existing_table_names:
            updater.create_table(table_obj)
            continue  # Move to the next table, nothing to sync yet

        # 2. If table exists, perform the synchronization
        table_rename_map = rename_map.get(table_name, {})
        updater.sync_table(table_obj, rename_map=table_rename_map)

    print(f"✅ Schema sync for '{app_label}' complete.")


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

            seq = session.execute(
                text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": table_name, "col": pk_name}
            ).scalar()

            if not seq:
                skipped.append((table_name, f"no backing sequence for '{pk_name}'"))
                continue

            max_id = session.execute(
                text(f'SELECT COALESCE(MAX("{pk_name}"), 0) FROM "{table_name}"')
            ).scalar() or 0

            # set sequence so next nextval() becomes max_id + 1
            session.execute(
                text("SELECT setval(:seq, :val, :is_called)"),
                {"seq": seq, "val": max_id, "is_called": True}
            )

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

def dumpdata(app_label: str, model: str, output: str = "data.json"):
    """Dumps model records to a JSON file, resolving ChoiceTypes."""
    ap = DA_APPS.get(app_label)
    module = importlib.import_module(f'{ap["path"]}.models')
    model_class = getattr(module, model)

    with DBBase.managed_session(app_label=app_label) as session:
        results = session.query(model_class).all()
        data = []
        pk_name = model_class.__mapper__.primary_key[0].name

        for obj in results:
            fields = {col.name: to_python_scalar(getattr(obj, col.name))
                      for col in model_class.__table__.columns if col.name != pk_name}
            data.append({"model": model, "pk": getattr(obj, pk_name), "fields": fields})

    with open(output, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"📤 Dumped {len(data)} records to {output}")


def loaddata(app_label: str, file: str):
    """
    Load records from a JSON fixture file and reset PostgreSQL SERIAL sequences.

    The file path may be either:
    - an absolute path
    - a relative path (resolved against DCH_BASE_DIR)

    Example:
        python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layercategory.json
    """

    file_path = Path(file)

    # Resolve relative paths against DCH_BASE_DIR
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
    except Exception as e:
        print(f"❌ Failed to read JSON file: {file_path}")
        raise e

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
                session.merge(obj)

            session.commit()

            reset_pk_sequences(
                DBBase.get_engine(app_label=app_label),
                session,
                touched_models
            )

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
        app_label: str,
        layer_category_id=None,
        overwrite: bool = True,
        include_non_spatial: bool = False,
):
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

        result = service.save_app_layer_infos(
            db=session,
            app_label=app_label,
            layer_category_id=layer_category_id,
            quality_level_id=quality.id,
            user=None,
            overwrite=overwrite,
            include_non_spatial=include_non_spatial,
        )

        session.commit()

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


def create_layerinfo_from_raster(
        title: str,
        raster_path: str,
        layer_category_id=None,
        temporal: str = None,
        data_model_type: str = None,
        overwrite: bool = False,
        app_label: str = "dch",
        layer_name: str = None,
):
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

        obj, changed = service.save_raster_layer_info(
            db=session,
            title=title,
            raster_path=raster_path,
            layer_category_id=layer_category_id,
            temporal=temporal,
            data_model_type=data_model_type,
            user=None,
            overwrite=overwrite,
            app_label=app_label,
            layer_name=layer_name,
        )

        if getattr(obj, "quality_level_id", None) is None:
            obj.quality_level_id = quality.id

        session.commit()

    print(
        {
            "msg": "Raster layer info created/updated" if changed else "Raster layer already exists",
            "uuid": getattr(obj, "uuid", None),
            "layer_name": getattr(obj, "layer_name", None),
            "title": getattr(obj, "title", None),
            "path": (
                obj.extra_metadata.get("cog_path")
                if getattr(obj, "extra_metadata", None)
                else raster_path
            ),
        }
    )
    return obj


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

        user = User(
            username=username,
            password=CryptoUtils().encrypt_txt(password),
            profile_id=profile.id,
            is_superuser=True,
            is_staff=True,
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        print(f"✅ Superuser '{username}' created successfully with ID={user.id}.")


def create_organization(name: str, description: str = None):
    """Creates an organization."""
    normalized_name = name.strip()

    with DBBase.managed_session(app_label="auth") as session:
        existing = session.query(Organization).filter(
            Organization.name == normalized_name
        ).first()

        if existing:
            print(f"⚠️ Organization '{name}' already exists.")
            return

        organization = Organization(
            name=normalized_name,
            description=description
        )
        session.add(organization)
        session.commit()
        session.refresh(organization)

        print(f"✅ Organization '{name}' created with ID={organization.id}.")


def create_department(organization_name: str, name: str, description: str = None):
    """Creates an organizational department under an organization."""
    normalized_org = organization_name.strip()
    normalized_dept = name.strip().lower()

    with DBBase.managed_session(app_label="auth") as session:
        organization = session.query(Organization).filter(
            Organization.name == normalized_org
        ).first()

        if not organization:
            print(f"❌ Organization '{organization_name}' does not exist.")
            return

        existing = session.query(Department).filter(
            Department.organization_id == organization.id,
            Department.name == normalized_dept
        ).first()

        if existing:
            print(
                f"⚠️ Department '{name}' already exists in organization '{organization_name}'."
            )
            return

        department = Department(
            name=normalized_dept,
            description=description,
            organization_id=organization.id,
        )
        session.add(department)
        session.commit()
        session.refresh(department)

        print(
            f"✅ Department '{name}' created under '{organization_name}' "
            f"with ID={department.id}."
        )


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


def create_app_user(
        role_name: str,
        organization_name: str = None,
        department_name: str = None,
        is_staff: bool = True,
        is_superuser: bool = False,
        is_active: bool = True,
        is_verified: bool = True,
):
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
            organization = session.query(Organization).filter(
                Organization.name == organization_name.strip()
            ).first()
            if not organization:
                print(f"❌ Organization '{organization_name}' does not exist.")
                return

        department = None
        if department_name:
            dept_query = session.query(Department).filter(
                Department.name == department_name.strip().lower()
            )

            if organization:
                dept_query = dept_query.filter(
                    Department.organization_id == organization.id
                )

            department = dept_query.first()

            if not department:
                if organization_name:
                    print(
                        f"❌ Department '{department_name}' does not exist in organization '{organization_name}'."
                    )
                else:
                    print(f"❌ Department '{department_name}' does not exist.")
                return

            if not organization:
                organization = session.query(Organization).filter(
                    Organization.id == department.organization_id
                ).first()

        email = _prompt_valid_email()
        profile = session.query(Profile).filter(Profile.email == email).first()

        if not profile:
            name = input("Full Name: ")
            contact = input("Contact Number: ")
            profile = Profile(
                name=name,
                email=email,
                contact=contact,
                organization_id=organization.id if organization else None,
                department_id=department.id if department else None,
            )
            session.add(profile)
            session.flush()
        else:
            existing_user = session.query(User).filter(User.profile_id == profile.id).first()
            if existing_user:
                print(f"❌ A user already exists for email '{email}'.")
                return

            if department and not organization:
                organization = session.query(Organization).filter(
                    Organization.id == department.organization_id
                ).first()

            if organization:
                profile.organization_id = organization.id
            if department:
                profile.department_id = department.id

        username = _prompt_unique_username()
        password = input("Password: ")

        user = User(
            username=username,
            password=CryptoUtils().encrypt_txt(password),
            profile_id=profile.id,
            is_superuser=_to_bool(is_superuser),
            is_staff=_to_bool(is_staff),
            is_active=_to_bool(is_active),
            is_verified=_to_bool(is_verified),
        )
        session.add(user)
        session.flush()

        user.roles.append(role)

        session.commit()
        session.refresh(user)

        print(
            f"✅ App user '{username}' created successfully "
            f"with role '{role_name}' and ID={user.id}."
        )


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


# --- SECURITY ---

def generate_crypto_key():
    print(Fernet.generate_key().decode())


def generate_secret_key_hex(length: int = 32):
    print(binascii.hexlify(os.urandom(int(length))).decode())


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
