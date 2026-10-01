#!/bin/sh
# Container entrypoint.
#   web        apply migrations, then serve with gunicorn on :8000
#   scheduler  run the background jobs (poll, rollups, housekeeping) on their timers
#   anything else is run as a command, e.g. `python manage.py createsuperuser`
set -e

wait_for_db() {
    # The database container may still be starting even after its healthcheck
    # passes the first time; retry for up to a minute.
    i=0
    until python manage.py check --database default >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "Database not reachable after 60 s:" >&2
            python manage.py check --database default
            exit 1
        fi
        sleep 2
    done
}

case "$1" in
    web)
        wait_for_db
        python manage.py migrate --noinput
        exec gunicorn WS4Free.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${GUNICORN_WORKERS:-3}" --threads 2 \
            --timeout 120 --graceful-timeout 30 \
            --max-requests 1000 --max-requests-jitter 100 \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
            --access-logfile - --error-logfile -
        ;;
    scheduler)
        wait_for_db
        # The web container applies migrations; don't run jobs against an old schema
        # (first start, or right after an upgrade).
        until python manage.py migrate --check >/dev/null 2>&1; do
            echo "[scheduler] waiting for migrations to be applied…"
            sleep 5
        done
        exec python docker/scheduler.py
        ;;
    *)
        exec "$@"
        ;;
esac
