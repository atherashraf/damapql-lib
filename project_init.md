
---

# 🚀 Project Initialization Guide

Follow these steps to set up your environment, database schema, organizations, departments, roles, and initial administrative users.

## 1️⃣ Environment Configuration (`.env`)

Create a `.env` file in your project root and fill in your database credentials and placeholders for the keys.

```ini
# Database Configuration
DCH_HOST=localhost
DCH_PORT=5432
DCH_USER=postgres
DCH_PASSWORD=your_password
DCH_NAME=wasa_dch
DCH_ENGINE=postgresql

# Security Keys (to be generated in Step 2)
SECRET_KEY=
CRYPTO_KEY=
JWT_SECRET_KEY=
JWT_REFRESH_SECRET_KEY=

# Email Configuration
GMAIL_PASS_CODE=your_app_specific_password
````

---

## 2️⃣ Generate Security Keys

Use `manage.py` to generate the required cryptographic and JWT keys, then copy the output values into your `.env` file.

### Generate `CRYPTO_KEY`

```bash
python manage.py generate_crypto_key
```

### Generate `SECRET_KEY`, `JWT_SECRET_KEY`, and `JWT_REFRESH_SECRET_KEY`

Run this command three times to generate three different values:

```bash
python manage.py generate_secret_key_hex --length 32
```

> Note: if your `generate_secret_key_hex()` uses `os.urandom(length)`, then `--length 32` produces a 64-character hex string.

---

## 3️⃣ Database Schema Setup

Initialize the tables for the authentication system and the DCH app.

```bash
# Create core auth tables
python manage.py migrate --app_label auth

# Create DCH tables
python manage.py migrate --app_label dch
python manage.py loaddata --app_label dch  --file /app/dch/fixtures/layercategory.json
python manage.py loaddata --app_label dch  --file /app/dch/fixtures/layerqualitylevel.json
                      
```

---

## 4️⃣ Create the Superuser

The superuser is required to manage the system and create initial users.

```bash
python manage.py createsuperuser
```

Follow the prompts for:

* Email
* Full Name
* Contact Number
* Username
* Password

---

## 5️⃣ Configure Access Control (Mandatory)

Before creating standard users, create the organization, department, and roles.

### Create the Organization

```bash
python manage.py create_organization --name "Digital Arz"
```

### Create the Department

```bash
python manage.py create_department --organization_name "Digital Arz" --name "GeoAI development" --description "Core Development Team"
```

### Create the App Manager Role

```bash
python manage.py create_role --name app_manager --description "Full access to manage DCH layers and DB connections"
```

---

## 6️⃣ Create an App User

Create a staff user and assign the required role.

```bash
python manage.py create_app_user --role_name app_manager --organization_name "Digital Arz" --department_name "GeoAI development"
```

Follow the prompts for:

* Email
* Full Name
* Contact Number
* Username
* Password

---

## 7️⃣ Optional Additional Roles

You can create more roles as needed.

```bash
python manage.py create_role --name data_editor --description "Can edit DCH data"
python manage.py create_role --name reviewer --description "Can review submitted records"
```

---

## 8️⃣ Data Portability

You can export and import model records in JSON format.

### Dump data

```bash
python manage.py dumpdata --app_label auth --model User --output users_backup.json
```

### Load data

```bash
python manage.py loaddata --app_label auth --file users_backup.json
```

---

# 📈 Scaling & Application Management Guide

Once the core engine is initialized, use the following workflow to add new modules, features, or data integrations.

## 1️⃣ Scaffold a New App

Use `startapp` to generate a standardized app structure.

```bash
python manage.py startapp --app_label inventory
```

Files created:

* `models.py`
* `routers.py`
* `admin.py`
* `__init__.py`

---

## 2️⃣ Register the App in `settings.py`

Add the new app to `DA_APPS`.

```python
DA_APPS = {
    "auth": {"path": "app.auth", "db_key": "default"},
    "dch": {"path": "app.dch", "db_key": "default"},
    "inventory": {"path": "app.inventory", "db_key": "default"},
    "migration": {"path": "app.migration", "db_key": "legacy_db"},
}
```

---

## 3️⃣ Implement Models

In `app/<app_label>/models.py`, inherit from `DBBase`.

### Example model

```python
from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_utils import ChoiceType

from utils.adapters.db import DBBase


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

Your project supports two different schema workflows.

### A. Initial table creation

Use `migrate` the first time you create the app tables.

```bash
python manage.py migrate --app_label inventory
```

### B. Add new columns to existing tables

Use `sync_schema` when you add new columns later.

```bash
python manage.py sync_schema --app_label inventory
```

> `sync_schema` is intended for adding missing columns. It is not a full migration framework for renames, drops, or complex schema refactors.

---

## 5️⃣ Multi-Database Configuration

If an app uses a DB key other than `default`, define that database in `settings.py`.

```python
DATABASES = {
    "default": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST"),
        "name": os.environ.get("DCH_NAME"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD"),
        "port": os.environ.get("DCH_PORT"),
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

## 🛠 Scaling Checklist

1. Scaffold the app:

   ```bash
   python manage.py startapp --app_label <name>
   ```

2. Register it in `DA_APPS`.

3. Define models in `app/<name>/models.py`.

4. Apply schema changes:

   * `migrate` for first creation
   * `sync_schema` for adding new columns

5. Add API routes in `routers.py` and include them in `main.py`.

---

## 🛠 Quick Reference

| Command                                                                                                        | Usage                                        |
| -------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| `python manage.py help`                                                                                        | Show all available management commands       |
| `python manage.py migrate --app_label <app>`                                                                   | Create tables for a new app                  |
| `python manage.py sync_schema --app_label <app>`                                                               | Add newly defined columns to existing tables |
| `python manage.py create_organization --name "<org>"`                                                          | Create an organization                       |
| `python manage.py create_department --organization_name "<org>" --name "<dept>"`                               | Create a department under an organization    |
| `python manage.py create_role --name "<role>"`                                                                 | Create a security role                       |
| `python manage.py create_app_user --role_name "<role>" --organization_name "<org>" --department_name "<dept>"` | Create an application user                   |

---


