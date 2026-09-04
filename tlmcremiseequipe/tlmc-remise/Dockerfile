FROM python:3.12-slim

# tesseract : OSD (détection d'orientation) + OCR local éventuel
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tlmc/ tlmc/
COPY spec/layout.json spec/
COPY app.py remise.py entrypoint.sh ./
# lots embarqués (seed) : copiés dans le volume au premier démarrage
COPY sessions/ /app/sessions_seed/

# les lots vivent ici : à monter sur un volume persistant
VOLUME /app/sessions

EXPOSE 8080
CMD ["./entrypoint.sh"]
