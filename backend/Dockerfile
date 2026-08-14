

FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

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

# Default: run the Flask dev server with debug mode.
# Override via docker-compose `command:` for other entry points
# (uvicorn, workflow, pytest, etc.)
ENV FLASK_APP=app
EXPOSE 5000
CMD ["python", "-m", "flask", "--debug", "run", "--host=0.0.0.0", "--port=5000"]
