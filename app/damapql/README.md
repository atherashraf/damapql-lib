
# DAMapQL

Reusable **DAMap Query Layer** for FastAPI applications.

This repository is maintained as a reusable library extracted from the main **DAMapQL** development repository.

---

# Installation

Clone the library inside the `app` directory of your FastAPI project.

```bash
cd app
git clone https://github.com/atherashraf/damapql-lib.git damapql
```

Your project structure should look like:

```
project-root
│
├── app
│   ├── main.py
│   ├── api
│   └── damapql
│       ├── auth
│       ├── dch
│       └── ...
```

Then modules can be imported normally:

```python
from app.damapql.auth import models
from app.damapql.dch import routers
```

---

# Required Settings Configuration

The host application must define the following settings.

```python
DEFAULT_DB_KEY = 'dch'
DATA_DB_KEY = 'data'

DA_APPS = {
    "auth": {"path": "app.damapql.auth", "db_key": DEFAULT_DB_KEY},
    "dch": {"path": "app.damapql.dch", "db_key": DEFAULT_DB_KEY},
}
```

---

# Database Configuration

Example configuration:

```python
DATABASES = {

    "cache_db": {
        "engine": "sqlite",
        "file_path": os.path.join(BASE_DIR, "dch_cache.sqlite")
    },

    "dch": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST") if not IS_LOCAL else localhost,
        "name": os.environ.get("DCH_NAME"),
        "port": os.environ.get("DCH_PORT"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    },

    "data": {
        "engine": "postgresql",
        "host": os.environ.get("DATA_HOST") if not IS_LOCAL else localhost,
        "name": os.environ.get("DATA_NAME"),
        "port": os.environ.get("DATA_PORT"),
        "user": os.environ.get("DATA_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    }

}
```

### Notes

* `dch` and `data` databases **may be the same or separate** depending on the project.
* The key names **must match**:

```
DEFAULT_DB_KEY = 'dch'
DATA_DB_KEY = 'data'
```

These keys are used internally by DAMapQL modules to resolve database connections.

---

# Development Workflow

This repository is automatically maintained from the main development project.

To push updates from the main repository (`DAMapQL`) to this library:

```bash
git subtree push --prefix=app/damapql damapql-lib main
```

This command publishes the contents of:

```
app/damapql
```

into this repository.

---

# Updating the Library in a Project

To get updates in a project that uses this library:

```bash
cd app/damapql
git pull
```

---

# Author

**Ather Ashraf**
AI • GIS • Software Engineering
atherashraf@gmail.com
+923224785104/+966557252342

