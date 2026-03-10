"""
Base Service Layer (base_service.py)
-----------------------------------
A generic CRUD service layer bridging FastAPI routers and SQLAlchemy models.

USAGE:
1. Inherit for specific models:
    class UserService(BaseService[User]):
        ...
    user_service = UserService(User)

2. Use it in Router
    # app/users/routers.py
    from fastapi import APIRouter, Depends, HTTPException
    from utils.adapters.deps import get_db
    from .services import user_service

    router = APIRouter()

    @router.post("/")
    def create_user(data: dict, db = Depends(get_db)):
        # This will automatically return 409 Conflict if user exists
        # because of your Mixin + Exception Handler in main.py
        return user_service.create(db, data, allow_update=False)

    @router.get("/{user_id}")
    def read_user(user_id: int, db = Depends(get_db)):
        user = user_service.get_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user.to_object()

3. Create with Conflict Protection:
    user_service.create(db, data, allow_update=False) # Raises 409 if exists
"""

from typing import Type, TypeVar, List, Generic, Optional, Any, Union
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

T = TypeVar("T")

class BaseService(Generic[T]):
    def __init__(self, model: Type[T]):
        """
        Initializes the service with the given SQLAlchemy model.
        """
        self.model = model

    def get_all(self, db: Session) -> List[T]:
        """Fetch all records from the database."""
        return db.query(self.model).all()

    async def get_all_async(self, db: AsyncSession) -> List[T]:
        """Fetch all records asynchronously (FastAPI friendly)."""
        result = await db.execute(select(self.model))
        return result.scalars().all()

    def get_by_id(self, db: Session, id: Any) -> Optional[T]:
        """Fetch a single record by its Primary Key."""
        return db.get(self.model, id)

    def create(self, db: Session, obj_in: Union[dict, Any], allow_update: bool = False) -> T:
        """
        Creates a new record.
        If allow_update=False and record exists (via unique_fields or PK),
        raises ValueError (Caught as 409 Conflict in main.py).
        """
        obj = self.model(**obj_in) if isinstance(obj_in, dict) else obj_in
        return obj.save(session=db, allow_update=allow_update)

    def update(self, db: Session, db_obj: T, obj_in: dict) -> T:
        """
        Updates an existing database object with new data.
        Explicitly calls .save(allow_update=True).
        """
        for field, value in obj_in.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        return db_obj.save(session=db, allow_update=True)

    def remove(self, db: Session, id: Any) -> bool:
        """
        Deletes a record by its Primary Key.
        Returns True if successful, False if record not found.
        """
        obj = db.get(self.model, id)
        if obj:
            obj.delete(session=db)
            return True
        return False