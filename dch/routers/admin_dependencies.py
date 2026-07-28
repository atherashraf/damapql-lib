from fastapi import HTTPException, status

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils


def require_dch_admin(current_user: UserInfo) -> None:
    """
    Allow DCH administration to:

    - superusers
    - users having the app_manager role
    """
    if getattr(current_user, "is_superuser", False):
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Superuser or app_manager privileges required",
    )