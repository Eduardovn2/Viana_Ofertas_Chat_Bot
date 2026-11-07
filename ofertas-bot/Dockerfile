FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv/ofertas-bot
COPY requirements.deploy.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.deploy.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin vianinha
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY docs/CONTRATO_FONTES_SHOPEE_20261026.json ./docs/CONTRATO_FONTES_SHOPEE_20261026.json
COPY docs/CONTRATO_INDICADORES_SHOPEE_20261021.json ./docs/CONTRATO_INDICADORES_SHOPEE_20261021.json
USER 10001:10001
CMD ["python", "-m", "uvicorn", "app.deployment:create_control_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
