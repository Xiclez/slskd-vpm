#!/usr/bin/env bash
# setup.sh — configura el descargador aislado (se ejecuta una sola vez).
# Genera: .env, slskd/slskd.yml (con API key y contraseña web aleatorias) y las carpetas.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f slskd/slskd.yml ]]; then
  read -rp "Ya existe slskd/slskd.yml. ¿Reconfigurar desde cero? [s/N] " r
  [[ "$r" =~ ^[sS]$ ]] || { echo "Sin cambios."; exit 0; }
fi

command -v docker >/dev/null || { echo "Falta Docker. Instálalo primero."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Falta el plugin 'docker compose'."; exit 1; }
command -v python3 >/dev/null || { echo "Falta python3 (sudo apt install python3)."; exit 1; }

rand() { python3 -c "import secrets; print(secrets.token_hex($1))"; }

echo
echo "== Cuenta de Soulseek para este descargador =="
echo "IMPORTANTE: usa una cuenta DISTINTA a la de tu slskd principal. Si dos clientes"
echo "entran con el mismo usuario, Soulseek desconecta a uno de los dos todo el tiempo."
echo "No hay que registrarse en ninguna web: si el usuario no existe, Soulseek lo crea"
echo "con esa contraseña la primera vez que se conecta."
echo
read -rp  "Usuario Soulseek: " SLSK_USER
while [[ -z "$SLSK_USER" ]]; do read -rp "Usuario Soulseek: " SLSK_USER; done
read -rsp "Contraseña Soulseek: " SLSK_PASS; echo
while [[ -z "$SLSK_PASS" ]]; do read -rsp "Contraseña Soulseek: " SLSK_PASS; echo; done

read -rp "Puerto web de este slskd [5031]: " WEB_PORT;  WEB_PORT=${WEB_PORT:-5031}
read -rp "Puerto Soulseek de este slskd [50301]: " SLSK_PORT; SLSK_PORT=${SLSK_PORT:-50301}

API_KEY=$(rand 24)        # 48 caracteres hex: esta es la "API key"
WEB_USER=vpm
WEB_PASS=$(rand 8)

mkdir -p slskd descargas incompletas listas trabajo entrega

cat > .env <<EOF
PUID=$(id -u)
PGID=$(id -g)
WEB_PORT=$WEB_PORT
SLSK_PORT=$SLSK_PORT
API_KEY=$API_KEY
EOF
chmod 600 .env

# Render de la plantilla (comillas YAML seguras para contraseñas con símbolos)
SLSK_USER="$SLSK_USER" SLSK_PASS="$SLSK_PASS" SLSK_PORT="$SLSK_PORT" \
WEB_USER="$WEB_USER" WEB_PASS="$WEB_PASS" API_KEY="$API_KEY" python3 - <<'PY'
import os
q = lambda v: "'" + v.replace("'", "''") + "'"
t = open("slskd.yml.template", encoding="utf-8").read()
for k in ("SLSK_USER", "SLSK_PASS", "WEB_USER", "WEB_PASS", "API_KEY"):
    t = t.replace(f"__{k}__", q(os.environ[k]))
t = t.replace("__SLSK_PORT__", os.environ["SLSK_PORT"])
open("slskd/slskd.yml", "w", encoding="utf-8").write(t)
PY
chmod 600 slskd/slskd.yml

echo
echo "✓ Configuración lista."
echo
echo "  Interfaz web:  http://<IP-del-servidor>:$WEB_PORT"
echo "  Usuario web:   $WEB_USER"
echo "  Clave web:     $WEB_PASS"
echo "  API key:       $API_KEY   (ya está puesta en .env y slskd.yml, no tienes que copiarla)"
echo
echo "Siguiente paso:"
echo "  docker compose up -d slskd"
echo "  docker compose build vpm"
echo "  docker compose run --rm vpm listas"
