from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.damapql.auth.models import User, Role


class UserRoleManager:

    @staticmethod
    def add_role(session: Session, username: str, role_name: str) -> User:
        role_name = role_name.strip().lower()

        user = (
            session.query(User)
            .options(selectinload(User.roles))
            .filter_by(username=username)
            .first()
        )

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        role = session.query(Role).filter_by(name=role_name).first()

        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")

        if role in user.roles:
            raise HTTPException(status_code=400, detail="Role already assigned")

        user.roles.append(role)
        user.save(session=session)

        return user

    @staticmethod
    def remove_role(session: Session, username: str, role_name: str) -> User:
        role_name = role_name.strip().lower()

        user = (
            session.query(User)
            .options(selectinload(User.roles))
            .filter_by(username=username)
            .first()
        )

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        role = session.query(Role).filter_by(name=role_name).first()

        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")

        if role not in user.roles:
            raise HTTPException(status_code=400, detail="Role not assigned")

        user.roles.remove(role)
        user.save(session=session)

        return user

    @staticmethod
    def set_roles(session: Session, user: User, roles: List[str]) -> User:
        """
        Replace all roles of a user with the given roles.
        """
        if not roles:
            user.roles = []
            return user.save(session=session)

        normalized_roles = [r.strip().lower() for r in roles]

        role_objects = (
            session.query(Role)
            .filter(Role.name.in_(normalized_roles))
            .all()
        )

        found_role_names = {r.name for r in role_objects}
        missing_roles = [r for r in normalized_roles if r not in found_role_names]

        if missing_roles:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid roles: {', '.join(missing_roles)}"
            )

        user.roles = role_objects
        return user.save(session=session)