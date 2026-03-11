
# Environment Configuration

The application requires several environment variables for database connectivity, security, email communication, and optional cloud storage integration.

These variables should be defined in your `.env` file or deployment environment.

---

# 1. Database Configuration

## DCH Database (Primary Database)

Configure the main **DCH database connection** using the following parameters.

```bash
DCH_HOST=
DCH_PORT=
DCH_USER=
DCH_PASSWORD=
DCH_NAME=
DCH_ENGINE=
```

### Example

```bash
DCH_HOST=localhost
DCH_PORT=5432
DCH_USER=postgres
DCH_PASSWORD=your_password
DCH_NAME=dch_database
DCH_ENGINE=postgresql
```

---

## Optional DATA Database

The application can optionally connect to an additional **DATA database**.
If this database is not required, the system can operate using the **DCH database only**.

```bash
DATA_HOST=
DATA_PORT=
DATA_USER=
DATA_PASSWORD=
DATA_NAME=
```

---

# 2. Security Keys

The system requires several cryptographic keys for **application security, encryption, and JWT authentication**.

```bash
SECRET_KEY=
CRYPTO_KEY=
JWT_SECRET_KEY=
JWT_REFRESH_SECRET_KEY=
```

### Generating Keys

You can generate secure keys using the following management commands:

```bash
# Generate the required security keys using the following commands.
# Each command will output a value that must be copied and added to the .env file.

python manage.py generate_secret_key_hex --length 24      # use output as SECRET_KEY
python manage.py generate_crypto_key                      # use output as CRYPTO_KEY
python manage.py generate_secret_key_hex --length 32      # use output as JWT_SECRET_KEY
python manage.py generate_secret_key_hex --length 32      # use output as JWT_REFRESH_SECRET_KEY
```

These commands will produce secure random keys suitable for production use.

---

# 3. Email Communication

To enable email functionality (notifications, OTPs, password resets, etc.), configure the Gmail application password:

```bash
GMAIL_PASS_CODE=
```

⚠️ Use a **Gmail App Password**, not your personal Gmail password.

---

# 4. AWS Configuration (Optional – S3 Storage)

If your deployment requires **AWS S3 for file storage**, configure the following parameters:

```bash
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION_NAME=
```

### Example

```bash
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION_NAME=us-east-1
```

---

# Notes

* Store sensitive values securely in environment variables.
* Never commit `.env` files containing secrets to version control.
* Use different keys and credentials for **development**, **staging**, and **production** environments.

