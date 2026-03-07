from fastapi import APIRouter, Depends, HTTPException

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
from app.dch.utils.email_utils import EmailRequest, send_email

dch_export_router = APIRouter(tags=["dch"])


def require_export_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


@dch_export_router.post("/send-email/")
async def dch_send_email(
    email_data: EmailRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Sends an email.
    Protected endpoint.
    """
    require_export_manager(current_user)

    try:
        send_email(email_data.recipient, email_data.subject, email_data.body)
        return {"message": f"Email successfully sent to {email_data.recipient}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")