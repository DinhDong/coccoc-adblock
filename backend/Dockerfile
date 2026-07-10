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

ENTRYPOINT ["python", "-m"]
CMD ["app.services.workflow", "--help"]
