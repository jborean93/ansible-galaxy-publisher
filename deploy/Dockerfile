# Ansible Galaxy Publisher - Production Container
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy application code and frozen requirements
COPY pyproject.toml ./
COPY src/ ./src/
COPY deploy/requirements.txt ./

# Install production dependencies from frozen requirements
# This requirements.txt is generated from uv.lock by the deployment script
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps -e .

# Copy configuration
# This will be the user's servers.yml with env var references
COPY config/servers.yml /app/config/servers.yml

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=20s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Run the application
CMD ["python", "-m", "uvicorn", "galaxy_publisher.main:app", "--host", "0.0.0.0", "--port", "8000"]
