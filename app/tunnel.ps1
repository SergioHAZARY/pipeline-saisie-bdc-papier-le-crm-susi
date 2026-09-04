# Ouvre un tunnel Cloudflare vers la plateforme locale.
# ATTENTION : l'URL trycloudflare change à chaque lancement — ne la mettez ni
# dans un favori ni dans une doc. Pour une URL stable : tunnel Cloudflare nommé
# ou Tailscale (chantier de durcissement n° 4).
cloudflared tunnel --url http://localhost:8760
