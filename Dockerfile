# MDC (the ModelDB server) as a container: its Universal Object API and Storage Explorer,
# with the DuckDB database on a volume at /data. Railway builds this (railway.json); anywhere
# else: docker build -t modeldb . && docker run -p 8000:8000 -v modeldb:/data -e MDC_API_TOKENS=... modeldb
# (without MDC_API_TOKENS it makes a token, keeps it in /data/modeldb-api-token and prints it)
FROM python:3.12-slim
WORKDIR /app
COPY mdc/requirements.txt mdc/requirements.txt
RUN pip install --no-cache-dir -r mdc/requirements.txt
COPY mdc mdc
COPY deploy/serve.py deploy/serve.py
# HOME is on the volume so tokens issued with `mdc token issue` survive restarts.
ENV PYTHONPATH=/app/mdc/src HOME=/data PORT=8000 MODELDB_DATABASE=/data/mdc.duckdb PYTHONUNBUFFERED=1
WORKDIR /app/mdc
EXPOSE 8000
CMD ["python", "/app/deploy/serve.py"]
