
---

# 🚀 Project Initialization Guide

This guide explains how to initialize a new project using **DAMapQL**, including environment setup, schema creation, fixtures, organizations, departments, roles, and administrative users.

All management commands should be run using:

```bash
python -m app.damapql.manage <command>
```

For example:

```bash
python -m app.damapql.manage help
```

---

## 1️⃣ Environment Configuration (`.env`)

Create a `.env` file in your project root and define the required database and security settings.

```ini
# -----------------------------
# Database Configuration
# -----------------------------
DCH_HOST=localhost
DCH_PORT=5432
DCH_USER=postgres
DCH_PASSWORD=your_password
DCH_NAME=wasa_dch
DCH_ENGINE=postgresql

DATA_HOST=localhost
DATA_PORT=5432
DATA_USER=postgres
DATA_PASSWORD=your_password
DATA_NAME=wasa_data
DATA_ENGINE=postgresql

# -----------------------------
# Security Keys
# -----------------------------
SECRET_KEY=
CRYPTO_KEY=
JWT_SECRET_KEY=
JWT_REFRESH_SECRET_KEY=

# -----------------------------
# Email Configuration
# -----------------------------
GMAIL_PASS_CODE=your_app_specific_password
```

If your project uses only one PostgreSQL database for both `dch` and `data`, both keys may point to the same database instance.

---

## 2️⃣ Generate Security Keys

Use the DAMapQL management module to generate the required keys.

### Generate `CRYPTO_KEY`

```bash
python -m app.damapql.manage generate_crypto_key
```

### Generate `SECRET_KEY`

```bash
python -m app.damapql.manage generate_secret_key_hex --length 32
```

### Generate `JWT_SECRET_KEY`

```bash
python -m app.damapql.manage generate_secret_key_hex --length 32
```

### Generate `JWT_REFRESH_SECRET_KEY`

```bash
python -m app.damapql.manage generate_secret_key_hex --length 32
```

Copy the generated values into your `.env` file.

> `generate_secret_key_hex --length 32` produces a 64-character hex value.

---

## 3️⃣ Database Schema Setup

Initialize the tables for the DAMapQL applications.

### Create authentication tables

```bash
python -m app.damapql.manage migrate --app_label auth
```

### Create DCH tables

```bash
python -m app.damapql.manage migrate --app_label dch
```

### Load DCH fixture data

```bash
python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layercategory.json
python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layerqualitylevel.json
```

---

## 4️⃣ Create the Superuser

Create the first administrative user for the system.

```bash
python -m app.damapql.manage createsuperuser
```

You will be prompted for:

* Email
* Full Name
* Contact Number
* Username
* Password

This user will have:

* `is_superuser = True`
* `is_staff = True`
* full access to the SQLAdmin panel and protected operations

---

## 5️⃣ Configure Core Access Control

Before creating regular application users, define the base organizational structure.

### Create organization

```bash
python -m app.damapql.manage create_organization --name "Digital Arz"
```

### Create department

```bash
python -m app.damapql.manage create_department --organization_name "Digital Arz" --name "GeoAI development" --description "Core Development Team"
```

### Create role

```bash
python -m app.damapql.manage create_role --name app_manager --description "Full access to manage DCH layers and DB connections"
```

---

## 6️⃣ Create an Application User

Create a standard application user and assign the appropriate role.

```bash
python -m app.damapql.manage create_app_user --role_name app_manager --organization_name "Digital Arz" --department_name "GeoAI development"
```

You will be prompted for:

* Email
* Full Name
* Contact Number
* Username
* Password

---

## 7️⃣ Optional Additional Roles

You can add more roles depending on project needs.

```bash
python -m app.damapql.manage create_role --name data_editor --description "Can edit DCH data"
python -m app.damapql.manage create_role --name reviewer --description "Can review submitted records"
python -m app.damapql.manage create_role --name publisher --description "Can publish approved content"
```

---

## 8️⃣ Data Portability

DAMapQL supports JSON-based data export and import.

### Dump records to JSON

```bash
python -m app.damapql.manage dumpdata --app_label auth --model User --output users_backup.json
```

### Load records from JSON

```bash
python -m app.damapql.manage loaddata --app_label auth --file users_backup.json
```

---

# 📈 Scaling & Application Management Guide

Once the base project is initialized, you can extend it with additional apps and modules.

---

## 1️⃣ Scaffold a New App

Use `startapp` to create a new application module.

```bash
python -m app.damapql.manage startapp --app_label inventory
```

This creates:

* `__init__.py`
* `routers.py`
* `models.py`
* `admin.py`

under your configured `APP_DIR`.

---

## 2️⃣ Register the App in `settings.py`

Add the new app to `DA_APPS`.

Example:

```python
DA_APPS = {
    "auth": {"path": "app.damapql.auth", "db_key": "dch"},
    "dch": {"path": "app.damapql.dch", "db_key": "dch"},
    "inventory": {"path": "app.inventory", "db_key": "dch"},
    "migration": {"path": "app.migration", "db_key": "legacy_db"},
}
```

> The `db_key` defines which database connection the app should use.

---

## 3️⃣ Implement Models

In your new app’s `models.py`, inherit from `DBBase`.

Example:

```python
from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_utils import ChoiceType

from app.damapql.utils.adapters.db import DBBase


class InventoryItem(DBBase):
    __tablename__ = "inventory_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    status: Mapped[str] = mapped_column(
        ChoiceType({"active": "Active", "archived": "Archived"}),
        default="active"
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

---

## 4️⃣ Apply Schema Changes

DAMapQL supports two schema workflows.

### Initial table creation

Use `migrate` when creating tables for the first time.

```bash
python -m app.damapql.manage migrate --app_label inventory
```

### Add missing columns to existing tables

Use `sync_schema` after adding new columns to existing models.

```bash
python -m app.damapql.manage sync_schema --app_label inventory
```

> `sync_schema` is designed for additive schema updates only.
> It is not a replacement for a full migration tool for renames, type changes, or dropped columns.

---

## 5️⃣ Multi-Database Configuration

If an app uses another database key, define it in `settings.py`.

Example:

```python
import os

DATABASES = {
    "dch": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST"),
        "name": os.environ.get("DCH_NAME"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD"),
        "port": os.environ.get("DCH_PORT"),
    },
    "data": {
        "engine": "postgresql",
        "host": os.environ.get("DATA_HOST"),
        "name": os.environ.get("DATA_NAME"),
        "user": os.environ.get("DATA_USER"),
        "password": os.environ.get("DATA_PASSWORD"),
        "port": os.environ.get("DATA_PORT"),
    },
    "legacy_db": {
        "engine": "postgresql",
        "host": "192.168.1.50",
        "name": "old_system_db",
        "user": "readonly_user",
        "password": "password_here",
        "port": 5432,
    },
}
```

---

# 🛠 Recommended Initialization Order

For a fresh project, use this order:

```bash
python -m app.damapql.manage migrate --app_label auth
python -m app.damapql.manage migrate --app_label dch
python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layercategory.json
python -m app.damapql.manage loaddata --app_label dch --file app/damapql/dch/fixtures/layerqualitylevel.json
python -m app.damapql.manage createsuperuser
python -m app.damapql.manage create_organization --name "Digital Arz"
python -m app.damapql.manage create_department --organization_name "Digital Arz" --name "GeoAI development" --description "Core Development Team"
python -m app.damapql.manage create_role --name app_manager --description "Full access to manage DCH layers and DB connections"
python -m app.damapql.manage create_app_user --role_name app_manager --organization_name "Digital Arz" --department_name "GeoAI development"
```

---

# 🛠 Quick Reference

| Command                                                                                                                    | Purpose                                |
| -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `python -m app.damapql.manage help`                                                                                        | Show all available commands            |
| `python -m app.damapql.manage migrate --app_label <app>`                                                                   | Create tables for an app               |
| `python -m app.damapql.manage sync_schema --app_label <app>`                                                               | Add missing columns to existing tables |
| `python -m app.damapql.manage loaddata --app_label <app> --file <file>`                                                    | Import fixture/data JSON               |
| `python -m app.damapql.manage dumpdata --app_label <app> --model <model> --output <file>`                                  | Export model data to JSON              |
| `python -m app.damapql.manage create_organization --name "<org>"`                                                          | Create organization                    |
| `python -m app.damapql.manage create_department --organization_name "<org>" --name "<dept>"`                               | Create department                      |
| `python -m app.damapql.manage create_role --name "<role>"`                                                                 | Create role                            |
| `python -m app.damapql.manage create_app_user --role_name "<role>" --organization_name "<org>" --department_name "<dept>"` | Create standard app user               |
| `python -m app.damapql.manage createsuperuser`                                                                             | Create admin/superuser                 |
| `python -m app.damapql.manage generate_crypto_key`                                                                         | Generate CRYPTO_KEY                    |
| `python -m app.damapql.manage generate_secret_key_hex --length 32`                                                         | Generate a hex secret                  |

---

# ✅ Notes

* Use `migrate` for first-time table creation.
* Use `sync_schema` only for adding newly defined columns.
* `loaddata` also resets PostgreSQL sequences after import.
* `dch` and `data` may point to the same database or separate databases depending on project design.
* `DA_APPS` controls app registration and database routing.

---

\
