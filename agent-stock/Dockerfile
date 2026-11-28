# Agent de gestion de stock - image autonome
FROM python:3.12-slim

WORKDIR /app

# Dépendances d'abord (meilleur cache Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code de l'agent (le .env n'est JAMAIS copié : voir .dockerignore)
COPY . .

# Le port d'écoute doit correspondre à celui de base_url dans agent.manifest.json
EXPOSE 9001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:9001/health'); r.raise_for_status()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9001"]