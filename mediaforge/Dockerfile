FROM python:3.12-slim

LABEL org.opencontainers.image.description="MediaForge - ffmpeg toolbox for your Plex library (downmix, subtitles, track surgery, A/V sync)"

# ffmpeg is bundled so jobs can run inside this container
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mediaforge

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# commit this image was built from (set by CI); powers the weekly update check
ARG GIT_SHA=
ENV GIT_SHA=${GIT_SHA}

ENV MEDIAFORGE_DATA=/config
VOLUME /config
EXPOSE 8191

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8191/api/status', timeout=5)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8191"]
