#!/usr/bin/env bash
# subir.sh — arma la entrega de una lista y la sube a Google Drive con el rclone del host.
#
# Uso:
#   ./subir.sh <lista> <remoto:carpeta> [--link] [--seguir [minutos]] [--fg]
#
#   Por defecto corre en SEGUNDO PLANO (sobrevive a cerrar SSH) y te muestra
#   el log en vivo; Ctrl+C solo deja de mirar, la subida sigue.
#   --seguir   repite armar+subir cada N minutos (30 por defecto) hasta que la
#              descarga termine, y luego hace una pasada final
#   --link     al final imprime un enlace de Drive para compartir
#   --fg       correr en primer plano (se corta si cierras la sesión)
#
# Ejemplo:
#   ./subir.sh MI_LISTA gdrive:Clientes/Cliente1 --seguir --link
#
# Es incremental: solo sube archivos nuevos o cambiados y nunca borra nada en Drive.
set -euo pipefail
SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
cd "$(dirname "$SCRIPT")"

ayuda() { sed -n '2,17p' "$SCRIPT" | sed 's/^# \{0,1\}//'; exit 1; }
[[ $# -lt 2 ]] && ayuda

LISTA=$(basename "$1" .txt); DESTINO="$2"; shift 2
LINK=0; BG=1; SEGUIR=0; INTERVALO=30
while [[ $# -gt 0 ]]; do
  case "$1" in
    --link)   LINK=1 ;;
    --bg)     BG=1 ;;   # (ya es el comportamiento por defecto)
    --fg)     BG=0 ;;
    --seguir) SEGUIR=1
              if [[ "${2:-}" =~ ^[0-9]+$ ]]; then INTERVALO="$2"; shift; fi ;;
    *)        echo "Opción desconocida: $1"; ayuda ;;
  esac
  shift
done

mkdir -p logs
LOG="$PWD/logs/subir_${LISTA}.log"
STATE="trabajo/$LISTA/state.json"
REMOTO="${DESTINO%%:*}"

# ── validaciones (antes de irse a segundo plano, para ver los errores) ──
command -v rclone >/dev/null || { echo "rclone no está instalado en este servidor."; exit 1; }
if [[ "$DESTINO" != *:* ]] || ! rclone listremotes | grep -qx "${REMOTO}:"; then
  echo "El remoto '${REMOTO}:' no existe. Remotos configurados:"
  rclone listremotes | sed 's/^/  /'; exit 1
fi
[[ -f "$STATE" ]] || { echo "No hay progreso para '$LISTA' ($STATE no existe)."; exit 1; }

# ── segundo plano: se relanza desligado de la sesión ──
if [[ $BG -eq 1 && -z "${VPM_SUBIR_HIJO:-}" ]]; then
  if flock -n "logs/.subir_${LISTA}.lock" true 2>/dev/null; then :; else
    echo "Ya hay una subida en curso para '$LISTA'."
    echo "  Ver progreso:  tail -f $LOG"; exit 1
  fi
  args=("$LISTA" "$DESTINO" --fg)
  [[ $LINK -eq 1 ]] && args+=(--link)
  [[ $SEGUIR -eq 1 ]] && args+=(--seguir "$INTERVALO")
  touch "$LOG"
  INICIO=$(( $(wc -l < "$LOG") + 1 ))   # el visor muestra solo esta subida
  echo "──────── $(date '+%Y-%m-%d %H:%M:%S') nueva subida ────────" >> "$LOG"
  # doble fork: el proceso queda colgado de init, no de esta sesión ni del visor
  PIDF="logs/.subir_${LISTA}.pid"
  ( VPM_SUBIR_HIJO=1 setsid nohup bash "$SCRIPT" "${args[@]}" >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDF" )
  PID=$(cat "$PIDF")
  sleep 2
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "✗ El proceso en segundo plano terminó enseguida. Últimas líneas del log:"
    tail -n 15 "$LOG"; exit 1
  fi
  echo "✓ Subiendo en segundo plano (PID $PID). Puedes cerrar la sesión sin problema."
  echo "  Ver progreso después:  tail -f $LOG"
  echo "  Detenerlo:             kill $PID"
  echo
  echo "Mostrando el log en vivo (Ctrl+C solo deja de mirar, la subida sigue)…"
  echo
  exec tail -n +"$INICIO" -f "$LOG" --pid="$PID"
fi

# primer plano: además de la pantalla, todo queda en el log
if [[ -z "${VPM_SUBIR_HIJO:-}" ]]; then
  exec > >(tee -a "$LOG") 2>&1
fi

# una sola subida a la vez por lista
exec 9>"logs/.subir_${LISTA}.lock"
flock -n 9 || { echo "Ya hay una subida en curso para '$LISTA' (mira $LOG)."; exit 1; }

ts() { date '+%Y-%m-%d %H:%M:%S'; }

estado() {  # imprime: descargadas en_curso sin_buscar
  python3 - "$STATE" <<'PY'
import json, sys
s = json.load(open(sys.argv[1])).values()
c = lambda *st: sum(e["status"] in st for e in s)
print(c("downloaded"), c("queued"), c("pending", "dry_run"))
PY
}

contar() { grep -ciE '\.(mp3|flac)$' || true; }

pasada() {
  echo
  echo "════ $(ts)  pasada de subida ════"
  echo "── Armando ./entrega/$LISTA"
  docker compose run --rm -T vpm entregar "$LISTA"
  ORIGEN="entrega/$LISTA"
  [[ -d "$ORIGEN" ]] || { echo "Todavía no hay nada descargado."; return 0; }

  echo "── Subiendo a $DESTINO"
  rclone copy "$ORIGEN" "$DESTINO" \
    --transfers 4 --checkers 8 --drive-chunk-size 64M \
    --retries 5 --low-level-retries 20 \
    --log-level INFO --stats 1m --stats-one-line --stats-log-level NOTICE

  local loc rem
  loc=$(ls "$ORIGEN" | contar)
  rem=$(rclone lsf "$DESTINO" --files-only | contar)
  echo "── $(ts)  audio en entrega: $loc   audio en Drive: $rem"
  [[ "$rem" -lt "$loc" ]] && echo "   ⚠ faltan $((loc - rem)) en Drive; la próxima pasada lo reintenta"
  return 0
}

echo "$(ts)  inicio: lista=$LISTA destino=$DESTINO seguir=$SEGUIR (cada ${INTERVALO} min)"

if [[ $SEGUIR -eq 1 ]]; then
  while true; do
    read -r d q p < <(estado)
    echo
    echo "$(ts)  descarga: $d descargadas, $q en curso, $p sin buscar"
    pasada
    if [[ "$q" -eq 0 && "$p" -eq 0 ]]; then
      echo "$(ts)  la descarga terminó: no queda nada en curso ni sin buscar."
      break
    fi
    echo "$(ts)  próxima pasada en $INTERVALO min…"
    sleep $((INTERVALO * 60))
  done
else
  pasada
fi

if [[ $LINK -eq 1 ]]; then
  echo
  echo "Enlace para compartir (cualquiera con el enlace puede ver):"
  rclone link "$DESTINO" || echo "  no se pudo generar el enlace (compártelo desde la web de Drive)"
fi
echo
echo "$(ts)  fin."
