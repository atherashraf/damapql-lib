
---

# 🚀 Project Initialization Guide

Follow these steps to set up your environment, database schema, and initial administrative users.

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

# Security Keys (To be generated in Step 2)
SECRET_KEY=
CRYPTO_KEY=
JWT_SECRET_KEY=
JWT_REFRESH_SECRET_KEY=

# Email Configuration
GMAIL_PASS_CODE=your_app_specific_password

```

---

## 2️⃣ Generate Security Keys

Use your `manage.py` utility to generate the required cryptographic and JWT keys. Copy the output of these commands into your `.env` file.

### For `CRYPTO_KEY`:

```bash
python manage.py generate_crypto_key

```

### For `SECRET_KEY`, `JWT_SECRET_KEY`, and `JWT_REFRESH_SECRET_KEY`:

Run this command three times to get unique hex strings:

```bash
python manage.py generate_secret_key_hex --length 32

```

---

## 3️⃣ Database Schema Setup

Initialize the tables for the authentication system and the data clearinghouse (DCH) app.

```bash
# Create Core Auth Tables (Profiles, Users, Roles)
python manage.py migrate --app_label auth

# Create DCH Tables (LayerInfo, Connections)
python manage.py migrate --app_label dch

```

---

## 4️⃣ Create the Superuser

The Superuser is required to manage the system via the CLI and Admin UI.

```bash
python manage.py createsuperuser

```

* **Follow the prompts** for Email, Name, Username, and Password.

---

## 5️⃣ Configure Access Control (Mandatory)

Define the roles and departments before creating standard users.

### Create the App Manager Role

```bash
python manage.py create_role --name app_manager --description "Full access to manage DCH layers and DB connections"

```

### Create the Department

```bash
python manage.py create_department --name engineering --description "Core Development and GIS Team"

```

---

## 6️⃣ Create an App User with Role

Finally, create a staff user and assign them the `app_manager` role.

```bash
python manage.py create_app_user --role_name app_manager --department_name engineering

```


---

# 📈 Scaling & Application Management Guide

Once your core engine is initialized, use this workflow to add new features, modules, or external data integrations.

## 1️⃣ Scaffolding a New App

Use the `startapp` command to generate a standardized directory structure. This ensures consistency across your entire project.

```bash
python manage.py startapp --app_label inventory

```

**Files Created:**

* `models.py`: Database schema definitions.
* `routers.py`: FastAPI endpoint definitions.
* `admin.py`: Metadata for administrative interfaces.
* `__init__.py`: Package marker.

---

## 2️⃣ App Registration & DB Routing

Open `settings.py` and register your new app in the `DA_APPS` dictionary. This is the **Control Center** for your project's architecture.

```python
DA_APPS = {
    "auth": {"path": "app.auth", "db_key": "default"},
    "dch": {"path": "app.dch", "db_key": "default"},
    # New App targeting the primary DB
    "inventory": {"path": "app.inventory", "db_key": "default"},
    # External App targeting a different legacy DB
    "migration": {"path": "app.migration", "db_key": "legacy_db"},
}

```

---

## 3️⃣ Implementing Models

In your new `app/<app_label>/models.py`, inherit from `DBBase`. This grants your models automatic access to `.save()`, `.to_gdf()`, and `.managed_session()`.

### **Standard Model Template:**

```python
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, Boolean, DateTime, func
from utils.adapters.db import DBBase
from sqlalchemy_utils import ChoiceType

class InventoryItem(DBBase):
    __tablename__ = "inventory_item" # Tip: Keep it lowercase

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    
    # Example using ChoiceType for status
    status: Mapped[str] = mapped_column(
        ChoiceType({"active": "Active", "archived": "Archived"}), 
        default="active"
    )
    
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

```

---

## 4️⃣ Applying Changes (Migrate vs Sync)

Your project handles database changes in two distinct ways:

### **A. Initial Creation (`migrate`)**

Use this for the very first time you create an app's tables.

```bash
python manage.py migrate --app_label inventory

```

### **B. Schema Updates (`sync_schema`)**

Use this when you add a **new column** to an existing model. It performs an `ALTER TABLE` without wiping your existing data.

```bash
python manage.py sync_schema --app_label inventory

```

---

## 5️⃣ Advanced Multi-DB Configuration

If you set a `db_key` other than `"default"`, ensure the credentials exist in your `DATABASES` dictionary in `settings.py`.

```python
DATABASES = {
    "default": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST"),
        "name": "wasa_dch",
        # ...
    },
    "legacy_db": {
        "engine": "postgresql",
        "host": "192.168.1.50",
        "name": "old_system_db",
        "user": "readonly_user",
        "password": "password_here",
        "port": 5432
    }
}

```

---

## 🛠 Scaling Checklist

1. **Scaffold**: `python manage.py startapp --app_label <name>`
2. **Register**: Add to `DA_APPS` in `settings.py`.
3. **Define**: Write models in `app/<name>/models.py`.
4. **Deploy**: Run `migrate` (new) or `sync_schema` (updates).
5. **Route**: Create FastAPI endpoints in `routers.py` and include them in `main.py`.


---

## 🛠 Quick Reference

| Command | Usage |
| --- | --- |
| `sync_schema` | Use this if you add new columns to `models.py` later. |
| `help` | Use `python manage.py help` to see all available commands. |

