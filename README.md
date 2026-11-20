# pst-voiceover-engine

Full revoice of Planescape: Torment (classic) with Fish Audio Drama 3.
Split out of VVE_Builder: everything PST, nothing that needs VVE.

**[Listen to the voice showcase](https://b5635.github.io/pst-voiceover-engine/)** (GitHub Pages: https://b5635.github.io/pst-voiceover-engine/). Rebuild it with `python build_showcase.py`.

## Run

`run_pst.bat` opens `pst_gui.py`, which shows what is missing and what to click next.
`SPEECH_EXE`, `DRAMA3_MODEL` and `DRAMA3_DEVICE` default to the paths in the .bat.

## Note
This contains NO voices. You will need to generate them yourself. Do not ask for voices! Find some game and ask Claude to do it for you.

## Pipeline

1. `config_gui.py` – point `GAME_DIRECTORY` at classic Torment (`libs/appconfig.json`; EE settings in `libs/appconfig.ee.json`)
2. `dialog-report-prepare.py` – WeiDU extract → `dialog-report-pst-classic.csv`
3. `profiles-prepare.py` – original cast voices → `voices_pst/`
4. Library voices → `voice_library/`:
   - `extract_nwn_voices.bat [NWN EE folder]` – NWN soundsets (asks for the folder, remembers it in `nwn-path.txt`)
   - `get_wow_voices.bat` – the WoW voices PST is cast with; `get_wow_voices.bat all` for the full pool.
     Needs a RetailSounds checkout (`RETAILSOUNDS_DATA`, default `E:\GitHub\RetailSounds\data`), then Whisper fills transcripts
   - Finished `reference.wav` files are kept, so compiled Drama 3 voices don't change
5. `pst_npc_profiles.py`, `pst_cast.py` – LLM casting → `pst_cast/casting.json`
6. `pst_moods.py`, `pst_classify.py`, `pst_direct.py` – mood references and Strata line directions
7. `pst_fish.py` (one PC) or `pst_farm.py` + `pst_farm_worker.py` (SMB farm) – Drama 3 audio → `pst_revoice/audio_fish/`
8. `pst_revoice.py build` – WeiDU mod → `mod-pst-revoice/`

Floats and infopoints: `pst_floats.py`, `pst_infopoints.py`. Review: `pst_review.py`, `pst_cast_report.py`.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest -q
