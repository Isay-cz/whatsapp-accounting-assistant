# Build context: repo root (no ./api) -- alembic.ini y db/migrations viven un
# nivel arriba de este directorio y el entrypoint los necesita para migrar.
# docker-compose.yml y el workflow de CI construyen con
# `context: .` + `dockerfile: api/Dockerfile`.
FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install -r requirements.txt

COPY api/ .
COPY alembic.ini .
COPY db/ ./db/

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
