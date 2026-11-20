"""
Build the GitHub Pages voice showcase into docs/ (index.html + docs/audio/*.mp3).

Only voices and lines we really generated are used: every sample is a finished
Drama 3 take from pst_revoice/audio_fish/ (speech by the character only, no
narration), paired with the original voice it was cloned from.

    python build_showcase.py            # rebuild docs/
    python build_showcase.py --list     # show which candidate voices have generated speech

GitHub: Settings -> Pages -> Deploy from branch -> main, folder /docs.
"""

import argparse
import csv
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT                      # index.html at the repo root: Pages serves from "/ (root)" so it can play repo files
AUDIO = ROOT / "docs" / "audio"   # small mp3s of generated takes (the full-quality WAVs are not in the repo)
FISH = ROOT / "pst_revoice" / "audio_fish"
GENERATION = FISH / "generation.json"
PLAN = ROOT / "pst_revoice" / "plan.csv"
NWN_DIR = ROOT / "voice_library" / "nwn"
PST_ORIG = ROOT / "voices_pst"
FFMPEG = "ffmpeg"

# ==================== CONFIGURATION ====================
SAMPLES_PER_VOICE = 3
MIN_SECONDS, MAX_SECONDS = 3.0, 16.0
MIN_WORDS = 6
# Source voices to showcase, in page order. NWN ids are voice_library ids. A voice is listed only if it has
# generated speech (see --list); wanted-but-missing ones are reported on stdout.
WANTED_NWN = ["JalekM_PCV", "PreludeM1", "ClericM3", "AvHen4M"]   # asked for; skipped automatically if never generated
NWN_VOICES = ["SuccF", "AvHen5M", "SirilM_PCV"]   # SuccF replaces AvHen6M (Boddyknock Glinckle)
# Lines to always include for a voice (StrRef), e.g. one with a [chuckles] event tag
FEATURED = {"SirilM_PCV": ["12589"]}
PST_VOICES = ["Ravel", "The Transcendent One"]
# =======================================================


def mp3(src: Path, dst: Path, start: float = 0.0, seconds: float = 0.0) -> None:
    """Encode src to a small mono mp3 (skipped when dst already exists)."""
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "32000", "-b:a", "80k"]
    if seconds:
        cmd += ["-ss", str(start), "-t", str(seconds)]
    subprocess.run(cmd + [str(dst)], check=True)


def repo_url(path: Path) -> str:
    """Repo-relative, URL-encoded path of a source file the page plays directly."""
    from urllib.parse import quote
    return quote(path.relative_to(ROOT).as_posix())


def load_inputs():
    gen = json.loads(GENERATION.read_text(encoding="utf-8"))
    plan = {}
    with open(PLAN, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            plan[row["StrRef"]] = row
    catalog = {v["id"]: v for v in json.loads((NWN_DIR / "catalog.json").read_text(encoding="utf-8"))}
    return gen, plan, catalog


WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
QUOTE = re.compile(r'"([^"]+)"')
TAGS = re.compile(r"\[[^\[\]]*\]|</?[a-z_]+>")
GAP_SECONDS = 0.25


def words(text: str) -> list:
    return WORD.findall(TAGS.sub(" ", text).lower().replace("’", "'").replace("*", " "))


def speech_runs(entry: dict, voice_id: str):
    """Runs of consecutive segments spoken by this voice (narration excluded), clean first-attempt takes only.

    Yields (indices, raw_text): raw_text is the game's own quoted dialogue that matches the run.
    """
    segs = entry.get("segments") or []
    quotes = QUOTE.findall(entry.get("spoken", "")) or [entry.get("spoken", "")]   # unquoted lines are all speech
    run = []
    for i, seg in enumerate(segs + [None]):
        ok = (seg is not None and seg.get("voice") == voice_id
              and seg.get("source") in ("director", "director-direction-only", "mood") and seg.get("attempts") == ["ok"])
        if ok:
            run.append(i)
            continue
        if run:
            run_words = set(w for k in run for w in words(segs[k]["text"]))
            hits = [q for q in quotes if words(q) and len(set(words(q)) & run_words) / len(set(words(q))) >= 0.7]
            covered = set(w for q in hits for w in words(q))
            if hits and len(run_words & covered) / max(1, len(run_words)) >= 0.7:
                yield run, " ".join(hits)
        run = []


def candidates(gen: dict, plan: dict, voice_id: str, speaker: str = ""):
    """Generated speech-only runs for a voice: [(strref, entry, indices, seconds, raw)]."""
    out = []
    for strref, entry in gen.items():
        if speaker and entry.get("speaker") != speaker:
            continue
        if voice_id not in {s.get("voice") for s in entry.get("segments", [])}:
            continue
        for run, raw in speech_runs(entry, voice_id):
            files = [FISH / "segments" / f"VV{strref}_{i}.wav" for i in run]
            secs = sum(entry["segments"][i].get("seconds", 0) for i in run)
            if all(f.exists() for f in files) and MIN_SECONDS <= secs <= MAX_SECONDS and len(words(raw)) >= MIN_WORDS:
                out.append((strref, entry, run, secs, raw))
    return out


def pick(cands, n, key=lambda c: c[1]["speaker"]):
    """Pick n runs: one per group (character) first, most-voiced group first, median length; then fill."""
    groups = {}
    for c in cands:
        groups.setdefault(key(c), []).append(c)
    chosen = []
    for g in sorted(groups, key=lambda g: -len(groups[g])):
        lst = sorted(groups[g], key=lambda c: c[3])
        chosen.append(lst[len(lst) // 2])
        if len(chosen) == n:
            return chosen
    rest = [c for c in sorted(cands, key=lambda c: c[3]) if c not in chosen]
    step = max(1, len(rest) // max(1, n - len(chosen)))
    return chosen + rest[::step][: n - len(chosen)]


def sample(strref, entry, run, secs, raw, plan):
    import numpy as np
    import soundfile as sf
    name = f"VV{strref}_{'-'.join(str(i) for i in run)}"
    dst = AUDIO / "gen" / f"{name}.mp3"
    if not dst.exists():
        parts, rate = [], None
        for i in run:
            data, rate = sf.read(FISH / "segments" / f"VV{strref}_{i}.wav", dtype="float32")
            parts += [data if data.ndim == 1 else data.mean(axis=1), np.zeros(int(GAP_SECONDS * rate), "float32")]
        tmp = AUDIO / "_tmp.wav"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        sf.write(tmp, np.concatenate(parts[:-1]), rate)
        mp3(tmp, dst)
        tmp.unlink()
    segs = [entry["segments"][i] for i in run]
    return {
        "character": entry["speaker"],
        "file": ", ".join(f"VV{strref}_{i}.wav" for i in run),
        "strref": strref,
        "audio": f"docs/audio/gen/{name}.mp3",
        "raw": raw,
        "segments": [{"text": g["text"], "seed": g.get("seed"), "seconds": g.get("seconds"),
                      "reference": g.get("reference", "")} for g in segs],
        "seconds": round(secs, 1),
        "mood": entry.get("mood", ""),
        "director": entry.get("director", ""),
        "generated_at": entry.get("generated_at", "")[:10],
    }


def nwn_card(voice_key, gen, plan, catalog):
    vid = f"nwn:{voice_key}"
    cat = catalog.get(vid)
    cands = candidates(gen, plan, vid)
    if not cat or not cands:
        return None
    clips = [{"label": "Cloning reference (what Drama 3 heard)", "text": cat["reference_text"],
              "src": NWN_DIR / voice_key / "reference.wav", "name": "reference.wav"}]
    longest = max(cat["clips"], key=lambda c: c["seconds"])
    clips.append({"label": f"Original clip: {longest['slot']}", "text": longest["text"],
                  "src": NWN_DIR / voice_key / longest["file"], "name": longest["file"]})
    originals = []
    for i, clip in enumerate(clips):
        originals.append({"label": clip["label"], "text": clip["text"], "file": f"{voice_key}/{clip['name']}",
                          "audio": repo_url(clip["src"])})
    feat = [c for c in cands if c[0] in FEATURED.get(voice_key, [])]
    chosen = feat + pick([c for c in cands if c not in feat], SAMPLES_PER_VOICE - len(feat))
    return {"id": vid, "group": "nwn", "title": cat["name"], "subtitle": f"Neverwinter Nights sound set · {vid}",
            "originals": originals, "samples": [sample(*c, plan) for c in chosen], "line_count": len(cands)}


def pst_card(name, gen, plan):
    cands = candidates(gen, plan, name, speaker=name)
    if not cands:
        return None
    slug = re.sub(r"\W+", "_", name).strip("_")
    originals = []
    for i, wav in enumerate(sorted(PST_ORIG.glob(f"{name} *.WAV"))):
        txt = wav.with_suffix(".txt")
        originals.append({"label": f"Original game line {i + 1}", "file": wav.name,
                          "text": txt.read_text(encoding="utf-8").strip() if txt.exists() else "",
                          "audio": repo_url(wav)})
    chosen = pick(cands, SAMPLES_PER_VOICE, key=lambda c: c[1].get("mood") or "neutral")
    return {"id": name, "group": "pst", "title": name, "subtitle": "Planescape: Torment · original voice acting, cloned",
            "originals": originals, "samples": [sample(*c, plan) for c in chosen], "line_count": len(cands)}


# ==================== HTML ====================

def esc(s):
    return html.escape(str(s))


def directed_html(segments):
    parts = []
    for seg in segments:
        t = esc(seg["text"])
        t = re.sub(r"^\[(.*?)\]", r'<span class="dir">[\1]</span>', t)
        t = re.sub(r"&lt;(/?\w+)&gt;", r'<span class="tag">&lt;\1&gt;</span>', t)
        parts.append(f'<span class="seg">{t}</span>')
    return " ".join(parts)


CHUCKLE = ' <span class="badge text-bg-warning">has a [chuckles]</span>'


def card_html(card, idx):
    orig = "".join(
        f'<div class="orig mb-2"><div class="small text-secondary">{esc(o["label"])} · <code>{esc(o["file"])}</code></div>'
        f'<audio controls preload="none" src="{o["audio"]}"></audio>'
        f'<div class="small fst-italic">“{esc(o["text"])}”</div></div>' for o in card["originals"])
    samples = []
    for j, s in enumerate(card["samples"]):
        sid = f"s{idx}_{j}"
        params = "".join(
            f'<tr><td>{k + 1}</td><td>{esc(seg["seed"])}</td><td>{esc(seg["seconds"])} s</td><td>{esc(seg["reference"])}</td></tr>'
            for k, seg in enumerate(s["segments"]))
        samples.append(f'''
      <div class="sample border rounded p-3 mb-3">
        <div class="d-flex flex-wrap justify-content-between gap-1 mb-1">
          <strong>{esc(s["character"])}</strong>{CHUCKLE if re.search("chuckl", s["segments"][0]["text"], re.I) else ""}
          <span class="small text-secondary"><code>{esc(s["file"])}</code> · StrRef {esc(s["strref"])} · {esc(s["seconds"])} s</span>
        </div>
        <audio controls preload="none" src="{s["audio"]}"></audio>
        <p class="line mt-2 mb-2" data-directed='{esc(directed_html(s["segments"]))}' data-raw='{esc(s["raw"])}'></p>
        <button class="btn btn-sm btn-outline-primary toggle-line" type="button">Show raw game line</button>
        <a class="btn btn-sm btn-outline-secondary" data-bs-toggle="collapse" href="#{sid}">Take details</a>
        <div class="collapse mt-2" id="{sid}">
          <div class="small text-secondary mb-1">Mood tag: {esc(s["mood"] or "-")} · director: {esc(s["director"] or "-")} · generated {esc(s["generated_at"])}</div>
          <div class="table-responsive"><table class="table table-sm small mb-0">
            <thead><tr><th>Seg</th><th>Seed</th><th>Length</th><th>Voice reference</th></tr></thead>
            <tbody>{params}</tbody></table></div>
        </div>
      </div>''')
    return f'''
  <div class="col-12 col-lg-6 voice" data-group="{card["group"]}">
    <div class="card h-100 shadow-sm">
      <div class="card-body">
        <h2 class="h5 mb-0">{esc(card["title"])}</h2>
        <div class="small text-secondary mb-3">{esc(card["subtitle"])} · {card["line_count"]} clean speech-only lines generated</div>
        <h3 class="h6 text-uppercase text-secondary">Original voice</h3>
        {orig}
        <h3 class="h6 text-uppercase text-secondary mt-4">Generated with Fish Audio Drama 3</h3>
        {"".join(samples)}
      </div>
    </div>
  </div>'''


PAGE = """<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PST Revoice: Voice Showcase</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  audio { width: 100%; }
  .dir { color: #f0b429; font-weight: 600; }
  .tag { color: #6cb6ff; }
  .line { font-size: 1.02rem; }
  .raw { color: var(--bs-body-color); font-style: italic; }
  code { word-break: break-all; }
</style>
</head>
<body>
<nav class="navbar bg-body-tertiary border-bottom"><div class="container">
  <span class="navbar-brand mb-0 h1">PST Revoice · Voice Showcase</span>
</div></nav>

<main class="container py-4">
  <p class="lead">Planescape: Torment re-voiced with <strong>Fish Audio Drama 3</strong>. Each card pairs an original voice (the clip the model cloned) with real takes
  generated for the game. Every line is spoken by that character only (narration excluded). Press <em>Show raw game line</em> to swap the
  directed text Drama 3 received for the untouched game dialogue.</p>

  <div class="row g-4" id="voices">__CARDS__
  </div>

  <section id="workflow" class="mt-5">
    <h2 class="h4">Workflow</h2>
    <div class="row g-3 row-cols-1 row-cols-md-2 row-cols-xl-3">
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-primary mb-2">1</div><h3 class="h6">Extract the game</h3>
        <p class="small mb-0">Dialogue (DLG), creatures (CRE) and strings are dumped from the game files into a dialog report. Every line gets a speaker,
        its text and its original sound file, if it has one. Lines the game already voiced keep their original audio.</p>
      </div></div></div>
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-primary mb-2">2</div><h3 class="h6">Plan &amp; split</h3>
        <p class="small mb-0">Each line is split into spoken speech and narration. Narration goes to a single storyteller voice. Pronunciation rules
        fix invented words and names.</p>
      </div></div></div>
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-primary mb-2">3</div><h3 class="h6">Cast the voices</h3>
        <p class="small mb-0">Companions and key characters are cloned from their original acting. Everyone else is cast by an LLM (Qwen, local) from a character profile
        (age, personality, voice description) onto a library voice from Neverwinter Nights or World of Warcraft.</p>
      </div></div></div>
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-warning mb-2">4</div><h3 class="h6">Add voice direction (Qwen)</h3>
        <p class="small mb-0">A local Qwen3.6 35B-A3B reads the line, its context and the speaker, then writes a short acting direction such as
        <span class="dir">[mocking, sweetly sinister]</span> and marks emphasis. It may not change the words: the result is checked against the original text.</p>
      </div></div></div>
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-success mb-2">5</div><h3 class="h6">Generate with Fish Drama 3</h3>
        <p class="small mb-0">Drama 3 speaks the directed text in the cloned voice. Bad takes (cut off, too short, stalled) are rolled again with a new seed.
        The work is spread across several PCs with their own GPUs.</p>
      </div></div></div>
      <div class="col"><div class="card h-100"><div class="card-body">
        <div class="badge text-bg-secondary mb-2">6</div><h3 class="h6">Review &amp; build the mod</h3>
        <p class="small mb-0">Takes are stitched, silence-trimmed and reviewed, then packaged as a WeiDU mod that replaces the game's voice files.</p>
      </div></div></div>
    </div>
  </section>

  <section id="setup" class="mt-5">
    <h2 class="h4">Fish Drama 3 setup</h2>
    <div class="row g-4">
      <div class="col-12 col-lg-6"><div class="card h-100"><div class="card-body">
        <h3 class="h6 text-uppercase text-secondary">Model &amp; runtime</h3>
        <ul class="mb-0">
          <li>Drama 3 <em>final</em> weights, <strong>Q4_K GGUF</strong> (Drama-35B-A3B-v3), community quant by tunjayoff</li>
          <li>Runtime: <strong>speech.cpp 0.10.1</strong> (Windows, Vulkan), one persistent <code>speech worker</code> per GPU</li>
          <li>Cloning: <code>multi</code> voice mode, one voice per character built from ~10–30 s of reference clips with transcripts</li>
          <li>Direction: a leading <code>[free-form direction]</code>, <code>&lt;emphasis&gt;</code> tags and pronunciation fixes, written per line by a local LLM
              director (Strata, Qwen3.6 35B-A3B Q4_K_M)</li>
          <li>Narration and directed takes are generated separately and stitched; narration is left out of this page</li>
        </ul>
      </div></div></div>
      <div class="col-12 col-lg-6"><div class="card h-100"><div class="card-body">
        <h3 class="h6 text-uppercase text-secondary">Sampling parameters</h3>
        <table class="table table-sm mb-2">
          <tr><td>top_k</td><td>4097</td></tr>
          <tr><td>top_p</td><td>0.9</td></tr>
          <tr><td>temperature</td><td>1.0</td></tr>
          <tr><td>seed</td><td>per segment, reproducible (shown under <em>Take details</em>)</td></tr>
          <tr><td>max_seconds</td><td>3 s + 0.7 s per word (runaway-silence guard)</td></tr>
          <tr><td>retries</td><td>up to 3 takes; truncated, too-short or stalled takes are re-rolled</td></tr>
        </table>
        <p class="small text-secondary mb-0">A small top-k makes Drama 3 stall in silence at sentence ends, hence 4097. Samples here all passed on the first attempt.</p>
      </div></div></div>
      <div class="col-12"><div class="card"><div class="card-body">
        <h3 class="h6 text-uppercase text-secondary">Hardware</h3>
        <div class="table-responsive"><table class="table table-sm mb-2">
          <thead><tr><th>Machine</th><th>GPU</th><th>RAM</th><th>CPU</th></tr></thead>
          <tbody>
            <tr><td>Main PC</td><td>RTX 4060 Ti 16 GB (+ GTX 1660 SUPER 6 GB)</td><td>32 GB</td><td>Intel i7-13700F</td></tr>
            <tr><td>Farm node</td><td>RTX 3070 8 GB</td><td>32 GB</td><td>-</td></tr>
            <tr><td>Farm node</td><td>RTX 4070 Super 12 GB</td><td>32 GB</td><td>-</td></tr>
          </tbody></table></div>
        <p class="small text-secondary mb-0">Windows 11. Lines were spread across these machines; the machine that rendered an individual take is not recorded.
        The 35B-A3B model keeps as many expert layers on the GPU as fit and computes the rest on the CPU.</p>
      </div></div></div>
    </div>
  </section>

  <footer class="text-secondary small mt-5 border-top pt-3">
    Built with Fish Audio (Drama 3, Fish Audio Community License v1.0). Original clips are short excerpts of Planescape: Torment and Neverwinter Nights
    audio shown for demonstration; all rights belong to their owners. Generated __COUNT__ lines so far across the whole game.
  </footer>
</main>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  // Raw/directed toggle
  document.querySelectorAll('.line').forEach(function (p) {
    p.innerHTML = p.dataset.directed;
    p.dataset.mode = 'directed';
  });
  document.querySelectorAll('.toggle-line').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var p = btn.parentElement.querySelector('.line');
      if (p.dataset.mode === 'directed') {
        p.textContent = p.dataset.raw;
        p.classList.add('raw');
        p.dataset.mode = 'raw';
        btn.textContent = 'Show directed line';
      } else {
        p.innerHTML = p.dataset.directed;
        p.classList.remove('raw');
        p.dataset.mode = 'directed';
        btn.textContent = 'Show raw game line';
      }
    });
  });
  // Only one audio element plays at a time
  document.addEventListener('play', function (e) {
    document.querySelectorAll('audio').forEach(function (a) { if (a !== e.target) a.pause(); });
  }, true);
</script>
</body>
</html>
"""


BEGIN, END = "# BEGIN showcase whitelist (build_showcase.py)", "# END showcase whitelist"


def write_gitignore_whitelist(page: str) -> None:
    """Whitelist in .gitignore exactly the files index.html plays (the repo ignores *.mp3, *.wav, voice_library/ ...)."""
    from urllib.parse import unquote
    files = sorted(set(unquote(u) for u in re.findall(r'src="([^"]+\.(?:mp3|wav))"', page, re.I)))
    lines = [BEGIN, "!index.html", "!.nojekyll"]
    seen = set()
    for f in files:
        parts = f.split("/")
        for k in range(1, len(parts)):
            d = "/".join(parts[:k])
            if d not in seen:
                seen.add(d)
                lines += [f"!{d}/", f"{d}/*"]
        lines.append(f"!{f}")
    lines.append(END)
    path = ROOT / ".gitignore"
    text = path.read_text(encoding="utf-8")
    text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    # directory rules must be "dir/*" (not "dir/") so files inside can be re-included
    for d in ("voice_library", "voices_pst", "pst_revoice"):
        text = re.sub(rf"(?m)^{d}/\s*$", f"{d}/*", text)
    path.write_text(text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="only report voices with generated speech")
    args = ap.parse_args()

    gen, plan, catalog = load_inputs()
    for key in WANTED_NWN:
        n = len(candidates(gen, plan, f"nwn:{key}"))
        print(f"requested {key}: {n} generated speech-only lines" + ("" if n else "  -> skipped (never generated)"))
    if args.list:
        return 0

    cards = []
    for name in PST_VOICES:
        card = pst_card(name, gen, plan)
        print(f"{name}: {'ok' if card else 'no generated speech'}")
        cards.append(card)
    for key in WANTED_NWN + NWN_VOICES:
        card = nwn_card(key, gen, plan, catalog)
        if card:
            print(f"nwn:{key}: {card['line_count']} lines -> {[s['character'] for s in card['samples']]}")
            cards.append(card)
    cards = [c for c in cards if c]
    body = "".join(card_html(c, i) for i, c in enumerate(cards))
    page = PAGE.replace("__CARDS__", body).replace("__COUNT__", f"{len(gen):,}")
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    write_gitignore_whitelist(page)
    size = sum(f.stat().st_size for f in AUDIO.rglob("*.mp3")) / 1e6
    print(f"wrote {OUT / 'index.html'} ({len(cards)} voices, {size:.1f} MB audio)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
