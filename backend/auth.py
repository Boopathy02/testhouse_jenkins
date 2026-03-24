import os
from datetime import datetime, timedelta
from jose import jwt

# IMPORTANT: This is a secret key for signing the JWTs.
# You should replace "your-secret-key" with a strong, randomly generated secret
# and ideally load it from an environment variable or a secure vault.
SECRET_KEY = "your-secret-key"
ALGORITHM = "HS256"
_DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 120
try:
    ACCESS_TOKEN_EXPIRE_MINUTES = max(
        1,
        int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES") or _DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES),
    )
except ValueError:
    ACCESS_TOKEN_EXPIRE_MINUTES = _DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
