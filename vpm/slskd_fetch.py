#!/usr/bin/env python3
"""
slskd_fetch.py — Busca en Soulseek (vía la API de slskd) las canciones de
tracks.json, elige el mejor MP3 320 kbps y lo encola en tu instancia.

Configuración (variables de entorno):
    SLSKD_URL       p. ej. http://localhost:5030   (por defecto)
    SLSKD_API_KEY   la clave definida en slskd.yml → web.authentication.api_keys

Comandos:
    search   busca las pendientes y encola la mejor opción  (--dry-run para solo mirar)
    sync     revisa las descargas en slskd; si una falla, prueba el siguiente candidato
    run      search + sync en bucle hasta que no quede nada en curso
    report   resumen + no_encontradas.txt para la siguiente vuelta
    reset    vuelve a "pending" las que quedaron como not_found / failed

El progreso se guarda en state.json: puedes cortar con Ctrl+C y retomar.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests':  pip install requests")

STATE_FILE = "state.json"
MAX_CANDIDATES = 6
MAX_FLAC = 4
FAIL_STATES = ("Errored", "Rejected", "TimedOut", "Cancelled", "Failed", "Aborted")


# ───────────────────────── utilidades de texto ─────────────────────────

def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def words(s: str) -> list:
    return fold(s).split()


def has_phrase(hay: str, phrase: str) -> bool:
    p = fold(phrase)
    return bool(p) and re.search(rf"(?:^| ){re.escape(p)}(?: |$)", hay) is not None


STOP = {"the", "a", "an", "of", "and", "y", "el", "la", "de"}


# ───────────────────────── cliente slskd ─────────────────────────

class Slskd:
    def __init__(self, url, api_key, timeout=30):
        self.base = url.rstrip("/") + "/api/v0"
        self.s = requests.Session()
        if api_key:
            self.s.headers["X-API-Key"] = api_key
        self.timeout = timeout

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        if r.status_code == 401:
            sys.exit("slskd respondió 401: revisa SLSKD_API_KEY.")
        r.raise_for_status()
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    def search(self, text, search_timeout_ms=15000, max_wait_s=40):
        sid = str(uuid.uuid4())
        self._req("POST", "/searches", json={
            "id": sid,
            "searchText": text,
            "searchTimeout": search_timeout_ms,
            "responseLimit": 200,
            "fileLimit": 20000,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        })
        t0 = time.time()
        try:
            while time.time() - t0 < max_wait_s:
                time.sleep(1.5)
                st = self._req("GET", f"/searches/{sid}") or {}
                if st.get("isComplete") or str(st.get("state", "")).startswith("Completed"):
                    break
            return self._req("GET", f"/searches/{sid}/responses") or []
        finally:
            try:
                self._req("DELETE", f"/searches/{sid}")
            except Exception:
                pass

    def enqueue(self, username, filename, size):
        self._req("POST", f"/transfers/downloads/{quote(username, safe='')}",
                  json=[{"filename": filename, "size": size}])

    def server_state(self):
        return self._req("GET", "/server") or {}

    def wait_ready(self, max_wait=180):
        """Espera a que slskd responda y esté conectado/logueado en Soulseek."""
        t0, last = time.time(), None
        while time.time() - t0 < max_wait:
            try:
                st = self.server_state()
                state = str(st.get("state", ""))
                if st.get("isLoggedIn") or "LoggedIn" in state:
                    return True
                msg = f"slskd responde, estado Soulseek: {state or 'desconocido'}"
            except requests.RequestException as e:
                msg = f"esperando a slskd ({e.__class__.__name__})"
            if msg != last:
                print("…", msg)
                last = msg
            time.sleep(5)
        print("! slskd no quedó conectado a Soulseek. Revisa usuario/contraseña en slskd.yml "
              "y los logs:  docker compose logs slskd")
        return False

    def downloads(self):
        return self._req("GET", "/transfers/downloads") or []


# ───────────────────────── evaluación de resultados ─────────────────────────

def effective_bitrate(f):
    br = f.get("bitRate")
    if br:
        return br
    size, length = f.get("size"), f.get("length")
    if size and length:
        return size * 8 / length / 1000
    return None


def score_file(track, resp, f, opts):
    """Devuelve (puntaje, motivo) o (None, motivo_de_descarte)."""
    path = f.get("filename", "")
    base = re.split(r"[\\/]", path)[-1]
    ext = (f.get("extension") or base.rsplit(".", 1)[-1]).lower().lstrip(".")
    if ext == "flac":
        if not opts.flac:
            return None, "flac desactivado"
    elif ext != "mp3":
        return None, "formato no aceptado"
    else:
        if f.get("isVariableBitRate"):
            return None, "vbr"
        br = effective_bitrate(f)
        if br is None or br < opts.min_bitrate - 4:
            return None, f"bitrate {br}"

    length = f.get("length")
    if length and length < 100:
        return None, "preview/corto"

    fb = fold(re.sub(r"\.(mp3|flac)$", "", base, flags=re.I))
    fp = fold(path)

    # título: todas las palabras significativas deben estar en el nombre del archivo
    tw = [w for w in words(track["title"]) if w not in STOP] or words(track["title"])
    missing = [w for w in tw if not has_phrase(fb, w)]
    if len(missing) > (1 if len(tw) >= 4 else 0):
        return None, f"título no coincide ({missing})"
    score = 50 - 10 * len(missing)

    # el título debe aparecer como frase seguida, no con las palabras sueltas
    # ("Beat On Time" ≠ "axis_of_time-jump_on_the_beat")
    sq = lambda x: re.sub(r"[^a-z0-9]", "", fold(x))
    if sq(track["title"]) and sq(track["title"]) not in sq(fb):
        if not track["artists"]:
            return None, "título no aparece como frase"
        score -= 30

    pw = track.get("partial_word")
    if pw and re.search(rf"(?:^| ){re.escape(fold(pw))}", fb):
        score += 5

    # artista: al menos uno de los artistas debe aparecer en la ruta completa
    if track["artists"]:
        hits = [a for a in track["artists"] if has_phrase(fp, a)]
        if not hits:
            return None, "artista no aparece"
        score += 5 * min(len(hits), 3)

    hint, remixer = track.get("mix_hint"), track.get("remixer")
    title_f = fold(track["title"])

    def present(word):
        return has_phrase(fb, word) and not has_phrase(title_f, word)

    if hint == "remix" and remixer:
        rw = [w for w in words(remixer) if len(w) > 1]
        if rw and not all(has_phrase(fb, w) for w in rw):
            return None, "remixer no coincide"
        score += 25
    elif present("remix") or present("rmx") or present("bootleg") or present("rework"):
        score -= 30  # no pediste remix y este lo es

    for bad in ("acapella", "a cappella", "instrumental", "karaoke", "live", "intro", "clean", "tool"):
        if present(bad) and bad not in fold(track.get("mix") or ""):
            score -= 40

    if hint in ("extended", "original", None):
        if present("extended"):
            score += 15 if hint == "extended" else 5
        if present("original mix"):
            score += 10 if hint in ("extended", "original") else 5
        if present("radio edit") or present("radio") or present("edit"):
            score -= 25 if hint == "extended" else 10
        if hint == "extended" and length and length < 210:
            score -= 20
    elif hint == "radio" and present("radio"):
        score += 10
    elif hint and present(hint):
        score += 10

    # palabras de sobra en el nombre (otra canción, otro artista…)
    expected = set(words(track["title"])) | set(w for a in track["artists"] for w in words(a))
    expected |= set(words(track.get("mix") or "")) | {"mix", "extended", "original", "feat", "ft", "remix"}
    extra = [w for w in fb.split() if w not in expected and not w.isdigit()]
    score -= min(len(extra), 10)

    # calidad del usuario
    if resp.get("hasFreeUploadSlot"):
        score += 10
    score -= min(resp.get("queueLength", 0) or 0, 50) / 5
    score += min((resp.get("uploadSpeed", 0) or 0) / 1_000_000 * 4, 10)

    return score, "ok"


def reason_key(why):
    if why.startswith("bitrate"):
        return f"bitrate menor a {MIN_BR[0]} kbps"
    return {"formato no aceptado": "formato no aceptado (M4A, WAV, OGG…)",
            "flac desactivado": "FLAC (desactivado con --no-flac)", "vbr": "MP3 VBR (no 320 fijo)",
            "preview/corto": "preview de menos de 100 s"}.get(why, why.split(" (")[0])


MIN_BR = [320]


def rank(track, responses, opts, explain=None):
    MIN_BR[0] = opts.min_bitrate
    cands = []
    for resp in responses:
        for f in resp.get("files", []) or []:
            sc, why = score_file(track, resp, f, opts)
            if explain is not None:
                if sc is None:
                    explain["reasons"][reason_key(why)] += 1
                    # guardar ejemplos relevantes: que al menos contengan el título
                    base = re.split(r"[\\/]", f.get("filename", ""))[-1]
                    tw = [w for w in words(track["title"]) if w not in STOP]
                    if tw and all(has_phrase(fold(base), w) for w in tw) and len(explain["samples"]) < 8:
                        br = effective_bitrate(f)
                        explain["samples"].append(f"{base}  [{int(br) if br else '?'} kbps] → {why}")
                elif sc < opts.min_score:
                    explain["reasons"][f"puntaje menor a {opts.min_score}"] += 1
            if sc is not None and sc >= opts.min_score:
                fname = f.get("filename") or ""
                cands.append({
                    "format": "flac" if fname.lower().endswith(".flac") or
                              (f.get("extension") or "").lower().lstrip(".") == "flac" else "mp3",
                    "score": round(sc, 1),
                    "username": resp.get("username"),
                    "filename": f.get("filename"),
                    "size": f.get("size"),
                    "bitrate": effective_bitrate(f),
                    "length": f.get("length"),
                })
    cands.sort(key=lambda c: c["score"], reverse=True)

    def top(items, n):
        # una sola opción por usuario para tener alternativas reales si alguien falla
        seen, out = set(), []
        for c in items:
            if c["username"] not in seen:
                seen.add(c["username"])
                out.append(c)
            if len(out) >= n:
                break
        return out

    return (top([c for c in cands if c["format"] == "mp3"], MAX_CANDIDATES),
            top([c for c in cands if c["format"] == "flac"], MAX_FLAC))


# ───────────────────────── estado ─────────────────────────

def load_state(tracks_path):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {}
    with open(tracks_path, encoding="utf-8") as fh:
        tracks = json.load(fh)
    for t in tracks:
        if t["key"] not in state:
            state[t["key"]] = {"track": t, "status": "pending", "candidates": [], "tried": [], "current": None}
        else:
            state[t["key"]]["track"] = t  # por si se re-normalizó
    # entradas de una limpieza anterior de la lista que ya no existen en tracks.json:
    # se descartan, salvo las ya descargadas o en curso (esas archivos son reales)
    current = {t["key"] for t in tracks}
    stale = [k for k in state if k not in current]
    dropped = 0
    for k in stale:
        if state[k]["status"] in ("downloaded", "queued"):
            state[k]["stale"] = True
        else:
            del state[k]
            dropped += 1
    if dropped:
        print(f"(se descartaron {dropped} entradas de una versión anterior de la lista)", file=sys.stderr)
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def label(t):
    a = ", ".join(t["artists"])
    s = f"{a} - {t['title']}" if a else t["title"]
    return s + (f" ({t['mix']})" if t.get("mix") else "")


def enqueue_next(cli, entry, dry):
    tried = {(c["username"], c["filename"]) for c in entry["tried"]}
    for c in entry["candidates"]:
        if (c["username"], c["filename"]) in tried:
            continue
        if not dry:
            try:
                cli.enqueue(c["username"], c["filename"], c["size"])
            except requests.HTTPError as e:
                entry["tried"].append({**c, "error": str(e)})
                continue
        entry["tried"].append(c)
        entry["current"] = c
        entry["status"] = "queued" if not dry else "dry_run"
        return True
    entry["current"] = None
    entry["status"] = "failed" if entry["tried"] else "not_found"
    return False


# ───────────────────────── comandos ─────────────────────────

def cmd_search(cli, state, opts):
    todo = [e for e in state.values() if e["status"] == "pending"]
    if not opts.include_ambiguous:
        skipped = [e for e in todo if e["track"]["ambiguous"]]
        for e in skipped:
            e["status"] = "skipped_ambiguous"
        todo = [e for e in todo if not e["track"]["ambiguous"]]
    if opts.only:
        todo = [e for e in state.values() if e["track"]["index"] in set(opts.only)
                and e["status"] in ("pending", "skipped_ambiguous", "not_found", "dry_run")]
    if opts.limit:
        todo = todo[: opts.limit]
    print(f"Buscando {len(todo)} canciones…\n")

    for i, e in enumerate(todo, 1):
        t = e["track"]
        print(f"[{i}/{len(todo)}] {label(t)}")
        cands, flacs = [], []
        for q in t["queries"]:
            try:
                responses = cli.search(q, opts.search_timeout * 1000)
            except requests.RequestException as ex:
                print(f"    ! error en búsqueda '{q}': {ex}")
                time.sleep(opts.delay)
                continue
            from collections import Counter
            nfiles = sum(len(r.get("files") or []) for r in responses)
            ex = {"reasons": Counter(), "samples": []}
            cands, fl = rank(t, responses, opts, ex)
            seen_f = {(c["username"], c["filename"]) for c in flacs}
            flacs = sorted(flacs + [c for c in fl if (c["username"], c["filename"]) not in seen_f],
                           key=lambda c: c["score"], reverse=True)[:MAX_FLAC]
            extra = f" (+{len(fl)} FLAC)" if fl and not cands else ""
            print(f"    q='{q}'  →  {len(responses)} usuarios, {nfiles} archivos, "
                  f"{len(cands)} candidatos MP3 320{extra}")
            if (not cands and not fl and nfiles) or opts.explain:
                top = ", ".join(f"{k}: {v}" for k, v in ex["reasons"].most_common(4))
                if top:
                    print(f"      descartados → {top}")
                for smp in ex["samples"][: (8 if opts.explain else 3)]:
                    print(f"        · {smp}")
            if opts.explain and (cands or fl):
                for c in cands + fl:
                    name = re.split(r"[\\/]", c["filename"])[-1]
                    print(f"        ✓ {c['score']:>6}  {name}  [{c['username']}]")
            time.sleep(opts.delay)
            if cands:
                break
        # FLAC al final de la cola: se usa si no hubo MP3, o si fallan todos los MP3
        e["candidates"] = cands + flacs
        cands = e["candidates"]
        e["searched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if cands:
            enqueue_next(cli, e, opts.dry_run)
            c = e["current"]
            if c:
                name = re.split(r"[\\/]", c["filename"])[-1]
                q_txt = "FLAC" if c.get("format") == "flac" else f"{int(c['bitrate'] or 0)} kbps"
                print(f"    ✓ {'(simulado) ' if opts.dry_run else ''}{name}  "
                      f"[{c['username']}, {q_txt}, score {c['score']}]")
        else:
            e["status"] = "not_found"
            print("    ✗ sin resultados válidos")
        save_state(state)


def cmd_sync(cli, state, opts, quiet=False):
    try:
        users = cli.downloads()
    except requests.RequestException as ex:
        print(f"! no pude leer las descargas: {ex}")
        return 0
    status = {}
    for u in users:
        for d in u.get("directories", []) or []:
            for f in d.get("files", []) or []:
                status[(u.get("username"), f.get("filename"))] = f.get("state", "")

    in_flight = changed = 0
    for e in state.values():
        if e["status"] != "queued" or not e["current"]:
            continue
        c = e["current"]
        st = status.get((c["username"], c["filename"]))
        if st is None:
            in_flight += 1  # aún no aparece (o ya se limpió de la lista)
            continue
        if "Succeeded" in st:
            e["status"] = "downloaded"
            changed += 1
            if not quiet:
                print(f"  ✓ {label(e['track'])}")
        elif any(x in st for x in FAIL_STATES):
            changed += 1
            if not quiet:
                print(f"  ✗ {label(e['track'])}  ({st}) → probando siguiente candidato")
            if not enqueue_next(cli, e, False) and not quiet:
                print("      sin más candidatos")
            if e["status"] == "queued":
                in_flight += 1
        else:
            in_flight += 1
    save_state(state)
    if not quiet and (changed or not getattr(opts, "_watching", False)
                      or time.time() - getattr(opts, "_last_beat", 0) > 600):
        opts._last_beat = time.time()
        print(f"[{time.strftime('%H:%M')}] En curso: {in_flight}   cambios: {changed}")
    return in_flight


def cmd_run(cli, state, opts):
    cmd_search(cli, state, opts)
    if opts.dry_run:
        return
    print("\nVigilando descargas (Ctrl+C para salir; se puede retomar con 'sync')…")
    opts._watching = True
    while True:
        n = cmd_sync(cli, state, opts, quiet=False)
        if n == 0:
            break
        time.sleep(opts.watch)
    cmd_report(state, opts)


def cmd_report(state, opts):
    from collections import Counter
    c = Counter(e["status"] for e in state.values())
    nflac = sum(1 for e in state.values()
                if e["status"] in ("downloaded", "queued") and (e.get("current") or {}).get("format") == "flac")
    print("\nResumen:")
    for k in ("downloaded", "queued", "dry_run", "pending", "not_found", "failed", "skipped_ambiguous"):
        if c.get(k):
            print(f"  {k:18} {c[k]}")
    if nflac:
        print(f"  (de esas, en FLAC: {nflac})")
    missing = [e for e in state.values() if e["status"] in ("not_found", "failed", "skipped_ambiguous")]
    missing.sort(key=lambda e: e["track"]["index"])
    with open(opts.report_file, "w", encoding="utf-8") as fh:
        fh.write(f"NO ENCONTRADAS (ni MP3 320 ni FLAC) — {time.strftime('%Y-%m-%d %H:%M')}\n")
        fh.write(f"PENDIENTES: {len(missing)}\n\n")
        for i, e in enumerate(missing, 1):
            t = e["track"]
            why = {"not_found": "sin resultados", "failed": "fallaron todos los candidatos",
                   "skipped_ambiguous": "ambigua, revisar a mano"}[e["status"]]
            kb = f" | actual: {t['current_kbps']} kbps" if t.get("current_kbps") else ""
            fh.write(f"{i:03d}. {t['original']}{kb} | motivo: {why}\n")
    print(f"\n→ {opts.report_file} ({len(missing)} canciones)")


GROUPS = {
    "descargadas": ("downloaded",),
    "en_curso":    ("queued",),
    "sin_buscar":  ("pending", "dry_run"),
    "faltantes":   ("not_found", "failed", "skipped_ambiguous"),
}
GROUP_TITLES = {
    "descargadas": "DESCARGADAS",
    "en_curso":    "EN CURSO (en cola o bajando en slskd)",
    "sin_buscar":  "SIN BUSCAR TODAVÍA",
    "faltantes":   "FALTANTES (no encontradas / fallidas / ambiguas)",
}


def cmd_list(state, opts):
    """Lista canción por canción, agrupada por estado. Solo lee state.json."""
    want = opts.filter or "todo"
    if want not in GROUPS and want != "todo":
        sys.exit(f"Filtro desconocido '{want}'. Usa: todo, " + ", ".join(GROUPS))
    entries = sorted(state.values(), key=lambda e: e["track"]["index"])
    counts = {g: sum(e["status"] in st for e in entries) for g, st in GROUPS.items()}
    print("  ".join(f"{g}: {n}" for g, n in counts.items()) + f"   (total {len(entries)})")
    why = {"not_found": "no encontrada", "failed": "fallaron todos los candidatos",
           "skipped_ambiguous": "ambigua (revisar a mano)"}
    for g, sts in GROUPS.items():
        if want not in ("todo", g):
            continue
        rows = [e for e in entries if e["status"] in sts]
        if not rows:
            continue
        print(f"\n── {GROUP_TITLES[g]}: {len(rows)} ──")
        for e in rows:
            t, c = e["track"], e.get("current") or {}
            line = f"{t['index']:>4}. {label(t)}"
            if g in ("descargadas", "en_curso") and c:
                fmt = "FLAC" if c.get("format") == "flac" else f"MP3 {int(c.get('bitrate') or 0)}"
                name = re.split(r"[\\/]", c.get("filename", ""))[-1]
                line += f"\n        [{fmt}] {name}"
                if g == "en_curso":
                    line += f"  (usuario {c.get('username')}, intento {len(e.get('tried', []))})"
            elif g == "faltantes":
                line += f"   → {why.get(e['status'], e['status'])}"
            print(line)


def cmd_reset(state, opts):
    n = 0
    for e in state.values():
        if e["status"] in ("not_found", "failed", "skipped_ambiguous", "dry_run"):
            e.update(status="pending", candidates=[], tried=[], current=None)
            n += 1
    save_state(state)
    print(f"{n} canciones vueltas a 'pending'.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["search", "sync", "run", "report", "reset", "list"])
    ap.add_argument("filter", nargs="?", help="para 'list': todo | descargadas | en_curso | sin_buscar | faltantes")
    ap.add_argument("--tracks", default="tracks.json")
    ap.add_argument("--dry-run", action="store_true", help="buscar y elegir, pero no encolar nada")
    ap.add_argument("--explain", action="store_true", help="mostrar motivos de descarte y todos los candidatos")
    ap.add_argument("--only", type=int, nargs="+", help="procesar solo estos números de la lista original")
    ap.add_argument("--limit", type=int, help="procesar solo N canciones (para probar)")
    ap.add_argument("--include-ambiguous", action="store_true", help="buscar también las sin artista/título corto")
    ap.add_argument("--min-bitrate", type=int, default=320)
    ap.add_argument("--no-flac", dest="flac", action="store_false",
                    help="no aceptar FLAC como respaldo cuando no hay MP3 320")
    ap.add_argument("--min-score", type=float, default=20)
    ap.add_argument("--search-timeout", type=int, default=15, help="segundos por búsqueda en slskd")
    ap.add_argument("--delay", type=float, default=3, help="pausa entre búsquedas (no saturar la red)")
    ap.add_argument("--watch", type=int, default=60, help="segundos entre revisiones en 'run'")
    ap.add_argument("--report-file", default="no_encontradas.txt")
    opts = ap.parse_args()

    state = load_state(opts.tracks)
    if opts.command == "report":
        return cmd_report(state, opts)
    if opts.command == "reset":
        return cmd_reset(state, opts)
    if opts.command == "list":
        return cmd_list(state, opts)

    url = os.environ.get("SLSKD_URL", "http://localhost:5030")
    key = os.environ.get("SLSKD_API_KEY")
    if not key:
        print("Aviso: SLSKD_API_KEY no está definida.", file=sys.stderr)
    cli = Slskd(url, key)
    if not cli.wait_ready():
        sys.exit(1)
    try:
        {"search": cmd_search, "sync": cmd_sync, "run": cmd_run}[opts.command](cli, state, opts)
    except KeyboardInterrupt:
        save_state(state)
        print("\nInterrumpido. Progreso guardado en state.json.")


if __name__ == "__main__":
    main()
