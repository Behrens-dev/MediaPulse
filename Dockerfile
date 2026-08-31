FROM python:3.12-slim

WORKDIR /opt/plexpulse

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV PLEXPULSE_DATA=/config
VOLUME /config
EXPOSE 8181

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8181/api/activity', timeout=5)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8181"]
