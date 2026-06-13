FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99

# Playwright system deps + Xvfb (virtual display for mss screenshots) + scrot fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb scrot curl ca-certificates ffmpeg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libx11-6 libxext6 libxrender1 libxtst6 \
    libpango-1.0-0 libpangocairo-1.0-0 fonts-liberation libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app/backend

# Install Python deps first — layer cached until pyproject.toml or uv.lock changes
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --no-install-project --frozen

# Playwright Chromium (--with-deps installs any remaining system packages).
# browser-use drives this same Chromium.
RUN uv run playwright install chromium --with-deps

# Application code
COPY backend/agent/ ./agent/
COPY backend/server.py ./

EXPOSE 8000

# Xvfb gives mss a virtual display for full-screen screenshots.
# Playwright runs headless so it doesn't strictly need it, but having a
# display makes the screenshot stack work identically to local dev.
CMD ["bash", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &>/dev/null & sleep 1 && exec uv run uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1"]
