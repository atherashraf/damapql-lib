import binascii
import importlib
import json
import sys
import os
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from cryptography.fernet import Fernet
from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import Integer, Float, String, Boolean, JSON, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.auth.dtypes import EmailValidator, RoleEnum
from app.auth.models import Profile, User
from digitalarzengine.io.managers.db_manager import DBManager
from utils.adapters.db import DBBase, metadata_obj

from utils.adapters.model_utils import ModelUtils
from utils.crypto import CryptoUtils
from settings import APP_DIR, DA_APPS

# FastAPI router placeholder (not used in CLI context, but kept for completeness)
manage_router = APIRouter(
    tags=["manage"],
    responses={404: {"description": "Not found"}},
)


# ---------------------
# Command Implementations
# ---------------------


def to_python_scalar(v):
    if isinstance(v, pd.Series):
        return to_python_scalar(v.iloc[0]) if not v.empty else None
    if isinstance(v, (pd.Timestamp,)):
        return v.to_pydatetime()
    if isinstance(v, np.generic):   # numpy scalar -> python scalar
        return v.item()
    return v

def startapp(app_label: str):
    """Creates a new FastAPI app structure under APP_DIR."""
    dir_path = os.path.join(APP_DIR, app_label)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)
    file_names = ["__init__.py", "routers.py", "models.py", "admin.py"]
    for fname in file_names:
        with open(os.path.join(dir_path, fname), "w") as f:
            f.write(f"# {fname} for {app_label} app")
    print(f"{app_label} app created at {dir_path}")


def createsuperuser():
    """Interactively creates a superuser."""
    print("Enter your details to create a superuser:")

    # Step 1: Get and check email
    while True:
        email_input = input("Email: ")
        try:
            validated = EmailValidator(email=email_input)
            email = validated.email  # ✅ this is the actual string you want
            break
        except ValidationError:
            print("Invalid email. Please enter a valid email address.")
    profile = Profile.get_by_email(email)
    already_exist = False

    if profile:
        profile_id = profile.id  # ✅ Correct
        already_exist = True
    else:
        name = input("Name: ")
        contact = input("Contact: ")
        profile = Profile(name=name, email=email, contact=contact)
        saved_profile = profile.save()  # returns a Profile object now
        profile_id = saved_profile.id

    # Step 2: Check if a user already exists for the profile
    if already_exist:
        user = User.get_by_profile_id(profile_id)
        if user is not None:
            print("A user already exists for this profile.")
            return

    # Step 3: Loop until an available username is given
    while True:
        username = input("Username: ")
        if User.is_user_exists(username):
            print("Username already exists. Please choose another.")
        else:
            break
    print("profile_id", profile_id)
    # Step 4: Create the superuser
    password = input("Password: ")
    # user = User(
    #     username=username,
    #     password=CryptoUtils().encrypt_txt(password),  # hash in production
    #     profile_id=profile_id,
    #     is_superuser=True,
    #     is_staff=True
    # )
    # after computing profile_id:
    profile_id = to_python_scalar(profile_id)
    if profile_id is None:
        raise ValueError("Could not resolve a valid profile_id")

    # when building the User:
    user = User(
        username=username,
        password=CryptoUtils().encrypt_txt(password),  # see #2 below
        profile_id=int(profile_id),  # ensure plain int
        is_superuser=True,
        is_staff=True,
        # create_app_user only:
        # roles=[user_role]  # see #3 below
    )

    user_id = user.save()
    print(f"Superuser '{username}' created with ID {user_id}")


def create_app_user(user_role: RoleEnum):
    """Interactively creates a superuser."""
    print("Enter your details to create a superuser:")

    # Step 1: Get and check email
    while True:
        email_input = input("Email: ")
        try:
            validated = EmailValidator(email=email_input)
            email = validated.email  # ✅ this is the actual string you want
            break
        except ValidationError:
            print("Invalid email. Please enter a valid email address.")
    profile = Profile.get_by_email(email)
    already_exist = False

    if profile:
        profile_id = profile.id  # ✅ Correct
        already_exist = True
    else:
        name = input("Name: ")
        contact = input("Contact: ")
        profile = Profile(name=name, email=email, contact=contact)
        saved_profile = profile.save()  # returns a Profile object now
        profile_id = saved_profile.id

    # Step 2: Check if a user already exists for the profile
    if already_exist:
        user = User.get_by_profile_id(profile_id)
        if user is not None:
            print("A user already exists for this profile.")
            return

    # Step 3: Loop until an available username is given
    while True:
        username = input("Username: ")
        if User.is_user_exists(username):
            print("Username already exists. Please choose another.")
        else:
            break
    print("profile_id", profile_id)
    # Step 4: Create the superuser
    password = input("Password: ")
    # user = User(
    #     username=username,
    #     password=CryptoUtils().encrypt_txt(password),  # hash in production
    #     profile_id=profile_id,
    #     is_superuser=True,
    #     is_staff=True,
    #     roles=[user_role]
    # )
    # after computing profile_id:
    profile_id = to_python_scalar(profile_id)
    if profile_id is None:
        raise ValueError("Could not resolve a valid profile_id")

    # when building the User:
    user = User(
        username=username,
        password=CryptoUtils().encrypt_txt(password),  # see #2 below
        profile_id=int(profile_id),  # ensure plain int
        is_superuser=True,
        is_staff=True,
        # create_app_user only:
        # roles=[user_role]  # see #3 below
    )

    user_id = user.save()
    print(f"App USer '{username}' created with ID {user_id}")

def change_password():
    """Interactively changes the password of a user."""
    username = input("Username: ")
    new_password = input("New password: ")
    confirm_password = input("Confirm password: ")

    if User.change_password(username, new_password, confirm_password):
        print("Password changed successfully.")
    else:
        print("Password change failed. Please check your credentials.")


def generate_crypto_key():
    """Generates a Fernet key."""
    key = Fernet.generate_key()
    print(key.decode("utf-8"))


def generate_secret_key_bytes(length: int):
    """
    Generates a cryptographically strong random byte string.
    :param length_bytes: The desired length of the key in bytes.
    """
    val = os.urandom(int(length))
    print(val)


def generate_secret_key_hex(length: int):  # Added type hint for clarity
    """
    Generates a cryptographically strong random secret key as a hex string.
    This is often preferred for readability and easy handling in config files.
    :param length_bytes: The desired length of the key in bytes (must be an integer).
    """
    # Ensure 'length_bytes' is an integer before passing to os.urandom
    val = binascii.hexlify(os.urandom(int(length))).decode('utf-8')
    print(val)


def sqlalchemy_column_to_python_type(col):
    if isinstance(col.type, Integer):
        return "int"
    elif isinstance(col.type, Float):
        return "float"
    elif isinstance(col.type, String):
        return "str"
    elif isinstance(col.type, Boolean):
        return "bool"
    elif isinstance(col.type, (JSON, JSONB)):
        return "dict"
    else:
        return "Any"


def generate_pydantic_type(model_name: str = None):
    """
    Dynamically generate a Pydantic model from a SQLAlchemy model.
    """
    model_class = ModelUtils.get_model(model_name)
    model_name = model_class.__name__ + "Type"
    print(f"class {model_name}(BaseModel):")

    if not hasattr(model_class, "__table__"):
        print("    pass\n")
        return

    for col in model_class.__table__.columns:
        name = col.name
        typ = sqlalchemy_column_to_python_type(col)
        print(f"    {name}: {typ}")

    print("    model_config = ConfigDict(from_attributes=True)\n")


# def inspectdb(table_name: str, database: str):
#     """
#     Inspects the structure of a table in the given database.
#     """
#     params = DBBase.get_db_params(database)
#     manager = GeoDBManager(params)
#     manager.inspect_table(table_name)

def migrate(app_label: str, database: str = None):
    """
    Performs database migration by creating tables defined in the model.
    """
    ap = DA_APPS[app_label]

    # Get DB connection parameters
    if database is not None:
        db_param = DBBase.get_db_params(database)
    elif "db_key" in ap:
        db_param = DBBase.get_db_params(ap["db_key"])
    else:
        raise ValueError(f"No database specified and no 'db_key' found for {app_label}")

    db_manager = DBManager(db_param)

    # Load the models module dynamically
    module = importlib.import_module(f'{ap["path"]}.models')

    # Get list of table names defined via __tablename__
    table_names = [
        getattr(getattr(module, attr), "__tablename__")
        for attr in vars(module)
        if hasattr(getattr(module, attr), "__tablename__")
    ]

    # # Filter only those metadata tables that match the model's table names
    tables = [
        table for key, table in metadata_obj.tables.items()
        if key.split(".")[-1] in table_names
    ]

    # Create tables in the database
    metadata_obj.create_all(db_manager.get_engine(), tables=tables)
    print("Migration completed.")
    return {"payload": "Model Migration Completed"}

def get_model_class(app_label: str, model_name: str):
    ap = DA_APPS[app_label]
    module = importlib.import_module(f'{ap["path"]}.models')
    return getattr(module, model_name)

def dumpdata(app_label: str, model: str, output: str = "data.json", database: str = None):
    """Dump database table into JSON fixture like Django."""
    model_class = get_model_class(app_label, model)

    # connect db
    if database:
        db_param = DBBase.get_db_params(database)
    else:
        db_param = DBBase.get_db_params(DA_APPS[app_label]["db_key"])
    db_manager = DBManager(db_param)
    engine = db_manager.get_engine()
    session = Session(engine)

    results = session.query(model_class).all()
    data = []
    pk_name = model_class.__mapper__.primary_key[0].name

    for obj in results:
        fields = {}
        for col in model_class.__table__.columns:
            if col.name != pk_name:
                val = getattr(obj, col.name)
                fields[col.name] = to_python_scalar(val)

        data.append({
            "model": model,
            "pk": getattr(obj, pk_name),
            "fields": fields
        })

    with open(output, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"✅ Dumped {len(data)} records from {model} to {output}")


def reset_pk_sequences(engine, session, touched_models):
    """
    Ensure the next autoincrement value is >= max(pk)+1 for each touched table.
    Supports PostgreSQL (serial/identity) and MySQL/MariaDB. No-op for SQLite.
    """
    dialect = engine.dialect.name
    seen_tables = set()

    for model_class in touched_models:
        tbl = model_class.__table__
        table_name = tbl.name
        schema = tbl.schema  # may be None
        if table_name in seen_tables:
            continue
        seen_tables.add(table_name)

        # single-column integer PK only
        pk_cols = list(tbl.primary_key.columns)
        if len(pk_cols) != 1:
            continue
        pk_col = pk_cols[0]
        pk_name = pk_col.name

        # Fully-qualified table for raw SQL
        if schema:
            fq_table = f'"{schema}"."{table_name}"' if dialect == "postgresql" else f"`{schema}`.`{table_name}`"
        else:
            fq_table = f'"{table_name}"' if dialect == "postgresql" else (f"`{table_name}`" if dialect in ("mysql","mariadb") else table_name)

        # Get current max id
        max_id = session.execute(text(f"SELECT COALESCE(MAX({pk_name}), 0) FROM {fq_table}")).scalar_one()

        if dialect == "postgresql":
            # Try to find the backing sequence (works for SERIAL); falls back to IDENTITY.
            seq = session.execute(
                text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": f"{schema+'.' if schema else ''}{table_name}", "col": pk_name},
            ).scalar()

            if seq:
                # setval(seq, max_id) makes nextval() return max_id+1
                session.execute(text("SELECT setval(:seq, :v)"), {"seq": seq, "v": max_id})
            else:
                # Identity column: restart with max_id+1
                session.execute(
                    text(f'ALTER TABLE {fq_table} ALTER COLUMN "{pk_name}" RESTART WITH :v'),
                    {"v": int(max_id) + 1 if max_id else 1},
                )

        elif dialect in ("mysql", "mariadb"):
            session.execute(
                text(f"ALTER TABLE {fq_table} AUTO_INCREMENT = :v"),
                {"v": int(max_id) + 1 if max_id else 1},
            )

        else:
            # SQLite or other backends: usually nothing to do
            pass

    session.commit()

def loaddata(app_label: str, file: str, database: str = None):
    """Load fixture JSON into database."""
    file = Path(__file__).parent.joinpath(file).resolve()
    print(f"Loading data from {file}")
    if not os.path.exists(file):
        raise FileNotFoundError(f"{file} not found")

    # connect db
    if database:
        db_param = DBBase.get_db_params(database)
    else:
        db_param = DBBase.get_db_params(DA_APPS[app_label]["db_key"])
    db_manager = DBManager(db_param)
    engine = db_manager.get_engine()
    session = Session(engine)

    with open(file, "r") as f:
        records = json.load(f)

    touched_models = set()

    for entry in records:
        model_class = get_model_class(app_label, entry["model"])
        touched_models.add(model_class)
        pk_name = model_class.__mapper__.primary_key[0].name
        obj = model_class(**entry["fields"])
        setattr(obj, pk_name, entry["pk"])
        session.merge(obj)

    session.commit()

    reset_pk_sequences(engine, session, touched_models)
    print(f"✅ Loaded {len(records)} records from {file}")


# ---------------------
# Main Command Dispatcher
# ---------------------

def parse_params(argv):
    """
    Parses CLI args into a dictionary like:
    --app_label auth --database default → {'app_label': 'auth', 'database': 'default'}
    """
    params = {}
    key = None
    for val in argv:
        if key:
            params[key] = val
            key = None
        elif val.startswith("--"):
            key = val[2:]
    return params


def main():
    if len(sys.argv) < 2:
        print("Usage: python manage.py <command> [--key value ...]")
        sys.exit(1)

    command = sys.argv[1]
    params = parse_params(sys.argv[2:])

    # Get function from globals
    func = globals().get(command)
    if not callable(func):
        print(f"Unknown command: {command}")
        sys.exit(1)

    try:
        print(f"Executing command: {command}")
        print(f"With parameters: {params}")
        func(**params)
    except Exception as e:
        print("An error occurred while executing the command:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
