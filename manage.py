"""
@project DigitalArz FastAPI Engine
@module Management Utilities

To see a list of all available commands and their descriptions:
    $ python manage.py help

COMMAND USAGE GUIDE:

1. Scaffold a new application:
   $ python manage.py startapp --app_label survey_app

2. Initial Database Setup (Create all tables):
   $ python manage.py migrate --app_label auth

3. Update Schema (Add new columns without losing data):
   $ python manage.py sync_schema --app_label auth

4. Create Administrative Users & Access Control:
   $ python manage.py createsuperuser
   $ python manage.py create_organization --name "Digital Arz"
   $ python manage.py create_department --organization_name "Digital Arz" --name "GeoAI development" --description "Core Development Team"
   $ python manage.py create_app_user --role_name manager --organization_name "Digital Arz" --department_name "GeoAI development"
5. Data Portability (JSON):
   $ python manage.py dumpdata --app_label auth --model User --output users_backup.json
   $ python manage.py loaddata --app_label auth --file users_backup.json

6. Security:
   $ python manage.py generate_crypto_key
   $ python manage.py generate_secret_key_hex --length 64
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
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import Integer, Float, String, Boolean, JSON, text, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy_utils import ChoiceType
from cryptography.fernet import Fernet

# Project Imports
from settings import APP_DIR, DA_APPS, DEFAULT_DB_KEY
from utils.adapters.db import DBBase, metadata_obj
from utils.crypto import CryptoUtils
from app.auth.models import Profile, User, Role, Department, Organization
from app.auth.dtypes import EmailValidator

manage_router = APIRouter(
    tags=["manage"],
    responses={404: {"description": "Not found"}},
)

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
    dir_path = os.path.join(APP_DIR, app_label)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    file_names = ["__init__.py", "routers.py", "models.py", "admin.py"]
    for fname in file_names:
        with open(os.path.join(dir_path, fname), "w") as f:
            f.write(f'"""\n{fname} for {app_label} app\n"""\n')
    print(f"🚀 App '{app_label}' created at {dir_path}")

# --- SCHEMA & MIGRATIONS ---

def sync_schema(app_label: str):
    """Adds missing columns to existing tables using ALTER TABLE."""
    ap = DA_APPS.get(app_label)
    if not ap:
        print(f"❌ App label '{app_label}' not found.")
        return

    importlib.import_module(f'{ap["path"]}.models')
    engine = DBBase.get_engine(app_label=app_label)
    inspector = inspect(engine)

    app_tables = [t for name, t in metadata_obj.tables.items() if name.startswith(app_label)]

    for table_obj in app_tables:
        table_name = table_obj.name

        if table_name not in inspector.get_table_names():
            table_obj.create(engine)
            print(f"📦 Created new table: {table_name}")
            continue

        existing_cols = {c['name'] for c in inspector.get_columns(table_name)}

        for column in table_obj.columns:
            if column.name not in existing_cols:
                if engine.dialect.name == 'sqlite' and hasattr(column.type, 'item_type'):
                    print(f"⚠️ Skipping Array column {column.name} - Not supported in SQLite")
                    continue

                # Standard compilation matches the 'migrate' (create_all) behavior
                # Typically varchar(255) for ChoiceType unless 'impl' is set in models.py
                col_type = column.type.compile(engine.dialect)
                null_stmt = "NULL" if column.nullable else "NOT NULL"

                default_val = ""
                if column.server_default:
                    default_val = f"DEFAULT {column.server_default.arg.text}"

                alter_cmd = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type} {null_stmt} {default_val}'

                with engine.begin() as conn:
                    conn.execute(text(alter_cmd))
                print(f"✨ Added column [{column.name}] (Type: {type(column.type).__name__}) to {table_name}")

    print(f"✅ Schema sync for '{app_label}' complete.")

def migrate(app_label: str):
    """Runs standard SQLAlchemy create_all."""
    ap = DA_APPS.get(app_label)
    if not ap: return

    importlib.import_module(f'{ap["path"]}.models')
    engine = DBBase.get_engine(app_label=app_label)
    metadata_obj.create_all(engine)
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
    """Loads records from JSON and resets Postgres SERIAL sequences."""
    file_path = Path(file).resolve()
    if not file_path.exists():
        print(f"❌ File {file_path} not found.")
        return

    with open(file_path, "r") as f:
        records = json.load(f)

    ap = DA_APPS.get(app_label)
    module = importlib.import_module(f'{ap["path"]}.models')
    touched_models = set()

    with DBBase.managed_session(app_label=app_label) as session:
        for entry in records:
            model_class = getattr(module, entry["model"])
            touched_models.add(model_class)
            pk_name = model_class.__mapper__.primary_key[0].name

            obj = model_class(**entry["fields"])
            setattr(obj, pk_name, entry["pk"])
            session.merge(obj)

        session.commit()
        reset_pk_sequences(DBBase.get_engine(app_label=app_label), session, touched_models)
    print(f"📥 Loaded {len(records)} records from {file}")

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

    print(f"✅ Superuser '{username}' created successfully.")

def create_organization(name: str, description: str = None):
    """Creates an organization."""
    normalized_name = name.strip()

    with DBBase.managed_session(app_label="auth") as session:
        existing = session.query(Organization).filter(Organization.name == normalized_name).first()
        if existing:
            print(f"⚠️ Organization '{name}' already exists.")
            return

        session.add(Organization(name=normalized_name, description=description))

    print(f"✅ Organization '{name}' created.")


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

        session.add(
            Department(
                name=normalized_dept,
                description=description,
                organization_id=organization.id,
            )
        )

    print(f"✅ Department '{name}' created under '{organization_name}'.")


def create_role(name: str, description: str = None):
    """Creates a security role."""
    normalized_name = name.strip().lower()

    with DBBase.managed_session(app_label="auth") as session:
        existing = session.query(Role).filter(Role.name == normalized_name).first()
        if existing:
            print(f"⚠️ Role '{name}' already exists.")
            return

        session.add(Role(name=normalized_name, description=description))

    print(f"✅ Role '{name}' created.")


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

    print(
        f"✅ App user '{username}' created successfully "
        f"with role '{role_name}'."
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