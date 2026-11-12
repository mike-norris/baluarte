# syntax=docker/dockerfile:1

# Shared image for the API and (from BAL-009) the worker; they differ only by command.
FROM python:3.15-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# pip is only needed to build the environment, so it is removed from it afterwards.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir . \
    && /opt/venv/bin/pip uninstall -y pip

FROM python:3.15-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Pick up Debian security fixes released after the base image was built. pip and its bundled
# wheel are never used at runtime and vendor their own dependencies, so they are removed.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip && rm -f /usr/local/lib/python3.*/ensurepip/_bundled/*.whl
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin baluarte
COPY --from=build /opt/venv /opt/venv
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"]
CMD ["uvicorn", "baluarte.main:app", "--host", "0.0.0.0", "--port", "8000"]
