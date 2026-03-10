# SMTP Configuration (Replace with actual credentials)
import os
import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pydantic import BaseModel, EmailStr

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "your_email@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "your_app_password")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USERNAME)


class EmailRequest(BaseModel):
    recipient: EmailStr
    subject: str
    body: str


def send_email(recipient: str, subject: str, body: str):
    """Function to send email using SMTP."""
    try:
        # Set up the email
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = recipient
        msg["Subject"] = subject
        # msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(body, "html"))  # Ensure HTML content type

        # # Establish connection with SMTP Server
        # server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        # server.ehlo()  # Identify to the SMTP server
        # server.starttls()  # Upgrade the connection to secure TLS
        # server.ehlo()  # Re-identify after starttls()
        #
        # # Login and send email
        # server.login(SMTP_USERNAME, SMTP_PASSWORD)
        # server.sendmail(SENDER_EMAIL, recipient, msg.as_string())
        # server.quit()
        # Use SMTP_SSL for direct SSL connection
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient, msg.as_string())



    except Exception as e:
        traceback.print_exc()
        # raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")
        raise Exception(f"failed to send email: {str(e)}")
