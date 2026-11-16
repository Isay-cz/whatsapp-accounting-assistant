# Build context: repo root (no ./api) -- alembic.ini y db/migrations viven un
# nivel arriba de este directorio y el entrypoint los necesita para migrar.
# docker-compose.yml y el workflow de CI construyen con
# `context: .` + `dockerfile: api/Dockerfile`.
FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ .
COPY alembic.ini .
COPY db/ ./db/

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
# Comando de producción: sin `--reload` (un vigilante de archivos no tiene nada
# que hacer en la imagen desplegada) y **sin `--workers`**: los vigilantes del
# buffer y el poll de la whitelist viven en memoria, en un solo proceso.
# Desarrollo recupera `--reload` desde el `command` de docker-compose.yml.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
