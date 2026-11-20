# Handoff: Fish Audio Drama 3 for the PST revoice

Status as of 2026-11-14 night. Written for the next agent (Grok) to continue.

## Goal

Revoice Planescape: Torment with **Fish Audio Drama 3** as the production TTS (OmniVoice and VoiceBox are
deprecated). The user insists on Drama 3 itself (no S2-Pro, no pruned or distilled derivatives). Day-to-day work
is `run_pst.bat` / `pst_gui.py`.

Hardware: RTX 4060 Ti 16 GB (desktop apps hold ~3 GB of it), 32 GB RAM, i7-13700F, Windows 11.
Disks: `C:` NVMe (MSI M371), `E:` SATA SSD, **`D:` is a spinning HDD** (WD20EZBX, ~46-85 MB/s). The repo lives on D:.

## What is installed (outside the repo)

| Item | Path | Notes |
|---|---|---|
| speech.cpp 0.10.1 (Windows, Vulkan) | `D:\fish-drama3\speech\speech.exe` | prebuilt release, no CUDA toolkit needed. `SPEECH_EXE` overrides |
| Drama 3 final weights, Q4_K GGUF | `C:\fish-drama3\models\drama-3-final\Drama-35B-A3B-v3-Q4_K.gguf` | [tunjayoff/Drama-3-final-Q4_K-GGUF](https://huggingface.co/tunjayoff/Drama-3-final-Q4_K-GGUF), Oct 29 checkpoint `78e5ab7`, SHA-256 `6fa78c7f...cc7` verified. `DRAMA3_MODEL` overrides |
| Same file (HDD copy) | `D:\fish-drama3\models\drama-3-final\` | can be deleted; loads in ~240 s from the HDD vs ~24 s from C: |
| Research notes | `D:\fish-drama3\speechcpp_*.md`, `hf_search.txt`, `hf_repos2.txt` | speech.cpp docs and the HF quant survey |

Why this model + runtime (full survey in the chat; summary):
- Official `fishaudio/drama-3` needs 48 GB VRAM + Linux (SGLang-Omni). `drama-3-int4` needs ~24 GB + Linux.
- No Unsloth/bartowski quants exist. mradermacher GGUFs are llama.cpp backbone-only (no audio).
- Jmica GGUFs (audio.cpp port) are built from the Oct 13 *preview* weights, which have a known "chunk collapse"
  (silence/hum until the cap). tunjayoff's Q4_K is the *final* weights in speech.cpp's layout.
- speech.cpp's port is checked against the official tensors (96 dB+ SNR every stage) and keeps as many expert layers
  on the GPU as fit, computing the rest on the CPU.
- Candidate for later: `pentacoxian-dev/drama3-expressive-medium-GGUF` (fine-tune that follows mid-line directions,
  audio.cpp only) - a derivative, so only if the user agrees.

## What was built (in the repo)

| File | Role |
|---|---|
| `libs/tts_drama3.py` | speech.cpp client: `build_voice()` (reference clips + transcripts -> `.voice.gguf`) and `Drama3Worker` (one `speech worker` process, JSON Lines; `synthesize()` returns a `Take`) |
| `libs/pst_references.py` | reference-clip lookup (cloned mood clips, narrator, library voices) |
| `pst_fish.py` | Drama 3 generator. Builds voices, Drama 3 text per segment, retries bad takes, stitches to `pst_revoice/audio_fish/` |
| `pst_direct.py` | Strata (PolyStrata Qwen3.6 35B-A3B at `http://127.0.0.1:8080/v1`) "voice director": writes `pst_revoice/directions.json` |
| `pst_gui.py` / `run_pst.bat` | the only PST launcher (setup + Direct/Generate/Build). Checklist + next-step line |
| `tests/test_pst_fish.py` | 24 unit tests (text prep, director validation, take checks). Full suite: 342 passed |
| `libs/utils.py` | `setup_logging` fix: one console handler per process (imports of other script modules used to echo every line 2-3x); `VVE_LOG_STDOUT=1` forces console output into a pipe (GUI subprocesses) |
| `README.md` | new "Fish Audio Drama 3" section |

### What Drama 3 is fed per segment

- **Voice**: `--voice-mode multi` (default): one voice per character from their default mood clip + most-used mood
  clips (by `pst_revoice/moods.csv` counts) up to `MULTI_REFERENCE_SECONDS = 30` (Drama 3 clones best from ~10-30 s;
  every request's prompt carries the whole reference). `mood` = the line's mood clip (OmniVoice-style), `default` =
  one clip. Narrator: `voices_moods_pst/Narrator/neutral`. Cast NPCs: their library clip (needs a transcript; WoW
  clips without one are skipped with a warning).
- **Text** (after the existing pronunciation rules + patcher config, which strip `<...>`/`[...]`, so tags are added
  after): `*word*` -> `<emphasis>word</emphasis>`, shouted CAPS in mixed-case text -> lowercase `<emphasis>`.
- **Direction**: from `directions.json` only if the entry's `base` equals the current segment text exactly (so text or
  pronunciation edits never speak stale words); `sanitize_directed()` drops unknown tags, caps events at 2 and the
  direction at 20 words, and if the words changed keeps only the leading `[direction]`. Without a director entry:
  `MOOD_DIRECTIONS[mood]` (e.g. `[dry, sarcastic]`). Narrator: `[calm, measured storyteller voice]`.
- **Sampling**: `top_k=4097, top_p=0.9, temperature=1.0` (small top-k makes Drama 3 stall in silence at sentence ends).
- **Guards**: `max_seconds = 3 + 0.7 s/word`; a take that is cut off, too short (<0.12 s/word) or has an inner silence
  >2 s is retried with another seed (3 attempts, seeds reproducible per strref/segment/attempt); silence trimmed.

Drama 3 syntax reference (docs.fish.audio): leading `[free-form direction]`, point events (`[sighs]`, `[laughs]`,
`[short pause]`...), paired tags `<whisper> <emphasis> <soft> <fast> <slow> <stress> <forceful>`, Arpabet
`<|phoneme_start|>...<|phoneme_end|>`, multi-speaker `<|speaker:N|>`. Point tags and mid-line directions are reported
as less reliable than a leading direction.

## Test set and results

20 lines (10 Morte, 10 Annah), strrefs in `pst_revoice/test_lines.txt`.

- `pst_revoice/directions_claude.json`: directions hand-written by Claude (the "frontier" baseline).
- `pst_revoice/directions.json`: the same 20 by Strata (`pst_direct.py`), after prompt tuning 19/20 kept their words
  exactly (1890 "Hisss! Get!" falls back to direction-only: the LLM keeps restoring the on-screen spelling).
- Strata speed: ~2 min model load, then ~3 s/line -> full game (~18k planned lines, fewer with speech) ~15 h
  sequential; `--workers N` if the server has parallel slots (not checked).

Generated so far (all in `pst_revoice/`, listed side by side in `compare.html`):

| Folder | Voice | Directions | Status |
|---|---|---|---|
| `_archive_omnivoice/` | Old OmniVoice/VoiceBox output (archived 2026-11-15; not synced, not counted as voiced) | - | archived |
| `audio_fish_claude_moodclips/` | Drama 3, `--voice-mode mood` | Claude | done: 20/20 lines, 42 segments, no retries or flawed takes, 14.7 min incl. 14 voice files + 24 s load |
| `audio_fish/` | Drama 3, `--voice-mode multi` (3 clips, ~28 s per character) | Strata | done: 20/20, 13.9 min |
| `audio_fish_claude/` | Drama 3, `--voice-mode multi` | Claude | done: 20/20, 11.7 min; `compare.html` rewritten with all four columns |

To regenerate them:
```powershell
$ids = (Get-Content pst_revoice\test_lines.txt) | ForEach-Object { "--strref"; $_ }
python pst_fish.py @ids --force                                   # -> audio_fish (Strata directions)
python pst_fish.py @ids --force --directions pst_revoice\directions_claude.json --out pst_revoice\audio_fish_claude
```

Durations vs OmniVoice were plausible (Drama 3 ~10-40% longer, consistent with acted pauses; no runaway silences).
The user prefers multi-clip voices, so `multi` is the default. Listening is in progress from `run_pst.bat`. VRAM
tuning was dropped: the machine can run Generate for days.

## Remaining work

1. **Full Direct + Generate from `run_pst.bat`.** Direct missing (Strata loaded) then Generate missing. Existing
   directions and WAVs are skipped. `pst_revoice.py build` copies `pst_revoice/audio_fish/`.
2. **WoW transcripts.** Drama 3 skips library voices with empty `reference_text`. Transcribe WoW in the GUI (or
   `voicelib_transcribe.py`). Five groups were done for samples; most of the 91 groups are still empty.
3. **Pharod 17834** never got a director note: `parse_reply` uses greedy `{.*}` + `json.loads` and quote-heavy
   lines can fail. Fish falls back to the mood direction for that line.
4. Ideas not requested: Drama 3 multi-speaker scene mode (`<|speaker:N|>` instead of stitching); Arpabet phoneme
   tags for PST names; `previous_takes` continuity; companion bios from `beast.ini` / dialog dump for the cloned
   cast holes (Dak'kon, Nordom, Ravel, Vhailor, TTO, Nameless One).

## How to run

```powershell
run_pst.bat    # Plan, Direct missing, Generate missing, Transcribe WoW, review, Build mod
python pst_direct.py --speaker Morte --limit 10
python pst_fish.py --speaker Morte --limit 10
```


License: Fish Audio Community License v1.0 (free under USD 500k revenue, no hosted TTS service, no cloning real people
without consent, credit "Built with Fish Audio" when distributing).
