# ─── iFind Scraper API — Hugging Face Spaces Dockerfile ──────────────────────
#
# HF Spaces requirements:
#   - Must run as uid 1000 (non-root)
#   - Must listen on port 7860
#   - Secrets (MONGODB_URI, COHERE_API_KEY) injected as runtime env vars
#
# Selenium scrapers need a real Chrome binary + matching ChromeDriver.
# We install Google Chrome stable and let webdriver-manager handle the driver,
# OR install chromedriver directly to avoid network calls at runtime.

FROM python:3.11-slim

# ─── System deps + Google Chrome ─────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chrome runtime deps
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    # Misc
    curl \
    unzip \
 && rm -rf /var/lib/apt/lists/*

# Install Google Chrome stable
RUN wget -q -O /tmp/chrome.deb \
    https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

# ─── Non-root user (HF Spaces requires uid 1000) ─────────────────────────────
RUN useradd -m -u 1000 user

USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    # Tell Selenium/webdriver-manager where to cache the driver
    WDM_CACHE_PATH=/home/user \
    # Suppress webdriver-manager download logs
    WDM_LOG=0 \
    # Chrome flags for headless containerised use
    CHROME_FLAGS="--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu"

WORKDIR $HOME/app

# ─── Python dependencies ──────────────────────────────────────────────────────
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Pre-download ChromeDriver at build time so there are no network calls at
# runtime (HF free tier has limited egress during scraping).
RUN python - <<'EOF'
from webdriver_manager.chrome import ChromeDriverManager
ChromeDriverManager().install()
EOF

# ─── Application code ─────────────────────────────────────────────────────────
COPY --chown=user . $HOME/app

# Checkpoint file lives inside the container (ephemeral — fine for scraping).
# If you attach a HF persistent storage bucket, mount it at /home/user/app/data.
RUN mkdir -p $HOME/app/data

# ─── Runtime ──────────────────────────────────────────────────────────────────
EXPOSE 10000

# MONGODB_URI and COHERE_API_KEY must be set as Secrets in the HF Space settings.
# They are injected automatically as environment variables at runtime.

# Render injects $PORT (default 10000); fall back to 7860 for local/other hosts.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
