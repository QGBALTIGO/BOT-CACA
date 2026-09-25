FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data DROP_PRIVILEGES=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 10001 livros \
    && useradd -u 10001 -g livros --no-create-home livros \
    && mkdir -p /app/data \
    && chown -R livros:livros /app
COPY --chown=livros:livros livros_baltigo ./livros_baltigo
CMD ["python", "-m", "livros_baltigo"]
