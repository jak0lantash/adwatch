# One image per fork. The base image comes from fork/build.env (BASE_IMAGE=...), e.g. a
# Playwright image for sites read in a browser; plain Python otherwise.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 PORT=8080 DEBUG_DIR=/data/debug
WORKDIR /srv
COPY . .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ -f fork/requirements.txt ]; then pip install --no-cache-dir -r fork/requirements.txt; fi && \
    rm -rf tests fork/tests example/tests
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=5)"
CMD ["python3", "-m", "adwatch.server"]
