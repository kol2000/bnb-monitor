FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home monitor && mkdir /data && chown monitor:monitor /data
COPY app.py worker.py sheets_sync.py prices.py ./
COPY templates ./templates
COPY static ./static
USER monitor
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "90", "app:app"]
