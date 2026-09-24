# syntax=docker/dockerfile:1

FROM registry.access.redhat.com/hi/python:3.11-builder AS build

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/venv \
    PATH="/app/venv/bin:${PATH}"

WORKDIR /app

RUN python3 -m venv "${VIRTUAL_ENV}"

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir ".[slack]" \
    && pip uninstall --yes pip setuptools wheel


FROM registry.access.redhat.com/hi/python:3.11 AS run

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/venv \
    PATH="/app/venv/bin:${PATH}"

USER 0

RUN ["/usr/local/bin/pip", "uninstall", "--yes", "pip", "setuptools", "wheel"]

WORKDIR /app

COPY --from=build /app/venv /app/venv
COPY skills/ skills/
COPY alembic/ alembic/
COPY alembic.ini .

USER 65532:65532

ENTRYPOINT ["status"]
CMD ["slack", "run"]
