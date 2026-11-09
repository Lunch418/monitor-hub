# Образ для проверок (make docker-test). На сервер monitor-hub ставится
# через venv и deploy/install.sh, см. README.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-tui.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-tui.txt -r requirements-dev.txt

COPY . .
RUN useradd --create-home app && chown -R app:app /app
USER app

CMD ["python", "-m", "pytest", "-q"]
