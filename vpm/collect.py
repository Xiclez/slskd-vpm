#!/usr/bin/env python3
"""
collect.py — Reúne en una carpeta plana las canciones descargadas de una lista,
verifica su bitrate real y deja un informe para entregar al cliente.

Uso (desde la carpeta de trabajo de la lista, donde está state.json):
    python3 collect.py --downloads /descargas --out /data/entrega/MI_LISTA
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
import unicodedata

try:
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
except ImportError:
    MP3 = FLAC = None

AUDIO_EXT = (".mp3", ".flac")


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def index_downloads(root):
    """basename exacto → [rutas] y basename 'plegado' → [rutas]."""
    exact, folded = {}, {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(AUDIO_EXT):
                continue
            p = os.path.join(dirpath, f)
            exact.setdefault(f, []).append(p)
            folded.setdefault(fold(f), []).append(p)
    return exact, folded


def find_local(remote_path, exact, folded):
    parts = re.split(r"[\\/]", remote_path)
    base, parent = parts[-1], (parts[-2] if len(parts) > 1 else "")
    cands = exact.get(base) or folded.get(fold(base)) or []
    if len(cands) > 1:  # slskd guarda en <carpeta remota>/<archivo>: usamos eso para desempatar
        same_dir = [c for c in cands if os.path.basename(os.path.dirname(c)) == parent]
        cands = same_dir or cands
    return cands[0] if cands else None


def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:180] or "sin_nombre"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--downloads", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-bitrate", type=int, default=320)
    args = ap.parse_args()

    if not os.path.exists(args.state):
        print("Todavía no hay descargas para esta lista (ejecuta primero 'descargar').")
        return 1
    with open(args.state, encoding="utf-8") as fh:
        state = json.load(fh)
    os.makedirs(args.out, exist_ok=True)
    exact, folded = index_downloads(args.downloads)

    rows, copied, low, missing, nflac = [], 0, 0, 0, 0
    todo = [e for e in sorted(state.values(), key=lambda x: x["track"]["index"])
            if e["status"] == "downloaded" and e.get("current")]
    print(f"Descargadas según el progreso: {len(todo)}. Revisando y copiando a la entrega…", flush=True)
    for i, e in enumerate(todo, 1):
        t = e["track"]
        c = e["current"]
        src = find_local(c["filename"], exact, folded)
        if not src:
            missing += 1
            rows.append([t["index"], t["original"], "", "", "", "", c["username"], "NO ENCONTRADO EN DISCO"])
            continue

        name = safe_name(os.path.basename(src))
        dst = os.path.join(args.out, name)
        # mismo nombre pero otro archivo (otra canción que se llama igual): numerar
        if os.path.exists(dst) and os.path.getsize(dst) != os.path.getsize(src):
            stem, ext = os.path.splitext(name)
            i = 2
            while os.path.exists(os.path.join(args.out, f"{stem} ({i}){ext}")):
                i += 1
            name = f"{stem} ({i}){ext}"
            dst = os.path.join(args.out, name)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied += 1
            print(f"  [{i}/{len(todo)}] + {name}", flush=True)
        elif i % 50 == 0:
            print(f"  [{i}/{len(todo)}] (ya estaban)", flush=True)

        fmt, kbps, dur, note = "", "", "", "ok"
        is_flac = dst.lower().endswith(".flac")
        if MP3:
            try:
                if is_flac:
                    nflac += 1
                    info = FLAC(dst).info
                    fmt = f"FLAC {info.bits_per_sample}bit/{info.sample_rate / 1000:g}kHz"
                    kbps = round(info.bitrate / 1000) if getattr(info, "bitrate", 0) else ""
                    if info.sample_rate < 44100 or info.bits_per_sample < 16:
                        note = "FLAC de baja resolución"
                        low += 1
                else:
                    info = MP3(dst).info
                    fmt = "MP3"
                    kbps = round(info.bitrate / 1000)
                    if kbps < args.min_bitrate - 4:
                        note = f"BITRATE BAJO ({kbps} kbps)"
                        low += 1
                    elif getattr(info, "bitrate_mode", None) and "VBR" in str(info.bitrate_mode):
                        note = "VBR"
                dur = f"{int(info.length // 60)}:{int(info.length % 60):02d}"
            except Exception as ex:
                note = f"no se pudo leer: {ex.__class__.__name__}"
        rows.append([t["index"], t["original"], name, fmt, kbps, dur, c["username"], note])

    report = os.path.join(args.out, "_informe.csv")
    with open(report, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "pedido", "archivo", "formato", "kbps", "duracion", "usuario_soulseek", "nota"])
        w.writerows(rows)

    nf = "no_encontradas.txt"
    if os.path.exists(nf):
        shutil.copy2(nf, os.path.join(args.out, "_no_encontradas.txt"))

    print(f"Copiadas ahora: {copied}   en la carpeta: {len([r for r in rows if r[2]])}")
    if nflac:
        print(f"  {nflac} en FLAC (no había MP3 320)")
    if low:
        print(f"  ⚠ {low} con calidad menor a la esperada (ver _informe.csv)")
    if missing:
        print(f"  ⚠ {missing} marcadas como descargadas pero no están en disco")
    print(f"→ {args.out}")


if __name__ == "__main__":
    sys.exit(main())
