FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install flask flask-cors spotdl

CMD ["python", "app.py"]