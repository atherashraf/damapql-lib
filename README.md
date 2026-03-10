# DAMapQL

Reusable **DAMap Query Layer** for FastAPI applications.
This library contains shared modules used across DAMap-based projects.

It is designed to be placed inside the **`app/` directory** of a FastAPI project so that imports remain consistent.

---

# Installation

Navigate to the **`app` directory** of your FastAPI project and clone the repository as `damapql`.

```bash
cd app
git clone https://github.com/atherashraf/damapql-lib.git damapql
```

Your project structure should look like this:

```
project-root
│
├── app
│   ├── main.py
│   ├── api
│   ├── models
│   └── damapql
│       ├── __init__.py
│       ├── services
│       ├── models
│       └── utils
```

---

# Usage

Once cloned inside `app/`, the modules can be imported like this:

```python
from app.damapql import services
```

Example:

```python
from app.damapql.services.mouza_service import MouzaService
```

---

# Updating the Library

To pull the latest updates:

```bash
cd app/damapql
git pull
```

---

# Purpose

`damapql` provides reusable components such as:

* GIS data services
* Query layer utilities
* shared data models
* reusable API helpers
* domain logic for DAMap-based applications

This allows multiple FastAPI projects to share the same codebase.

---

# Development Source

The primary development repository is:

```
DAMapQL
```

The `damapql-lib` repository is automatically synced from the `app/damapql` folder of the main project.

---

# Author

**Ather Ashraf**
AI / GIS / Software Engineering

