FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /srv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY config ./config
COPY prompts ./prompts
COPY db ./db
COPY scripts ./scripts

EXPOSE 8000
CMD ["sh", "-c", "/srv/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
