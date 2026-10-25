# previ-meteo

Prépare les prévisions AROME + ARPEGE (API officielles Météo-France) pour le site Hexagone Flux et les dépose dans Cloudflare KV.

## Mise en route
1. Dépôt GitHub contenant ces fichiers : **privé** possible (2 000 min/mois gratuites, le cron est calé pour ça) ou public (illimité).
2. Cloudflare → Storage & databases → **KV** → Create a namespace : `HEXAGONE_PREVI`. Noter son **ID**.
3. Cloudflare → My Profile → API Tokens → Create Token → **Custom token** → permission *Account · Workers KV Storage · Edit* → noter le jeton.
4. Secrets du dépôt (Settings → Secrets and variables → Actions) : `MF_KEY_AROME`, `MF_KEY_ARPEGE`, `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID`, `CF_API_TOKEN`.
5. Premier test : Actions → previ-meteo → Run workflow → `discover` coché. Vérifier que les noms de couvertures correspondent à `PARAMS` dans `previ.py` (sinon adapter les préfixes).
6. Deuxième test : Run workflow avec `force` coché. Vérifier dans KV la clé `meta` et les clés `t_41_0` … `t_51_9`.
7. Pages → Settings → Bindings → KV namespace : variable `METEO_KV` → `HEXAGONE_PREVI`. Redéployer.

## Limites KV gratuites à respecter
1 000 écritures/jour, 100 000 lectures/jour, 1 Go. Ce projet publie 4 fois par jour (≈ 708 écritures).
