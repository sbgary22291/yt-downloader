FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV CLOUD_MODE=1
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2 --threads 4
