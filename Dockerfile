FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home monitor && mkdir /data && chown monitor:monitor /data
COPY app.py worker.py sheets_sync.py prices.py email_notifications.py withdrawals.py mexc.py binance.py wallet_generator.py maintenance.py operations.py swaps.py ./
COPY templates ./templates
COPY static ./static
# git pull may inherit manage.py's private umask (077). Normalize only
# packaged application code; /data and runtime credentials stay private.
RUN find /app -type d -exec chmod 755 {} + && find /app -type f -exec chmod 644 {} +
USER monitor
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "90", "app:app"]

