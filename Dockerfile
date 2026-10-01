# WS4Free container image. See README "Docker" and compose.yaml.
#
# Two stages: build wheels with compilers present, then install them into a slim
# runtime that only carries the MySQL client library.

FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config default-libmysqlclient-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements.txt requirements-postgres.txt ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt -r requirements-postgres.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_LOG_FILE=/app/data/django_errors.log
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmariadb3 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 ws4free
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels

WORKDIR /app
COPY --chown=ws4free:ws4free . .
# Static files are collected at build time; WhiteNoise serves them. The settings
# need a secret and hosts to import, so give throwaway ones for this step only.
RUN DJANGO_SECRET_KEY=build-only DJANGO_ALLOWED_HOSTS=build DB_ENGINE=sqlite DJANGO_LOG_FILE=/dev/null \
    python manage.py collectstatic --noinput \
 && mkdir -p /app/data && chown -R ws4free:ws4free /app/data /app/staticfiles

USER ws4free
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
