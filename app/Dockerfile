FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

# gunicorn in front of Flask; a single worker is plenty for the demo and keeps
# the /health behaviour predictable. Binds the internal port nginx proxies to.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "app:app"]
