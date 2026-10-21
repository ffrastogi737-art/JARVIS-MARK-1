# JARVIS Cloud Brain (Phase 9) — the always-on server: LLM router, Gemini Live relay,
# edge task queue, tether, research, skills, self-evolution. The desktop GUI, terminal
# CUI and edge daemon run on the laptop and are not part of this image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY brain ./brain
RUN useradd --create-home --uid 10001 jarvis && chown -R jarvis /app
USER jarvis

EXPOSE 8000
# Hosts (Render, Railway, ECS) inject PORT; brain.config reads it, so main.py binds to it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"
CMD ["python", "main.py"]
