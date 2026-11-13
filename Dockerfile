FROM python:3.12-slim

WORKDIR /app

# Install system dependencies required for compilation and psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy build files and project source
COPY pyproject.toml README.md /app/
COPY src/ /app/src/

# Install the package and dependencies
RUN pip install --no-cache-dir -e .
RUN pip install --no-cache-dir gunicorn gevent

# Expose the API port
EXPOSE 5000

# Start the Flask factory via Gunicorn
CMD ["gunicorn", "--worker-class", "gthread", "-w", "1", "--threads", "10", "-b", "0.0.0.0:5000", "--timeout", "3600", "goldedge.adapters.primary.rest_api.factory:create_app()"]
