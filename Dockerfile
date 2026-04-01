FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 2112

CMD ["sh", "-c", "echo '[startup] DATABASE_URL=${DATABASE_URL:-unset}' && echo '[startup] Running migrations: alembic upgrade head' && alembic upgrade head && echo '[startup] Starting web server: uvicorn app.main:app --host 0.0.0.0 --port 2112' && uvicorn app.main:app --host 0.0.0.0 --port 2112"]
