#!/usr/bin/env python3
"""
normalize.py — Limpia una lista .txt de canciones y genera tracks.json
listo para buscar en Soulseek.

Uso:
    python3 normalize.py lista.txt -o tracks.json [--csv revision.csv]

Acepta líneas como:
    001. Artista, Otro & X - Título (Extended Mix) | actual: 128.4 kbps
    Título suelto [rIG9f2iqOos]
    Artista - Título truncado (Exte
"""
import argparse
import csv
import json
import re
import sys
import unicodedata

TRUNC_LEN = 44  # el origen de tu lista corta los nombres a 44 caracteres

LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.*?)\s*(?:\|\s*actual:\s*([\d.]+)\s*kbps)?\s*$", re.I)
YT_ID_RE = re.compile(r"\[[A-Za-z0-9_-]{11}\]")
UNCLOSED_RE = re.compile(r"[\(\[][^\)\]]*$")
NOISE_RE = re.compile(
    r"[\(\[]\s*(?:official\s*)?(?:audio|video|music video|full stream|visuali[sz]er|lyrics?|lyric video|hq|hd)"
    r"(?:\s*video)?\s*[\)\]]",
    re.I,
)
NOISE_INNER_RE = re.compile(r"\s*-\s*official\s+(?:audio|video|full stream)", re.I)
GENRES = {
    "tech house", "house", "techno", "deep house", "afro house", "bass house", "melodic house",
    "progressive house", "minimal", "edm", "dance", "electronic", "garage", "uk garage",
    "jackin house", "latin house", "g house", "big room", "trance", "drum and bass", "dnb",
}
MIX_WORDS = re.compile(r"\b(mix|remix|edit|rework|version|dub|bootleg|flip|vip|extended|club|rmx)\b", re.I)
FEAT_RE = re.compile(r"^(?:feat\.?|ft\.?|featuring|with)\s+(.+)$", re.I)
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\band\b)\s*", re.I)
# apóstrofes convertidos en "_" por el sistema de archivos: Don_t, She_s, Talkin_
APOS_RE = re.compile(r"(?<=[A-Za-z])_(?=(?:s|t|ll|re|ve|d|m)\b)|(?<=in)_(?=\s|$)", re.I)
STOP = {"the", "a", "an", "of", "and", "y", "el", "la", "de", "mix"}


def fold(s: str) -> str:
    """minúsculas, sin acentos, solo alfanumérico separado por espacios."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def query_tokens(s: str) -> list:
    """Palabras aptas para buscar en Soulseek. Los clientes parten los nombres
    por signos, así que hacemos lo mismo: "Don't"→"Don", "2.1"→(nada), "PAULY!"→"PAULY"."""
    s = unicodedata.normalize("NFC", s or "")
    return [w for w in re.sub(r"[^\w]+", " ", s, flags=re.U).split() if len(w) >= 2]


def fix_mojibake(s: str) -> str:
    """'HÃ¸rger' → 'Hørger' (UTF-8 leído como Latin-1)."""
    if "Ã" in s or "Â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def classify_mix(mix: str):
    """Devuelve (hint, remixer) a partir del texto entre paréntesis."""
    m = mix.lower()
    if not m:
        return None, None
    if re.search(r"\b(remix|rmx|rework|bootleg|flip)\b", m):
        remixer = re.sub(r"\b(extended|remix|rmx|rework|bootleg|flip|mix|edit|official|dub)\b", " ", mix, flags=re.I)
        remixer = re.sub(r"\s+", " ", remixer).strip(" -")
        return "remix", remixer or None
    if m.startswith("ext"):
        return "extended", None
    if "original" in m:
        return "original", None
    if "radio" in m:
        return "radio", None
    if "club" in m:
        return "club", None
    if "dub" in m:
        return "dub", None
    if re.search(r"\bmix\b|\bversion\b|\bedit\b", m):
        # "(Space Train Mix)" → mix con nombre, se trata como remix de ese nombre
        name = re.sub(r"\b(mix|version|edit)\b", " ", mix, flags=re.I).strip()
        return ("remix", name) if name else (None, None)
    return None, None


def parse_line(raw: str):
    m = LINE_RE.match(raw)
    if not m:
        return None
    idx, text, kbps = m.group(1), m.group(2), m.group(3)
    original = text
    truncated = len(text) == TRUNC_LEN and not text.rstrip().endswith((")", "]"))

    t = fix_mojibake(unicodedata.normalize("NFC", text))
    t = YT_ID_RE.sub(" ", t)
    t = APOS_RE.sub("'", t)
    t = t.replace("_", " ")
    t = re.sub(r"-\d+$", "", t.strip())        # "Don't Stop-2"
    t = NOISE_INNER_RE.sub("", t)
    t = NOISE_RE.sub(" ", t)

    # fragmento final sin cerrar: "(Exte", "[6FDA2m2_veA"
    trunc_frag = ""
    partial = ""  # última palabra posiblemente cortada: "Ca" de "Don't Ca[re]"
    um = UNCLOSED_RE.search(t)
    if um:
        trunc_frag = um.group(0)[1:].strip()
        t = t[: um.start()]
        truncated = True
    elif truncated:
        # cortado a mitad de palabra: quitamos la última palabra parcial
        parts = t.rsplit(" ", 1)
        if len(parts) == 2 and len(parts[0]) > 3 and " - " in parts[0]:
            partial = parts[1]
            t = parts[0]

    # separar artista / título
    cc = re.match(r"^(.+?)\s*\(\s*[A-Z]{2}\s*\)\s*([^\s(\[].*)$", t.strip())
    if " - " in t:
        artist_part, title_part = t.split(" - ", 1)
    elif cc:  # "Marian ( BR )Aint No Way"
        artist_part, title_part = cc.group(1), cc.group(2)
    elif re.search(r"\S {2,}[^\s(\[]", t):  # "ESSE  Make My Day"
        artist_part, title_part = re.split(r" {2,}(?=[^\s(\[])", t.strip(), 1)
    else:
        artist_part, title_part = "", t

    # paréntesis / corchetes del título
    mixes, feats = [], []
    for grp in re.findall(r"[\(\[]([^\)\]]*)[\)\]]", title_part):
        g = grp.strip()
        if not g:
            continue
        fm = FEAT_RE.match(g)
        if fm:
            feats.append(fm.group(1))
        elif fold(g) in GENRES:
            pass
        elif MIX_WORDS.search(g) or "original" in g.lower():
            mixes.append(g)
    core = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title_part)
    core = re.sub(r"\s+", " ", core).strip(" -")
    if trunc_frag and trunc_frag.lower().startswith(("feat", "ft.", "ft ", "with")):
        trunc_frag = ""

    # artistas
    artists = [a.strip() for a in ARTIST_SPLIT_RE.split(artist_part) if a and a.strip()]
    artists = [re.sub(r"\(\s*[A-Z]{2}\s*\)", "", a).strip() for a in artists]  # "SOLTO (FR)"
    artists = [a for a in artists if fold(a)]

    mix = mixes[0] if mixes else ""
    hint, remixer = classify_mix(mix)
    if not hint and trunc_frag:
        # pista del fragmento truncado: "Exte" → extended, "Odd Mob Rem" → remix
        f = trunc_frag.lower()
        if f.startswith("ext"):
            hint = "extended"
        elif f.startswith("orig"):
            hint = "original"
        elif re.search(r"\brem", f) or re.search(r"\brmx", f):
            hint, remixer = "remix", re.sub(r"\brem\w*|\brmx", "", trunc_frag, flags=re.I).strip() or None

    title_tokens = [w for w in fold(core).split() if w]
    no_artist = not artists
    sig_title = [w for w in title_tokens if w not in STOP]
    ambiguous = no_artist and (len(sig_title) < 2 or len(fold(core)) < 6)

    # consultas de búsqueda, de la más específica a la más amplia
    q_title = query_tokens(core)
    q_partial = query_tokens(partial) if len(partial) >= 3 else []
    q_artist = query_tokens(artists[0]) if artists else []
    q_remixer = query_tokens(remixer)[:2] if remixer else []
    if not q_title and artists:
        # título no buscable ("1,2,3"): reforzamos con el 2º artista y la versión
        extra = [w for a in artists[1:2] for w in query_tokens(a)]
        q_artist = q_artist + extra + (["extended"] if hint == "extended" else [])
    queries = []
    if q_partial:  # primero probamos asumiendo que la palabra está completa
        queries.append((q_artist[:2] if q_artist else []) + q_title + q_partial)
    if q_artist:
        if q_remixer:
            queries.append(q_artist + q_title + q_remixer)
            queries.append(q_title + q_remixer)
        queries.append(q_artist + q_title)
        if len(q_title) >= 2 and not q_remixer:
            queries.append(q_title + (["extended"] if hint == "extended" else []))
    else:
        if q_remixer:
            queries.append(q_title + q_remixer)
        queries.append(q_title)
    seen, uniq = set(), []
    for q in queries:
        s = " ".join(q).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)

    key = "|".join([
        " ".join(sorted(fold(a) for a in artists)),
        fold(core),
        hint or "",
        fold(remixer or ""),
    ])

    return {
        "key": key,
        "index": int(idx),
        "original": original,
        "artists": artists,
        "featuring": feats,
        "title": core,
        "partial_word": partial or None,
        "mix": mix or (trunc_frag if hint else ""),
        "mix_hint": hint,
        "remixer": remixer,
        "truncated": truncated,
        "no_artist": no_artist,
        "ambiguous": ambiguous,
        "current_kbps": float(kbps) if kbps else None,
        "queries": uniq,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="tracks.json")
    ap.add_argument("--csv", help="CSV para revisar a mano el resultado")
    args = ap.parse_args()

    tracks, merged = {}, 0
    with open(args.input, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            t = parse_line(raw.rstrip("\n"))
            if not t or not t["queries"]:
                continue
            prev = tracks.get(t["key"])
            if prev:
                merged += 1
                # nos quedamos con la versión no truncada
                if prev["truncated"] and not t["truncated"]:
                    tracks[t["key"]] = t
                continue
            tracks[t["key"]] = t

    out = sorted(tracks.values(), key=lambda x: x["index"])
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["#", "original", "artistas", "titulo", "mix", "hint", "remixer",
                        "truncado", "sin_artista", "ambiguo", "busquedas"])
            for t in out:
                w.writerow([t["index"], t["original"], "; ".join(t["artists"]), t["title"], t["mix"],
                            t["mix_hint"] or "", t["remixer"] or "", t["truncated"], t["no_artist"],
                            t["ambiguous"], " || ".join(t["queries"])])

    n = len(out)
    print(f"Canciones únicas: {n}  (duplicados fusionados: {merged})", file=sys.stderr)
    print(f"  truncadas:   {sum(t['truncated'] for t in out)}", file=sys.stderr)
    print(f"  sin artista: {sum(t['no_artist'] for t in out)}", file=sys.stderr)
    print(f"  ambiguas (se omiten por defecto al buscar): {sum(t['ambiguous'] for t in out)}", file=sys.stderr)
    print(f"→ {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
