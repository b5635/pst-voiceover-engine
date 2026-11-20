#!/usr/bin/env python3
"""
Voice Library Transcriber
Fills in the empty .txt transcripts of downloaded voice-library clips (WoW
lines ship without text) with Whisper, then rebuilds the WoW references and
catalog so every reference.wav has a matching reference.txt for cloning.

Runs in .venv-omnivoice (torch + transformers, Whisper only):
    .venv-omnivoice\\Scripts\\python.exe voicelib_transcribe.py
    (or Transcribe WoW in run_pst.bat)
"""

import argparse
import json
import sys
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
LIBRARY_DIR = Path("voice_library")
DEFAULT_MODEL = "openai/whisper-large-v3-turbo"     # same ASR OmniVoice uses
# =======================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Hugging Face Whisper model")
    parser.add_argument("--force", action="store_true", help="Re-transcribe clips that already have text")
    parser.add_argument("--only", action="append", help="Only this WoW folder (repeatable)")
    args = parser.parse_args()

    clips = sorted(LIBRARY_DIR.glob("wow/*/*.wav"))
    if args.only:
        allowed = set(args.only)
        clips = [w for w in clips if w.parent.name in allowed]
    todo = [w for w in clips if w.stem != "reference"
            and (args.force or not w.with_suffix(".txt").exists()
                 or not w.with_suffix(".txt").read_text(encoding="utf-8").strip())]
    print(f"{len(todo)} clips to transcribe")
    if todo:
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        asr = pipeline("automatic-speech-recognition", model=args.model, device=device,
                       torch_dtype=torch.float16 if device == 0 else torch.float32)
        for i, wav in enumerate(todo, 1):
            text = asr(str(wav), generate_kwargs={"language": "english", "task": "transcribe"})["text"].strip()
            wav.with_suffix(".txt").write_text(text, encoding="utf-8")
            if i % 50 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  {wav.parent.name}: {text[:60]}")

    import voicelib_wow
    groups = sorted(p.name for p in (LIBRARY_DIR / "wow").iterdir() if p.is_dir())
    catalog = voicelib_wow.build_catalog(groups)
    (LIBRARY_DIR / "wow" / "catalog.json").write_text(json.dumps(catalog, indent=2, ensure_ascii=False),
                                                      encoding="utf-8")
    print(f"Rebuilt {len(catalog)} WoW references and catalog")


if __name__ == "__main__":
    main()
