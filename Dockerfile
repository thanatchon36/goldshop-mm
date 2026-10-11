# syntax=docker/dockerfile:1

# ---- build: install the package + deps into an isolated venv ----------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml ./
COPY goldshop ./goldshop
RUN pip install .

# ---- runtime: venv + config only, non-root -----------------------------------
FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin goldshop \
 && mkdir -p /app/data \
 && chown goldshop:goldshop /app/data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY config.yaml ./config.yaml
USER goldshop

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]

# Paper-mode bot (phase 2). Market data + recorder only: python -m goldshop.market_data
CMD ["python", "-m", "goldshop"]
