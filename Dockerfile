FROM python:3.13-slim AS verified
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /verify
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY livros_baltigo ./livros_baltigo
COPY tests ./tests
COPY pyproject.toml ./
RUN python -m compileall -q livros_baltigo && python -m pytest -ra --junitxml=/verify/test-results.xml

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data DROP_PRIVILEGES=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 10001 livros \
    && useradd -u 10001 -g livros --no-create-home livros \
    && mkdir -p /app/data \
    && chown -R livros:livros /app
COPY --from=verified --chown=livros:livros /verify/livros_baltigo ./livros_baltigo
COPY --from=verified /verify/test-results.xml ./test-results.xml
CMD ["python", "-m", "livros_baltigo"]
