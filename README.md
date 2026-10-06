# vpm-downloader

Descargador aislado: le das una lista `.txt` de canciones y te entrega los MP3 a 320 kbps en una carpeta lista para el cliente.
Trae su propio slskd en Docker; no toca tu otra instancia de slskd, ni Navidrome, ni rclone.

```
vpm-downloader/
├── docker-compose.yml
├── setup.sh               ← se ejecuta una vez
├── slskd.yml.template
├── vpm/                   ← código del descargador (imagen Docker)
├── listas/                ← aquí pones los .txt de cada cliente
├── trabajo/               ← progreso interno de cada lista (se crea solo)
├── descargas/             ← donde slskd descarga (se crea solo)
└── entrega/               ← carpeta final por lista, con informe (se crea sola)
```

## Instalación (una sola vez)

Requisitos: Docker con el plugin `docker compose`, y `python3` en el host (Ubuntu ya lo trae).

```bash
# copia la carpeta al servidor, por ejemplo:
scp vpm-downloader.zip usuario@servidor:~
ssh usuario@servidor
unzip vpm-downloader.zip && cd vpm-downloader

./setup.sh
docker compose up -d slskd
docker compose build vpm
```

`setup.sh` te pide un usuario y una contraseña de Soulseek, y genera solo la API key y la clave de la interfaz web.

- **Usa una cuenta distinta a la de tu slskd principal.** Si dos clientes se conectan con la misma cuenta, Soulseek los desconecta mutuamente.
- **No hace falta registrarse:** si el usuario no existe, Soulseek lo crea la primera vez que se conecta con esa contraseña.

Para comprobar que se conectó, revisa los logs con `docker compose logs -f slskd` hasta ver que entró a la red.
También puedes abrir la web en `http://IP-del-servidor:5031`.

## Uso por cliente

```bash
cp ~/pedido_cliente.txt listas/          # 1. pon la lista en ./listas

docker compose run --rm vpm preparar pedido_cliente     # 2. limpia la lista
docker compose run --rm vpm probar pedido_cliente 10    # 3. simula 10 búsquedas (opcional)
docker compose run --rm vpm descargar pedido_cliente    # 4. busca, descarga y espera
docker compose run --rm vpm entregar pedido_cliente     # 5. arma ./entrega/pedido_cliente
```

O todo de una vez: `docker compose run --rm vpm todo pedido_cliente`.

Una lista de ~680 canciones tarda varias horas: cada búsqueda toma ~20 s y luego hay que esperar las descargas.
Lánzala dentro de `tmux` o `screen`, o en segundo plano:

```bash
nohup docker compose run --rm vpm todo pedido_cliente > trabajo/pedido_cliente.log 2>&1 &
tail -f trabajo/pedido_cliente.log
```

Si se corta, vuelve a ejecutar `descargar`: el script retoma donde iba.

### Qué recibe el cliente

`./entrega/<lista>/` contiene los MP3 y dos archivos de control:

- `_informe.csv` (abre bien en Excel) indica, por canción, qué se pidió, el archivo entregado, el bitrate real verificado, la duración y una nota.
  La nota avisa con **BITRATE BAJO** si un usuario anunció 320 pero el archivo no lo era.
- `_no_encontradas.txt` lista lo que no se consiguió en 320, con el motivo.

### Otros comandos

| Comando | Para qué |
|---|---|
| `vpm listas` | ver todas las listas y su avance |
| `vpm estado <lista>` | revisar descargas, reintentar las fallidas con otro usuario y actualizar faltantes |
| `vpm reintentar <lista>` | volver a buscar las no encontradas (útil días después: la red cambia) |

Al buscador se le pueden pasar opciones extra, por ejemplo `docker compose run --rm vpm descargar pedido_cliente --include-ambiguous --min-score 30`.

| Opción | Por defecto | Para qué |
|---|---|---|
| `--include-ambiguous` | no | buscar también títulos sin artista y muy cortos ("Action") |
| `--min-score` | 20 | súbelo si ves elecciones dudosas, bájalo si encuentra poco |
| `--delay` | 3 | pausa entre búsquedas, para no saturar la red |
| `--search-timeout` | 15 | segundos que se esperan respuestas por búsqueda |

## Notas

- **Puertos:** 5031 (web) y 50301 (Soulseek), para no chocar con tu slskd principal. Se cambian en `.env`.
  Abrir el 50301 en el router es opcional, pero mejora la conexión con más usuarios.
- **Compartir:** este slskd comparte la carpeta `descargas`. Es la norma en Soulseek y muchos usuarios bloquean a quien no comparte nada.
  Si no lo quieres, borra el bloque `shares` en `slskd/slskd.yml` y ejecuta `docker compose restart slskd`.
- **La API key** está en `.env` y en `slskd/slskd.yml`. La inventó `setup.sh`; si algún día quieres cambiarla, edita ambos archivos y reinicia slskd.
- **Para apagarlo todo:** `docker compose down`. Tus listas, descargas y entregas quedan en las carpetas.
