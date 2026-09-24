# syntax=docker/dockerfile:1

FROM cgr.dev/chainguard/python:latest-dev AS build

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/venv \
    PATH="/app/venv/bin:${PATH}"

WORKDIR /app

RUN python -m venv "${VIRTUAL_ENV}"

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir ".[slack]" \
    && pip uninstall --yes pip setuptools wheel


FROM cgr.dev/chainguard/python:latest AS run

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/venv \
    PATH="/app/venv/bin:${PATH}"

WORKDIR /app

COPY --from=build /app/venv /app/venv
COPY skills/ skills/
COPY alembic/ alembic/
COPY alembic.ini .

USER nonroot

ENTRYPOINT ["status"]
CMD ["slack", "run"]
