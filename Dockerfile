# Screener server in a container: prices every minute while the market is open,
# fundamentals every 30 minutes, page + API on port 8000.
#   docker build -t sp500-screener .
#   docker run -d --restart unless-stopped -p 8000:8000 -v screener-data:/app/data sp500-screener
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY index.html .
COPY data data
ENV PORT=8000 HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "backend.server"]
