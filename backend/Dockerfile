FROM python:3.11-slim

WORKDIR /app

# ffmpeg is required for audio processing (yt-dlp downloads + transcodes via it)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces (Docker SDK) expects the container to listen on 7860.
# Render ignores EXPOSE and just uses $PORT at runtime, which app.py already reads.
EXPOSE 7860

CMD ["python", "app.py"]