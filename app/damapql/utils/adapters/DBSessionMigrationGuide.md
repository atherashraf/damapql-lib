
---
## 🚀 Internal Migration Guide: Database Sessions

We are moving away from manual session management to a **Context Managed** approach. This prevents connection leaks and ensures transaction safety (automatic rollbacks on errors).

### ❌ The Legacy Way (Deprecated)

**Problem:** Requires manual `try/except` and `session.close()`. High risk of leaking connections if a developer forgets to close the session or if the code crashes before `.close()`.



```python
class MyModel:
    pass
# AVOID THIS
db = MyModel.get_session()
try:
    user = db.query(User).filter_by(id=1).first()
    user.name = "Gemini"
    db.commit()
finally:
    db.close()  # If you forget this, the connection hangs!

```

### ✅ The New Way (Preferred)

**Benefit:** The session is automatically closed and rolled back on failure. It respects the dynamic database routing (multi-db) automatically.

```python
# USE THIS
from my_app.models import MyModel

with MyModel.managed_session() as db:
    user = db.query(User).filter_by(id=1).first()
    user.name = "Gemini"
    db.commit() 
# No .close() needed. It's handled for you!

```

---

### 🛠 Quick Reference: Search & Replace

If you are refactoring existing code, here is the mental map for the change:

| Old Pattern | New Pattern |
| --- | --- |
| `db = Model.get_session()` | `with Model.managed_session() as db:` |
| `db.close()` | *(Remove this line)* |
| `db.rollback()` | *(Handled automatically on Exception)* |

### 💡 Advanced Usage: Passing Sessions

If you are inside a function that already has an active session (like a FastAPI route), `managed_session` is smart enough to just "pass through" that existing session without closing it prematurely:

```python
def update_profile(user_id: int, db_session: Session):
    # This will NOT close db_session because it was passed in.
    with MyModel.managed_session(session=db_session) as db:
        user = db.get(User, user_id)
        ...

```

---

### Final "Clean" Version of your Mixin Methods

Make sure your `ModelHelpersMixin` has these finalized signatures to avoid the `TypeError` bugs we identified:

```python
    @classmethod
    @contextmanager
    def managed_session(cls, session: Session = None, app_label: str = None) -> Generator[Session, None, None]:
        db_key = cls.get_db_key(app_label)
        if session:
            yield session
        else:
            with sync_session_scope(db_key) as s:
                yield s

    def delete(self, session: Union[Session, AsyncSession] = None, app_label=None):
        with self.managed_session(session, app_label) as s:
            if isinstance(s, AsyncSession):
                return self._delete_async(self, s) # 'self' is the instance to delete
            s.delete(self)
            s.commit()

    @staticmethod
    async def _delete_async(obj: Any, session: AsyncSession):
        await session.delete(obj)
        await session.commit()

```

**Would you like me to generate a script that scans your project files and lists exactly which files are still using the old `get_session()` call?**