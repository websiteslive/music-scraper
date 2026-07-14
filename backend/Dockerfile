FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# Install system dependencies (ffmpeg is required for audio processing)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browser binaries already ship in this base image, but this is a harmless
# no-op safety net in case the base image version drifts from requirements.txt.
RUN playwright install --with-deps chromium

COPY . .

# Hugging Face Spaces (Docker SDK) expects the container to listen on 7860.
EXPOSE 7860

CMD ["python", "app.py"]