FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi httpx Jinja2 pydantic pydantic-settings PyYAML rich uvicorn websockets

EXPOSE 8080

CMD ["python", "-m", "app.main", "serve"]
