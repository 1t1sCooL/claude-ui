FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN pip3 install fastapi uvicorn --break-system-packages

RUN useradd -m -u 1000 claudeuser

WORKDIR /app
COPY app.py .
RUN chown -R claudeuser:claudeuser /app

USER claudeuser
ENV HOME=/home/claudeuser
EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
