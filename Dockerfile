# Start with a modern lightweight Python image
FROM python:3.13-slim

# Copy uv directly from Astral's official container to vastly speed up dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Set default application working directory
WORKDIR /app

# Ensure we don't accidentally cache compiled pyc files in the container layer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Step 1: Just copy project setup files for better caching of dependencies
COPY requirements.txt ./

# Step 2: Install dependencies from requirements file, without generating a venv
RUN uv pip install --system -r requirements.txt

# Step 3: Copy all the actual microservice code
COPY . .

# Step 4: Expose the FastMCP HTTP port
EXPOSE 8000

# Step 5: Start the PRism FastMCP server using FastMCP's SSE HTTP transport built into server.py
CMD ["python", "server.py"]
