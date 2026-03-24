#!/bin/sh
set -e

echo "Waiting for Postgres to be ready..."
until pg_isready -h postgres -p 5432; do
  sleep 2
done

echo "Postgres is ready"

echo "Running Alembic migrations..."
alembic -c /app/database/alembic.ini upgrade head

echo "Starting backend services..."
exec "$@"