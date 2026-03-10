
# DAMapQL

Reusable **DAMap Query Layer** for **FastAPI applications**.

This repository provides shared modules used across multiple DAMap-based projects.
It is extracted from the main **DAMapQL development repository** and maintained as a reusable library.

---

# Installation

Clone the library inside the **`app` directory** of your FastAPI project.

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

# Module Overview

Current modules included in DAMapQL:

```
damapql
├── auth     → authentication and user management
├── dch      → data catalog / map services
└── tests
```

These modules are reusable across multiple FastAPI applications.

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
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER") == "True"

localhost = 'host.docker.internal' if IS_DOCKER else 'localhost'

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

* `dch` and `data` databases **may be the same or separate** depending on the project architecture.
* The key names **must match exactly**:

```
DEFAULT_DB_KEY = 'dch'
DATA_DB_KEY = 'data'
```

These keys are used internally by DAMapQL modules to resolve database connections.

# Running Tests

DAMapQL includes unit tests written using **pytest**.

Tests are located inside:

```
app/damapql/tests
```

Project structure:

```
app/damapql/tests
├── auth
│   └── test_*.py
└── dch
    └── test_*.py
```

### Run Authentication Tests

```bash
pytest -v app/damapql/tests/auth
```

### Run DCH Tests

```bash
pytest -v app/damapql/tests/dch
```

### Run All Tests

```bash
pytest -v app/damapql/tests
```

The `-v` flag runs pytest in **verbose mode**, showing each test result.

---


---

# Development Workflow

This repository is maintained from the main development project.

To publish updates from the main **DAMapQL** repository to this library:

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

To get the latest updates in a project that uses this library:

```bash
cd app/damapql
git pull
```

---

# Author

**Ather Ashraf**
AI • GIS • Software Engineering

📧 [atherashraf@gmail.com](mailto:atherashraf@gmail.com)
📱 +92 322 4785104
📱 +966 55 725 2342

---
