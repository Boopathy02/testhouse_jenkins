# ============================================================
# Base image (Python 3.11 – stable, PyYAML-safe)
# ============================================================
FROM python:3.11-slim-bookworm

# -------------------------
# Environment
# -------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# ============================================================
# System dependencies
# ============================================================
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    ca-certificates \
    unzip \
    gnupg \
    \
    openjdk-17-jdk \
    \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libffi-dev \
    libgl1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libx11-xcb1 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    fonts-liberation \
    \
    tesseract-ocr \
    \
    xvfb \
    x11vnc \
    fluxbox \
    novnc \
    websockify \
    \
    postgresql-client \
    \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# Java environment
# ============================================================
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"

# ============================================================
# Allure CLI
# ============================================================
RUN set -eux; \
    ALLURE_VERSION=2.36.0; \
    curl -fSL \
      "https://repo.maven.apache.org/maven2/io/qameta/allure/allure-commandline/${ALLURE_VERSION}/allure-commandline-${ALLURE_VERSION}.zip" \
      -o /tmp/allure.zip; \
    unzip /tmp/allure.zip -d /opt; \
    ln -sf /opt/allure-${ALLURE_VERSION}/bin/allure /usr/bin/allure; \
    rm -f /tmp/allure.zip; \
    allure --version

# ============================================================
# Python dependencies
# ============================================================
COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir uvicorn "playwright==1.52.0" && \
    python -m playwright install --with-deps chromium

# ============================================================
# Application source
# ============================================================
COPY . .

# ============================================================
# Entrypoint
# ============================================================
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fix noVNC root path
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# ============================================================
# Network
# ============================================================
EXPOSE 8001 6080 5900

# ============================================================
# Entrypoint
# ============================================================
ENTRYPOINT ["/entrypoint.sh"]

# ============================================================
# Startup
# ============================================================
CMD bash -c "\
  Xvfb :99 -screen 0 1920x1080x24 & \
  sleep 2 && \
  fluxbox & \
  sleep 2 && \
  x11vnc -display :99 -nopw -forever -shared \
         -listen 127.0.0.1 -rfbport 5900 & \
  sleep 2 && \
  websockify --web=/usr/share/novnc/ 6080 127.0.0.1:5900 & \
  python -m uvicorn main:app --host 0.0.0.0 --port 8001"