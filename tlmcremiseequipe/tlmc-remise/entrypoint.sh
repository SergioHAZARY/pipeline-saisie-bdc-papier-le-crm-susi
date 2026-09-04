#!/bin/sh
# Au premier démarrage sur un volume vierge, importe les lots embarqués dans l'image.
# NB : ignorer lost+found, présent sur tout volume ext4 fraîchement formaté
if [ -d /app/sessions_seed ] && [ -z "$(ls -A /app/sessions 2>/dev/null | grep -v lost+found)" ]; then
  echo "Volume vide : import des lots embarqués"
  cp -r /app/sessions_seed/. /app/sessions/
fi
exec uvicorn app:app --host 0.0.0.0 --port 8080
