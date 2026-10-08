#!/usr/bin/env bash
# subir.sh — arma la entrega de una lista y la sube a Google Drive con el rclone del host.
#
# Uso:
#   ./subir.sh <lista> <remoto:carpeta> [--link]
#
# Ejemplos:
#   ./subir.sh VPM_FALTAN_POR_MEJORAR gdrive:Clientes/Cliente1
#   ./subir.sh VPM_FALTAN_POR_MEJORAR gdrive:Clientes/Cliente1 --link   # + enlace para compartir
#
# Es incremental: puedes ejecutarlo varias veces mientras la descarga avanza;
# rclone solo sube los archivos nuevos o cambiados y nunca borra nada en Drive.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -lt 2 ]]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

LISTA=$(basename "$1" .txt)
DESTINO="$2"
LINK="${3:-}"
REMOTO="${DESTINO%%:*}"

command -v rclone >/dev/null || { echo "rclone no está instalado en este servidor."; exit 1; }
if [[ "$DESTINO" != *:* ]] || ! rclone listremotes | grep -qx "${REMOTO}:"; then
  echo "El remoto '${REMOTO}:' no existe. Remotos configurados:"
  rclone listremotes | sed 's/^/  /'
  exit 1
fi

echo "== 1/2 Armando ./entrega/$LISTA con lo descargado hasta ahora"
docker compose run --rm vpm entregar "$LISTA"

ORIGEN="entrega/$LISTA"
[[ -d "$ORIGEN" ]] || { echo "No existe $ORIGEN: todavía no hay nada descargado."; exit 1; }

echo
echo "== 2/2 Subiendo a $DESTINO"
rclone copy "$ORIGEN" "$DESTINO" \
  --progress --stats-one-line \
  --transfers 4 --checkers 8 \
  --drive-chunk-size 64M

echo
echo "Archivos en Drive:"
rclone size "$DESTINO" 2>/dev/null | sed 's/^/  /' || true

if [[ "$LINK" == "--link" ]]; then
  echo
  echo "Enlace para compartir (cualquiera con el enlace puede ver):"
  rclone link "$DESTINO" || echo "  no se pudo generar el enlace (compártelo desde la web de Drive)"
fi
