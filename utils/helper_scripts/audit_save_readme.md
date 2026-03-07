
### `audit_save_calls.py`

This script identifies every time `.save()` is called and flags whether `unique_fields` are being passed.

```python
import os
import re
from pathlib import Path

# Configuration
PROJECT_ROOT = "."
# Matches .save(...) calls and captures the contents of the parentheses
SAVE_PATTERN = r"\.save\((.*?)\)"
IGNORE_DIRS = {".git", "__pycache__", "venv", "env"}

def audit_save_methods():
    print(f"{'FILE':<40} | {'LINE':<5} | {'STATUS':<15} | {'CALL'}")
    print("-" * 110)
    
    audit_count = 0
    for path in Path(PROJECT_ROOT).rglob("*.py"):
        if any(ignored in path.parts for ignored in IGNORE_DIRS) or "mixin" in path.name.lower():
            continue

        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                match = re.search(SAVE_PATTERN, line)
                if match:
                    audit_count += 1
                    args_content = match.group(1)
                    
                    # Logic check: is unique_fields present?
                    if "unique_fields" in args_content:
                        status = "✅ UPSERT"
                    elif "session=" in args_content and len(args_content.split(',')) == 1:
                        status = "ℹ️ SESSION ONLY"
                    elif not args_content.strip():
                        status = "⚠️ RAW SAVE"
                    else:
                        status = "❓ CHECK ARGS"
                    
                    print(f"{str(path)[-40:]:<40} | {i:<5} | {status:<15} | {line.strip()}")

    print("-" * 110)
    print(f"Total .save() calls audited: {audit_count}")

if __name__ == "__main__":
    audit_save_methods()

```

---

### Why this audit matters for your new Mixin

Now that your `ModelHelpersMixin.save()` is "database-aware," you should check for these three scenarios:

#### 1. The "Dangerous" Raw Save

```python
obj.save() 

```

**Risk:** If `obj` has an ID that already exists in the DB, but you didn't pass `unique_fields`, SQLAlchemy might attempt a plain `INSERT` and throw a `Primary Key Violation`.
**Fix:** If you want it to update existing records, use `obj.save(unique_fields=['id'])`.

#### 2. The Multi-DB Routing

```python
obj.save(app_label='inventory')

```

**Verification:** Ensure that `inventory` is defined in your `DA_APPS` in `settings.py`. If it's missing, your Mixin will fall back to `DEFAULT_DB_KEY`.

#### 3. Atomic Transactions

```python
with MyModel.managed_session() as db:
    obj1.save(session=db)
    obj2.save(session=db)
    db.commit()

```

**Verification:** This is the gold standard. The audit script will mark this as `SESSION ONLY`. It ensures both saves happen in the same transaction.

---

### Pro-Tip: Global Unique Fields

If a model **always** uses the same unique fields for upserts (e.g., a `User` always uses `email`), you can modify your Mixin to look for a default list on the model:

```python
# In your Mixin's save method:
if not unique_fields and hasattr(self._Meta, "unique_fields"):
    unique_fields = self._Meta.unique_fields

```

This prevents you from having to pass `unique_fields=['email']` every single time you call `.save()`.

