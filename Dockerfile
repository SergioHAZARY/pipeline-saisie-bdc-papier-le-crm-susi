# susi-bdc — image de déploiement Fly.io
#
# Trois exigences que l'hébergement mutualisé PHP ne sait pas satisfaire, et qui
# expliquent le passage par un conteneur : un runtime Python en processus long,
# le binaire poppler pour le rendu des scans, et la possibilité de lancer
# `claude -p` en sous-processus pour les jobs d'extraction.

FROM python:3.13-slim

# poppler-utils fournit pdftoppm / pdfinfo. Sans lui, l'accueil d'un lot échoue
# au rendu des pages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# CLI Claude Code : nécessaire aux jobs d'extraction (le job runner l'invoque en
# sous-processus). Sans jeton d'authentification au démarrage, la plateforme
# reste parfaitement utilisable en revue/validation — seuls les jobs échouent,
# proprement et avec un message dans le log.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r /app/app/requirements.txt

COPY app/ /app/app/
COPY .claude/ /app/.claude/

# lots/ est un volume monté : les scans clients ne sont jamais dans l'image.
RUN mkdir -p /app/lots

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8080"]
