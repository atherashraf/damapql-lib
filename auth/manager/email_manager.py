import os
import smtplib
from typing import Literal, Dict, Any, Tuple

from pydantic import BaseModel, EmailStr
from email.message import EmailMessage


class EmailSchema(BaseModel):
    email: EmailStr
    subject: str
    body: str


# Define supported email types
EmailTypeLiteral = Literal["create_pulse_user", "deactivate_pulse_user", "user_created_with_password"]


class EmailUtils:
    @staticmethod
    def send_email_background(to_email: str, subject: str, body: str):
        print("using port 587 with starttls")
        sender_email = "spatiopulseprojects@gmail.com"
        sender_password = os.getenv("GMAIL_PASS_CODE", "")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = to_email
        msg.set_content(body)

        # with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        #     smtp.login(sender_email, sender_password)
        #     smtp.send_message(msg)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)

    @staticmethod
    def get_email_subject_and_body(
            email_type: EmailTypeLiteral,
            params: Dict[str, Any]
    ) -> Tuple[str, str]:
        subject = None
        body = None
        if email_type == "create_pulse_user":
            subject = "Activate your account"
            body = f"""
                Dear {params['profile_name']},

                    Please click the following link to activate your account and set a password:

                    {params['activation_url']}

                Regards,
                Your Team
                """
        elif email_type == "user_created_with_password":
            subject = "Your account has been created"
            body = f"""
                Dear {params['profile_name']},

                    Your account has been created. Here are your login credentials:

                    Username: {params['username']}
                    Password: {params['password']}

                Please login and change your password.

                Regards,
                Your Team
                """
        if subject is None and body is None:
            raise ValueError(f"Unsupported email type: {email_type}")
        return subject, body
