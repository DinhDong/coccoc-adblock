# --------------------------------------------------------------------------
# Dockerfile — AdBlock Rule Engine Generator (backend)
#
# Base image ships with Chromium, Firefox and WebKit pre-installed so
# Playwright works out of the box for crawling and sandbox validation.
#
# Build:   docker compose build
# Run:     docker compose run --rm backend app.services.workflow <report_id>
# --------------------------------------------------------------------------

FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first (layer cache optimisation)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

ENV PYTHONPATH=/app

# Default entrypoint lets callers pick any module:
#   docker compose run --rm backend app.services.workflow vnexpress-desktop
#   docker compose run --rm backend app.services.crawler https://... id
#   docker compose run --rm backend pytest app/tests/ -v
ENTRYPOINT ["python", "-m"]
CMD ["app.services.workflow", "--help"]
