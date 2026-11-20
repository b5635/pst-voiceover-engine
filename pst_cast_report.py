#!/usr/bin/env python3
"""
PST Casting Report
Lists every Planescape: Torment dialog, the NPCs (creatures) that use it, what
the LLM was told about them, the profile it wrote and the voice it picked, so
the mapping can be checked by eye.

    python pst_cast_report.py      -> pst_cast/report.html (+ report.csv)

The HTML page has a search box, a status filter and an audio player for every
picked voice's reference clip.
"""

import csv
import html
import json
import sys
from pathlib import Path

from pst_cast import CASTING_PATH, NPCS_PATH, build_profile_prompt, load_catalog

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
REPORT_HTML = Path("pst_cast/report.html")
REPORT_CSV = Path("pst_cast/report.csv")
# =======================================================

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e8;--card:#f7f7f9;--accent:#3b5bdb;
--ok:#2b8a3e;--warn:#c77700;--off:#868e96}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#e9e9ec;--muted:#9a9aa2;--line:#2c2c31;
--card:#1c1c20;--accent:#7d9bff;--ok:#69db7c;--warn:#ffc078;--off:#868e96}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0;padding:16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:12px}
.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;display:flex;gap:8px;flex-wrap:wrap;z-index:2;
border-bottom:1px solid var(--line)}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}
input{flex:1;min-width:200px}
.dlg{border:1px solid var(--line);border-radius:8px;margin:10px 0;background:var(--card);padding:10px 12px}
.head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.res{font-family:monospace;font-weight:600}
.name{font-weight:600}.tag{font-size:12px;padding:1px 7px;border-radius:10px;border:1px solid currentColor}
.cast{color:var(--ok)}.pending{color:var(--warn)}.voiced{color:var(--accent)}.narration{color:var(--off)}
table{border-collapse:collapse;margin:6px 0;font-size:13px;width:100%}td,th{border-bottom:1px solid var(--line);
padding:3px 6px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-top:6px}
@media (max-width:760px){.grid{grid-template-columns:1fr}}
.k{color:var(--muted)}details{margin-top:6px}summary{cursor:pointer;color:var(--accent)}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;
font-size:12px;max-height:420px;overflow:auto}audio{width:100%;max-width:360px;height:32px}
"""

JS = """
const q=document.getElementById('q'),s=document.getElementById('s'),n=document.getElementById('n');
function f(){const t=q.value.toLowerCase(),st=s.value;let c=0;
document.querySelectorAll('.dlg').forEach(d=>{
const ok=(!st||d.dataset.status===st)&&(!t||d.dataset.search.includes(t));
d.style.display=ok?'':'none';if(ok)c++});n.textContent=c+' shown'}
q.oninput=f;s.onchange=f;f();
"""


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def status_of(npc, cast) -> str:
    if npc["already_voiced"]:
        return "voiced"
    if npc["narration_only"]:
        return "narration"
    return "cast" if cast and cast.get("voice_id") else "pending"


def main() -> None:
    npcs = json.loads(NPCS_PATH.read_text(encoding="utf-8"))
    casting = json.loads(CASTING_PATH.read_text(encoding="utf-8")) if CASTING_PATH.exists() else {}
    voices = {v["id"]: v for v in load_catalog()}
    counts = {}
    parts = []
    rows = []

    for npc in npcs:
        c = casting.get(npc["dialog"])
        st = status_of(npc, c)
        counts[st] = counts.get(st, 0) + 1
        v = voices.get(c["voice_id"]) if c and c.get("voice_id") else None
        prof = (c or {}).get("profile") or {}
        label = {"voiced": f"already voiced: {npc['existing_voice']}", "narration": "narration only",
                 "cast": "cast", "pending": "not cast yet"}[st]

        cre_rows = "".join(
            f"<tr><td class=res>{esc(x['cre'])}</td><td>{esc(x['name'])}</td><td>{esc(x['gender'])}</td>"
            f"<td>{esc(x['race'])}</td><td>{esc(x['class'])}</td><td>{esc(x['animation'])}</td>"
            f"<td>{esc(x.get('faction'))}</td><td>{esc(x.get('alignment'))}</td>"
            f"<td>{esc(', '.join(x.get('areas', [])))}</td></tr>"
            for x in npc["creatures"])
        cre_html = (f"<table><tr><th>CRE</th><th>Name</th><th>Gender</th><th>Race</th><th>Class</th>"
                    f"<th>Animation (appearance)</th><th>Faction</th><th>Alignment</th><th>Found in</th></tr>"
                    f"{cre_rows}</table>") if cre_rows else \
            "<div class=k>No creature uses this dialog (started by a script or an object).</div>"

        prof_html = "".join(f"<div><span class=k>{esc(k)}:</span> {esc(val)}</div>" for k, val in prof.items()) \
            or "<div class=k>No profile yet.</div>"
        if v:
            audio = Path("..") / v["reference_wav"]
            voice_html = (f"<div class=name>{esc(v['id'])}</div><div>{esc(v['description'])}</div>"
                          f"<div class=k>{esc(v['source'])} · {esc(v['gender'])} · {esc(v['race'])} · "
                          f"{v['lines']} lines</div><div><span class=k>Why:</span> {esc(c.get('reason'))}</div>"
                          f"<audio controls preload=none src=\"{esc(audio.as_posix())}\"></audio>"
                          f"<div class=k>{esc(v.get('reference_text'))}</div>")
        else:
            voice_html = f"<div class=k>{esc((c or {}).get('reason') or 'No voice picked.')}</div>"

        prompt = (c or {}).get("profile_prompt") or (build_profile_prompt(npc) if st in ("cast", "pending") else "")
        lines_html = "\n".join(f"[{ln['strref']}]{' 🔊' if ln['has_sound'] else ''} {ln['text']}"
                               for ln in npc["lines"])
        search = " ".join([npc["dialog"], npc["speaker"], (v or {}).get("id", ""), json.dumps(prof)]
                          + [f"{x['name']} {x['animation']} {x['cre']}" for x in npc["creatures"]]).lower()
        parts.append(
            f"<div class=dlg data-status={st} data-search=\"{esc(search)}\">"
            f"<div class=head><span class=res>{esc(npc['dialog'])}</span><span class=name>{esc(npc['speaker'] or '?')}"
            f"</span><span class='tag {st}'>{esc(label)}</span><span class=k>{npc['line_count']} lines, "
            f"{npc['voiced_line_count']} with original audio</span></div>"
            f"<div><span class=k>Gender:</span> {esc(npc.get('gender'))} "
            f"<span class=k>({esc(npc.get('gender_source'))})</span></div>{cre_html}"
            f"<div class=grid><div><b>LLM profile</b>{prof_html}</div><div><b>Voice</b>{voice_html}</div></div>"
            + (f"<details><summary>What the LLM was given</summary><pre>{esc(prompt)}</pre></details>"
               if prompt else "")
            + f"<details><summary>All NPC lines ({npc['line_count']})</summary><pre>{esc(lines_html)}</pre></details>"
            f"</div>")

        rows.append({
            "Dialog": npc["dialog"], "Speaker": npc["speaker"], "Status": st,
            "Creatures": "; ".join(f"{x['cre']}={x['name']}" for x in npc["creatures"]),
            "Gender": npc.get("gender", ""), "GenderSource": npc.get("gender_source", ""),
            "Genders": ",".join(npc["genders"]), "Animations": "; ".join(npc["animations"]),
            "Lines": npc["line_count"], "ExistingVoice": npc["existing_voice"],
            "ProfileGender": prof.get("gender", ""), "ProfileType": prof.get("type", ""),
            "ProfileAge": prof.get("age", ""), "Appearance": prof.get("appearance", ""),
            "Personality": prof.get("personality", ""), "VoiceWanted": prof.get("voice", ""),
            "VoiceId": (c or {}).get("voice_id") or "", "VoiceDescription": (v or {}).get("description", ""),
            "Reason": (c or {}).get("reason", ""),
        })

    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    page = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>PST Voice Casting</title><style>{CSS}</style></head><body>"
            f"<h1>PST voice casting</h1><div class=sub>{len(npcs)} dialogs: {esc(summary)} · "
            f"{len(voices)} library voices"
            f"</div><div class=bar><input id=q placeholder='Search dialog, NPC, animation, voice…'>"
            f"<select id=s><option value=''>All</option><option value=cast>Cast</option><option value=pending>"
            f"Not cast yet</option><option value=voiced>Already voiced</option><option value=narration>"
            f"Narration only</option></select><span id=n class=k></span></div>{''.join(parts)}"
            f"<script>{JS}</script></body></html>")
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(page, encoding="utf-8")
    with REPORT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {REPORT_HTML} and {REPORT_CSV}: {summary}")


if __name__ == "__main__":
    main()
