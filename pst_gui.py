#!/usr/bin/env python3
"""
PST Revoice (Fish Audio Drama 3)

One window for the whole remaining workflow: see what is left, Direct missing
lines with Strata, Generate missing audio with Drama 3, transcribe WoW library
voices, import/generate Narrator examine (info-point) lines and BCS idle barks,
Run overnight (Direct → free GPU → Generate, resumable), Spot check every unique
conversation in one batch to audit voices, review and regenerate single lines, Build the
WeiDU mod, or Sync to the classic install.

    python pst_gui.py
    run_pst.bat

Spoken-text edits auto-save to pst-line-overrides.json (TTS only; in-game text
is unchanged). Notes auto-save to pst-line-notes.json. Drama 3 directions
auto-save to pst_revoice/directions.json. Pronunciation rules live in
pst-pronunciation.json. Hand-picked speaker/voice (the Cast row) saves to
pst-cast-overrides.json and beats pst_cast/casting.json until removed.

Review: pst_review.py flags lines worth a listen (quote typos, narration read by
the wrong voice, cut-off takes, your notes, ...). Filter "Needs review", read
the conversation so far under the line, open the conversation view to play a
whole DLG, fix the text / direction, Ask Strata again or Regenerate, then Mark
reviewed (pst-line-review.json). "Direct shown" / "Regenerate shown" redo every
line the table currently lists.
"""

import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from libs.appconfig import cfg
from libs.farm import FarmPaths, heartbeat_age, is_online, read_json
from libs.pst_references import load_library
from libs.tts_drama3 import DRAMA3_DEVICE, get_speech_devices, resolve_device
from libs.utils import load_patcher_config
from pst_cast import load_rules
from pst_classify import read_cache
from pst_farm import LOCAL_NAME as FARM_LOCAL_NAME
from pst_farm import STATE_PATH as FARM_STATE_PATH
from pst_farm import STOP_FLAG as FARM_STOP_FLAG
from pst_farm import load_config as load_farm_config
from pst_farm import node_root as farm_node_root
from pst_fish import (
    audio_stale_reasons,
    directed_segments,
    direction_fields,
    direction_stale_reasons,
    drama3_segments,
    load_directions,
    sanitize_directed,
    save_directions,
)
from pst_floats import (
    EXPECTED_COUNT as FLOAT_TOTAL,
)
from pst_floats import (
    TEST_STRREFS as FLOAT_TEST_STRREFS,
)
from pst_floats import (
    is_float,
)
from pst_infopoints import EXPECTED_COUNT, gemrb_sync_cmd
from pst_pipeline import (
    WHISPER_PYTHON,
    collect_status,
    drama3_status,
    load_overnight_state,
    load_unique_dialogs,
    next_action,
    overnight_blocker,
    overnight_steps,
    save_overnight_state,
    setup_html,
    spot_check_picks,
    start_strata_server,
    strata_server_cmd,
    strata_status,
    unload_strata,
    work_html,
)
from pst_review import (
    CONVERSATION_DIR,
    PLAYER_LABEL,
    audit,
    conversation_html,
    load_reviewed,
    load_texts,
    mark_reviewed,
    needs_review,
    save_reviewed,
)
from pst_revoice import (
    CAST_OVERRIDES_PATH,
    FISH_AUDIO_DIR,
    NARRATOR,
    PLAN_PATH,
    PlanRow,
    dialog_of_strrefs,
    generation_snapshot,
    load_cast_overrides,
    load_generation,
    load_line_notes,
    load_line_overrides,
    load_pronunciation,
    load_voice_aliases,
    needs_direction,
    plan_stale_reasons,
    profile_voices,
    read_plan,
    rebuild_plan,
    save_cast_overrides,
    save_line_notes,
    save_line_overrides,
    tts_segments,
    voice_for,
)

# ==================== CONFIGURATION ====================
SPOT_DIRECT_FILE = FISH_AUDIO_DIR.parent / "spot_check_direct.txt"     # strrefs for one batch direct
SPOT_GENERATE_FILE = FISH_AUDIO_DIR.parent / "spot_check_generate.txt"  # strrefs for one batch generate
SPOT_LINES_FILE = FISH_AUDIO_DIR.parent / "spot_check_lines.txt"       # the last spot-check batch (filter)
SPOT_FORCE_FILE = FISH_AUDIO_DIR.parent / "spot_check_force.txt"        # flagged takes to redo even if current
REDO_TAKE_CODES = {"take", "pace"}     # review flags whose current WAV is itself the problem
COLUMNS = ["StrRef", "Character", "Voice", "Directed", "Audio", "Edited", "Notes", "Review", "Line"]
SHOWN_DIRECT_FILE = FISH_AUDIO_DIR.parent / "shown_direct.txt"        # "Direct shown" batch
SHOWN_GENERATE_FILE = FISH_AUDIO_DIR.parent / "shown_generate.txt"    # "Regenerate shown" batch
CONTEXT_LINES = 8                  # conversation lines shown above the selected line
OWN_VOICE = "(speaker's own voice)"
FILTERS = ["All lines", "Spot check batch", "Needs review", "Flagged", "Reviewed", "Need directing", "Need generating",
           "Generated", "Needs regen",
           "Stale directions", "Edited", "Has notes", "Examine lines", "Idle barks", "Original VO"]
OVERNIGHT_POLL_MS = 2000
OVERNIGHT_SERVER_TRIES = 30
LOAD_STRATA_CMD = [sys.executable, "-c",
                   "from pst_pipeline import load_strata; load_strata()"]
FISH_ENV = {"VVE_LOG_STDOUT": "1"}
if DRAMA3_DEVICE:
    FISH_ENV["DRAMA3_DEVICE"] = DRAMA3_DEVICE
SAVE_DELAY_MS = 400
STATUS_MS = 15000
FARM_MS = 3000                     # farm panel refresh
FARM_STOP_GRACE_MS = 150000        # Stop job on a farm run: wait this long for a clean stop, then kill
FARM_COLUMNS = ["PC", "Status", "GPU", "Working on", "Out", "Done", "Lines/min"]
NEXT_NEED = ("font-size: 15px; font-weight: 700; color: #991b1b; background: #fee2e2; "
             "padding: 8px 10px; border-radius: 6px;")
NEXT_OK = ("font-size: 15px; font-weight: 700; color: #166534; background: #dcfce7; "
           "padding: 8px 10px; border-radius: 6px;")
NEXT_BTN = "background: #fecaca; color: #7f1d1d; font-weight: 700; border: 2px solid #dc2626; padding: 6px 10px;"
# =======================================================


class RevoiceWindow(QMainWindow):
    """Pipeline status on top, line list on the left, editor on the right."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PST Revoice — Fish Audio Drama 3")
        self.resize(1440, 920)

        self.plan: List[PlanRow] = [p for p in read_plan(PLAN_PATH) if p.action == "generate"] \
            if PLAN_PATH.exists() else []
        self.patcher_config = load_patcher_config(Path(cfg.PATCHER_CONFIG_PATH))
        self.pronunciation = load_pronunciation()
        self.overrides: Dict[int, str] = load_line_overrides()
        self.notes_map: Dict[int, str] = load_line_notes()
        self.generation: Dict[int, Dict] = load_generation(FISH_AUDIO_DIR / "generation.json")
        self.moods = read_cache()
        self.directions: Dict[int, Dict] = load_directions()
        self.cast_overrides: Dict[str, Dict] = load_cast_overrides()
        self.dialog_of: Dict[int, str] = dialog_of_strrefs()
        self.library: Dict[str, Dict] = load_library()
        self.reviewed: Dict[int, Dict] = load_reviewed()
        self.flags: Dict[int, List] = {}
        self._texts: Optional[Dict] = None      # dialog report strref -> (speaker, text), loaded on demand
        self._context = None                    # pst_classify.DialogContext, loaded on demand
        self._reaudit()
        self._direction_loaded = ""
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._flush_edits)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_MS)
        self._status_timer.timeout.connect(self._refresh_status)
        self.current: Optional[PlanRow] = None
        self.process: Optional[QProcess] = None
        self._after_job = None
        self._job_label = ""
        self._queued: List = []
        self._queue_done = None
        self._overnight_on = False
        self._strata_wait_tries = 0
        self._strata_on_ready = None
        self._unique_dialogs = load_unique_dialogs()
        self._spot_strrefs: List[int] = []
        self._spot_force: List[int] = []
        self._spot_count_dirty = True

        self.audio_out = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio_out)

        self._build_ui()
        self._refresh_table()
        self._refresh_status()
        self._status_timer.start()
        state = load_overnight_state()
        if state.get("status") in ("paused", "running"):
            step = state.get("step") or "the next unfinished step"
            self._log(f"Overnight was interrupted at {step}. Click Run overnight to continue.")

    # ---------------- UI ----------------

    def _build_ui(self):
        self.intro_label = QLabel(
            "This window revoices Planescape: Torment with Fish Audio Drama 3. "
            "Red means something is still missing. The highlighted button is what to click next.")
        self.intro_label.setWordWrap(True)
        self.intro_label.setStyleSheet("color: #3f3f46;")
        self.next_label = QLabel("")
        self.next_label.setWordWrap(True)
        self.next_label.setStyleSheet(NEXT_NEED)
        self.setup_label = QLabel("")
        self.setup_label.setWordWrap(True)
        self.setup_label.setTextFormat(Qt.RichText)
        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        self.counts_label.setTextFormat(Qt.RichText)
        self.tools_label = QLabel("")
        self.tools_label.setWordWrap(True)
        self.tools_label.setTextFormat(Qt.RichText)

        self.config_btn = QPushButton("Game config")
        self.config_btn.setToolTip("Open config_gui. Point GAME_DIRECTORY at classic PST and save, then Dialog report.")
        self.config_btn.clicked.connect(self._open_config)
        self.dialog_btn = QPushButton("Dialog report")
        self.dialog_btn.setToolTip("Extract DLG/TLK into the dialog CSV. First real data step. Slow.")
        self.dialog_btn.clicked.connect(self._dialog_report)
        self.extract_btn = QPushButton("Extract cloned voices")
        self.extract_btn.setToolTip(
            "Copy the 12 originally voiced PST characters (Morte, Annah, Dak'kon, …) into "
            "voices_pst/. Done when all 12 are there. Everyone else uses Cast NPCs.")
        self.extract_btn.clicked.connect(self._extract_voices)
        self.collect_moods_btn = QPushButton("Collect clone clips")
        self.collect_moods_btn.setToolTip(
            "Decode original conversation lines for the 12 voiced characters. "
            "Still needed for Drama 3: those clips become the clone voice. "
            "After this, label deliveries in pst-mood-labels.json, then Build clone refs.")
        self.collect_moods_btn.clicked.connect(self._collect_moods)
        self.build_moods_btn = QPushButton("Build clone refs")
        self.build_moods_btn.setToolTip(
            "Stitch labeled clips into voices_moods_pst/. Drama 3 stacks them into one "
            "voice per character (not OmniVoice's one-mood-per-line).")
        self.build_moods_btn.clicked.connect(self._build_moods)
        self.voicelib_btn = QPushButton("Build voice library")
        self.voicelib_btn.setToolTip("Download NWN soundsets + WoW NPC clips into voice_library/. Then Transcribe WoW.")
        self.voicelib_btn.clicked.connect(self._build_voicelib)
        self.cast_btn = QPushButton("Cast NPCs")
        self.cast_btn.setToolTip("Strata must be loaded. Profiles every unvoiced dialog and picks a library voice.")
        self.cast_btn.clicked.connect(self._cast_npcs)
        self.cast_report_btn = QPushButton("Casting report")
        self.cast_report_btn.setToolTip("Rebuild pst_cast/report.html and open it.")
        self.cast_report_btn.clicked.connect(self._cast_report)
        self.plan_btn = QPushButton("Plan")
        self.plan_btn.setToolTip(
            "Write pst_revoice/plan.csv from the dialog report and casting. Re-run when the dialog "
            "report or casting is newer (the checklist turns red).")
        self.plan_btn.clicked.connect(self._write_plan)

        self.direct_missing_btn = QPushButton("Direct missing")
        self.direct_missing_btn.setToolTip(
            "Strata writes [direction] for lines not in directions.json, and re-directs lines whose "
            "spoken text, speaker, or pronunciation no longer match (a voice-only recast keeps the "
            "direction). Overnight is fine.")
        self.direct_missing_btn.clicked.connect(self._direct_missing)
        self.generate_missing_btn = QPushButton("Generate missing")
        self.generate_missing_btn.setToolTip(
            "Drama 3 speaks lines with no WAV, and regenerates WAVs whose spoken text, direction, "
            "speaker, voice, or pronunciation changed. Unloads Strata first. Can run for days.")
        self.generate_missing_btn.clicked.connect(self._generate_missing)
        self.spot_check_btn = QPushButton("Spot check")
        self.spot_check_btn.setToolTip(
            "Audit every unique conversation (one person's DLG) in one run, plus every line waiting in "
            "Needs review (quote fixes, read-outs, flawed takes, notes...; flawed takes are redone). "
            "Picks each conversation's longest "
            "still-unsampled voiceable line, directs all that need it in one Strata batch, then "
            "generates them all in one Drama 3 batch. Resumable: Stop job and click again to continue. "
            "Conversations that already have a current voiced line are skipped. Recast "
            "(speaker or voice changed) or a stale direction makes them eligible again.")
        self.spot_check_btn.clicked.connect(self._spot_check)
        self.transcribe_btn = QPushButton("Transcribe WoW")
        self.transcribe_btn.setToolTip("Whisper fills empty WoW library transcripts so Drama 3 can clone those NPCs.")
        self.transcribe_btn.clicked.connect(self._transcribe_wow)
        self.build_btn = QPushButton("Build mod")
        self.build_btn.setToolTip("Copy pst_revoice/audio_fish into the WeiDU mod PST_Revoiced. "
                                  "Examine lines copy WAV only (no STRING_SET).")
        self.build_btn.clicked.connect(self._build_mod)
        self.import_examine_btn = QPushButton("Import examine")
        self.import_examine_btn.setToolTip(
            f"ARE info-point (? cursor) lines, voiced by the Narrator. "
            f"{EXPECTED_COUNT} in classic PST. Adds every remaining line to the plan. "
            f"Does not generate audio.")
        self.import_examine_btn.clicked.connect(self._import_examine)
        self.generate_examine_btn = QPushButton("Generate examine")
        self.generate_examine_btn.setToolTip(
            "Drama 3 speaks Narrator ?-click lines that are in the plan and have no WAV, "
            "and regenerates stale ones. Import examine first. Skip until you want ?-click VO.")
        self.generate_examine_btn.clicked.connect(self._generate_examine)
        self.import_floats_btn = QPushButton("Import idle barks")
        self.import_floats_btn.setToolTip(
            f"BCS FloatMessage / FloatMessageRnd overhead lines (collectors, workers, ghouls). "
            f"{FLOAT_TOTAL} in classic PST after skipping dialog already in the plan. "
            f"Adds every remaining line. Does not generate audio. Do not STRING_SET.")
        self.import_floats_btn.clicked.connect(self._import_floats)
        self.generate_floats_btn = QPushButton("Generate idle barks")
        self.generate_floats_btn.setToolTip(
            "Drama 3 speaks idle barks that are in the plan and have no WAV, "
            "and regenerates stale ones. Import idle barks first. "
            f"Test set is {', '.join(str(s) for s in FLOAT_TEST_STRREFS[:5])}.")
        self.generate_floats_btn.clicked.connect(self._generate_floats)
        self.overnight_btn = QPushButton("Run overnight")
        self.overnight_btn.setToolTip(
            "Unattended chain: import remaining examine/idle barks, start/load Strata, "
            "Direct missing, unload Strata (free the GPU), Transcribe WoW if needed, "
            "Generate missing with Drama 3. Skips finished work, so click again to resume "
            "after a failure or after Stop job. Does not Build the mod or Sync.")
        self.overnight_btn.clicked.connect(self._overnight)
        self.sync_btn = QPushButton("Sync to game")
        self.sync_btn.setToolTip(
            "GemRB tools/sync_vve_voices.py: copy WAVs into override and patch TLK "
            "sound resrefs only (never the text). Needs write access to the game folder.")
        self.sync_btn.clicked.connect(self._sync_gemrb)
        self.stop_job_btn = QPushButton("Stop job")
        self.stop_job_btn.clicked.connect(self._stop_job)
        self.stop_job_btn.setEnabled(False)

        setup_row1 = QHBoxLayout()
        for w in (self.config_btn, self.dialog_btn, self.extract_btn,
                  self.collect_moods_btn, self.build_moods_btn):
            setup_row1.addWidget(w)
        setup_row1.addStretch(1)
        setup_row2 = QHBoxLayout()
        for w in (self.voicelib_btn, self.cast_btn, self.cast_report_btn, self.plan_btn):
            setup_row2.addWidget(w)
        setup_row2.addStretch(1)
        setup_box = QGroupBox("Setup (once). Red on the checklist = not done yet. Do those in order.")
        setup_l = QVBoxLayout(setup_box)
        setup_l.addLayout(setup_row1)
        setup_l.addLayout(setup_row2)

        work_row1 = QHBoxLayout()
        for w in (self.overnight_btn, self.direct_missing_btn, self.transcribe_btn,
                  self.generate_missing_btn, self.spot_check_btn, self.stop_job_btn):
            work_row1.addWidget(w)
        work_row1.addStretch(1)
        work_row2 = QHBoxLayout()
        for w in (self.import_examine_btn, self.generate_examine_btn,
                  self.import_floats_btn, self.generate_floats_btn, self.build_btn, self.sync_btn):
            work_row2.addWidget(w)
        work_row2.addStretch(1)
        work_box = QGroupBox("Work. Direct every line, generate audio, listen, then Build. "
                            "Run overnight chains Direct → free GPU → Generate and resumes if you stop it.")
        work_l = QVBoxLayout(work_box)
        work_l.addLayout(work_row1)
        work_l.addLayout(work_row2)

        self.farm_sync_btn = QPushButton("Update farm PCs")
        self.farm_sync_btn.setToolTip(
            "Copy the bare minimum the farm PCs need (worker code, speech.cpp, the Drama 3 model, setup .bat "
            "files) to their shares in pst-farm.json. Only changed files are copied; the model (21 GB) only "
            "the first time. Then on each farm PC run install_worker.bat once and run_worker.bat to serve.")
        self.farm_sync_btn.clicked.connect(self._farm_sync)
        self.farm_run_btn = QPushButton("Farm generate")
        self.farm_run_btn.setToolTip(
            "Generate missing (and stale) lines on every PC that is serving: each gets batches of a few lines, "
            "a lock file per line keeps two PCs off the same line, and finished takes are pulled back here and "
            "stitched. PCs can join or leave while it runs. Stop job stops cleanly; click again to resume.")
        self.farm_run_btn.clicked.connect(self._farm_run)
        self.farm_local_box = QCheckBox("Use this PC too")
        self.farm_local_box.setToolTip("Also speak lines on this PC's GPU (unloads Strata first).")
        self.farm_local_box.setChecked(bool(load_farm_config().get("use_this_pc", True)))
        self.farm_summary = QLabel("")
        self.farm_summary.setWordWrap(True)
        self.farm_summary.setTextFormat(Qt.RichText)
        self.farm_table = QTableWidget(0, len(FARM_COLUMNS))
        self.farm_table.setHorizontalHeaderLabels(FARM_COLUMNS)
        self.farm_table.verticalHeader().setVisible(False)
        self.farm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.farm_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.farm_table.setWordWrap(False)
        self.farm_table.verticalHeader().setDefaultSectionSize(22)
        self.farm_table.setMaximumHeight(22 * 4 + 30)
        farm_header = self.farm_table.horizontalHeader()
        for i, width in enumerate((110, 150, 210, 0, 45, 55, 70)):
            if width:
                farm_header.setSectionResizeMode(i, QHeaderView.Interactive)
                self.farm_table.setColumnWidth(i, width)
        farm_header.setSectionResizeMode(3, QHeaderView.Stretch)
        farm_row = QHBoxLayout()
        for w in (self.farm_run_btn, self.farm_local_box, self.farm_sync_btn):
            farm_row.addWidget(w)
        farm_row.addWidget(self.farm_summary, 1)
        farm_box = QGroupBox("Farm: other PCs speak Drama 3 lines for this one (pst-farm.json)")
        farm_l = QVBoxLayout(farm_box)
        farm_l.addLayout(farm_row)
        farm_l.addWidget(self.farm_table)
        self._farm_probe: Dict[str, Dict] = {}
        self._farm_probe_thread: Optional[threading.Thread] = None
        self._farm_running = False
        self._farm_timer = QTimer(self)
        self._farm_timer.setInterval(FARM_MS)
        self._farm_timer.timeout.connect(self._refresh_farm)
        self._farm_timer.start()

        self.job_info = QLabel("Job: idle")
        self.job_info.setStyleSheet("color: #6b6b70;")

        pipeline = QWidget()
        pl = QVBoxLayout(pipeline)
        pl.setContentsMargins(8, 8, 8, 4)
        pl.addWidget(self.intro_label)
        pl.addWidget(self.next_label)
        pl.addWidget(self.setup_label)
        pl.addWidget(self.counts_label)
        pl.addWidget(self.tools_label)
        pl.addWidget(setup_box)
        pl.addWidget(work_box)
        pl.addWidget(farm_box)
        pl.addWidget(self.job_info)

        self.speaker_box = QComboBox()
        self.speaker_box.addItem("All characters")
        self.speaker_box.addItems(sorted({p.speaker for p in self.plan}))
        self.speaker_box.currentIndexChanged.connect(self._refresh_table)
        self.voice_box = QComboBox()
        self.voice_box.addItem("All voices")
        self.voice_box.addItems(sorted({self._line_voice(p) for p in self.plan}, key=str.lower))
        self.voice_box.setToolTip("Only lines spoken by this voice (cast library voice, or the character's own).")
        self.voice_box.currentIndexChanged.connect(self._refresh_table)
        self.search = QLineEdit(placeholderText="Search text, notes, or strref…")
        # Debounced: rebuilding the whole table on every keystroke is what made typing laggy.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self._refresh_table(reload_pronunciation=False))
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self._hay_static: Dict[int, str] = {}
        self.only_audio = QComboBox()
        self.only_audio.addItems(FILTERS)
        self.only_audio.currentIndexChanged.connect(self._refresh_table)

        self.direct_shown_btn = QPushButton("Direct shown")
        self.direct_shown_btn.setToolTip(
            "Ask Strata again for every line the table lists now (any filter / search), replacing their "
            "directions (manual ones too). Use after changing the director or the delivery guard.")
        self.direct_shown_btn.clicked.connect(self._direct_shown)
        self.generate_shown_btn = QPushButton("Regenerate shown")
        self.generate_shown_btn.setToolTip("Drama 3 regenerates every line the table lists now, even up-to-date ones.")
        self.generate_shown_btn.clicked.connect(self._generate_shown)

        filters = QHBoxLayout()
        filters.addWidget(self.speaker_box)
        filters.addWidget(self.voice_box)
        filters.addWidget(self.only_audio)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.direct_shown_btn)
        filters.addWidget(self.generate_shown_btn)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for i, width in enumerate((60, 150, 150, 70, 55, 55, 55, 65)):  # fixed: ResizeToContents rescans every row
            header.setSectionResizeMode(i, QHeaderView.Interactive)
            self.table.setColumnWidth(i, width)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_select)
        self._sort_col, self._sort_order = -1, Qt.AscendingOrder
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addLayout(filters)
        left_layout.addWidget(self.table)

        self.title = QLabel("Select a line")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")

        self.cast_speaker = QComboBox()
        self.cast_speaker.setEditable(True)
        self.cast_speaker.setToolTip("Who speaks this line (profile + pronunciation rules). Type a new name if needed.")
        self.cast_voice = QComboBox()
        self.cast_voice.setEditable(True)
        self.cast_voice.setToolTip(f"{OWN_VOICE} = the speaker's clone or alias; or a library voice id.")
        self.cast_apply_btn = QPushButton("Recast")
        self.cast_apply_btn.setToolTip(
            "Recast the whole conversation (DLG) this line is in: saved to pst-cast-overrides.json "
            "(beats casting.json and Cast NPCs) and the plan is rebuilt now. Examine lines and idle "
            "barks have no conversation, so only that line changes. Audio goes stale; directions "
            "only go stale if the speaker changes.")
        self.cast_apply_btn.clicked.connect(self._apply_recast)
        self.cast_remove_btn = QPushButton("Remove hand pick")
        self.cast_remove_btn.setToolTip("Drop the hand pick for this conversation and go back to casting.json.")
        self.cast_remove_btn.clicked.connect(self._remove_recast)
        self.cast_info = QLabel("")
        self.cast_info.setWordWrap(True)
        self.cast_info.setStyleSheet("color: #6b6b70;")
        self._fill_cast_choices()
        cast_row = QHBoxLayout()
        cast_row.addWidget(QLabel("Cast:"))
        cast_row.addWidget(self.cast_speaker, 1)
        cast_row.addWidget(self.cast_voice, 2)
        cast_row.addWidget(self.cast_apply_btn)
        cast_row.addWidget(self.cast_remove_btn)
        self.game_text = QPlainTextEdit(readOnly=True)
        self.game_text.setMaximumHeight(110)
        self.edit_text = QPlainTextEdit()
        self.edit_text.textChanged.connect(self._on_spoken_changed)
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setMaximumHeight(120)
        self.gen_info = QLabel("")
        self.gen_info.setWordWrap(True)
        self.gen_info.setStyleSheet("color: #6b6b70;")

        self.revert_btn = QPushButton("Revert to game text")
        self.revert_btn.clicked.connect(self._revert_override)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.revert_btn)
        edit_row.addStretch(1)

        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Notes for later (pronunciation, regen reminders…). Auto-saved.")
        self.notes.setMaximumHeight(80)
        self.notes.textChanged.connect(self._on_notes_changed)

        self.direction_edit = QPlainTextEdit()
        self.direction_edit.setPlaceholderText("One line per speech segment, e.g. [dry, mocking] Ugly little berks.")
        self.direction_edit.setMaximumHeight(110)
        self.direction_edit.textChanged.connect(self._on_direction_changed)
        self.direction_info = QLabel("")
        self.direction_info.setWordWrap(True)
        self.direction_info.setStyleSheet("color: #6b6b70;")
        self.direct_btn = QPushButton("Ask Strata for this line")
        self.direct_btn.clicked.connect(self._direct)
        direction_row = QHBoxLayout()
        direction_row.addWidget(self.direct_btn)
        direction_row.addWidget(self.direction_info, 1)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(lambda: self._play(FISH_AUDIO_DIR))
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.clicked.connect(self.player.stop)
        self.regen_btn = QPushButton("⟳ Regenerate this line")
        self.regen_btn.clicked.connect(self._regenerate)
        actions = QHBoxLayout()
        for w in (self.play_btn, self.stop_btn, self.regen_btn):
            actions.addWidget(w)
        actions.addStretch(1)

        self.review_info = QLabel("")
        self.review_info.setWordWrap(True)
        self.review_info.setTextFormat(Qt.RichText)
        self.review_btn = QPushButton("✓ Mark reviewed")
        self.review_btn.setToolTip(
            "Take the line out of Needs review (pst-line-review.json). A new flag brings it back.")
        self.review_btn.clicked.connect(self._toggle_reviewed)
        self.conversation_btn = QPushButton("Conversation view")
        self.conversation_btn.setToolTip(
            "Open this line's whole DLG in the browser: every line with its audio, direction and flags, "
            "player replies as links. Click a reply to jump there and play it.")
        self.conversation_btn.clicked.connect(self._open_conversation)
        review_row = QHBoxLayout()
        review_row.addWidget(self.review_info, 1)
        review_row.addWidget(self.review_btn)
        review_row.addWidget(self.conversation_btn)
        self.context_view = QPlainTextEdit(readOnly=True)
        self.context_view.setMaximumHeight(130)
        self.context_view.setPlaceholderText("Conversation so far (player choices and earlier lines)…")

        self.status = QPlainTextEdit(readOnly=True)
        self.status.setMaximumHeight(140)
        self.status.setPlaceholderText("Job log…")

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(self.title)
        rl.addLayout(review_row)
        rl.addWidget(QLabel("Conversation so far (one path; the last line is the player's choice):"))
        rl.addWidget(self.context_view)
        rl.addLayout(cast_row)
        rl.addWidget(self.cast_info)
        rl.addWidget(QLabel("Game text (on screen):"))
        rl.addWidget(self.game_text)
        rl.addWidget(QLabel("Spoken text (edit here; quotes = character, outside = narrator). Auto-saved."))
        rl.addWidget(self.edit_text, 1)
        rl.addLayout(edit_row)
        rl.addWidget(QLabel("What each voice will say (after pronunciation rules):"))
        rl.addWidget(self.preview)
        rl.addWidget(self.gen_info)
        rl.addWidget(QLabel("Notes (auto-saved; filterable):"))
        rl.addWidget(self.notes)
        rl.addWidget(QLabel("Drama 3 performance (one line per speech segment; [direction], <emphasis>, [events]). "
                            "Auto-saved."))
        rl.addWidget(self.direction_edit)
        rl.addLayout(direction_row)
        rl.addLayout(actions)
        rl.addWidget(self.status)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([700, 640])

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(pipeline)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self._set_enabled(False)

    def _pipeline_buttons(self) -> List[QPushButton]:
        return [self.dialog_btn, self.extract_btn, self.collect_moods_btn, self.build_moods_btn,
                self.voicelib_btn, self.cast_btn, self.cast_report_btn, self.plan_btn,
                self.direct_missing_btn, self.generate_missing_btn, self.spot_check_btn,
                self.transcribe_btn, self.build_btn, self.import_examine_btn,
                self.generate_examine_btn, self.import_floats_btn, self.generate_floats_btn,
                self.overnight_btn, self.sync_btn, self.direct_shown_btn, self.generate_shown_btn,
                self.farm_run_btn, self.farm_sync_btn]

    def _job_buttons(self) -> List[QPushButton]:
        return self._pipeline_buttons() + [self.direct_btn, self.regen_btn]

    def _set_enabled(self, on: bool):
        for w in (self.edit_text, self.revert_btn, self.notes, self.play_btn, self.regen_btn,
                  self.direction_edit, self.direct_btn, self.cast_speaker, self.cast_voice,
                  self.cast_apply_btn, self.cast_remove_btn, self.review_btn, self.conversation_btn):
            w.setEnabled(on)
        if on:
            p = self.current
            self.direct_btn.setEnabled(bool(p) and needs_direction(p, self._spoken(p)))
            self._update_play_buttons()

    def _update_play_buttons(self):
        p = self.current
        self.play_btn.setEnabled(bool(p) and self._has_audio(p))

    # ---------------- status ----------------

    def _action_buttons(self) -> Dict[str, QPushButton]:
        return {
            "dialog": self.dialog_btn,
            "extract": self.extract_btn,
            "moods_collect": self.collect_moods_btn,
            "moods_build": self.build_moods_btn,
            "voicelib": self.voicelib_btn,
            "cast": self.cast_btn,
            "plan": self.plan_btn,
            "direct": self.direct_missing_btn,
            "transcribe": self.transcribe_btn,
            "generate": self.generate_missing_btn,
            "generate_examine": self.generate_examine_btn,
            "generate_floats": self.generate_floats_btn,
            "import_examine": self.import_examine_btn,
            "import_floats": self.import_floats_btn,
            "overnight": self.overnight_btn,
            "build": self.build_btn,
        }

    def _highlight_next(self, key: str):
        for button in self._action_buttons().values():
            button.setStyleSheet("")
        target = self._action_buttons().get(key)
        if target is not None:
            target.setStyleSheet(NEXT_BTN)

    def _refresh_status(self):
        self.pronunciation = load_pronunciation()
        status = collect_status(
            self.plan, self.directions, self.overrides, FISH_AUDIO_DIR,
            load_library(), strata_status(), drama3_status(),
            generation=self.generation, moods=self.moods,
            patcher_config=self.patcher_config, pronunciation=self.pronunciation,
            plan_stale=bool(plan_stale_reasons()))
        key, message = next_action(status)
        self.next_label.setText(message)
        self.next_label.setStyleSheet(NEXT_OK if key in ("build", "listen") else NEXT_NEED)
        self.setup_label.setText(setup_html(status))
        self.counts_label.setText(work_html(status))
        strata_ok = status.strata == "loaded"
        drama_ok = status.drama3 == "ready"
        drama_device = resolve_device()
        devices = get_speech_devices()
        dev_desc = next((d.get("description", "") for d in devices if d.get("name") == drama_device), drama_device)
        dev_short = "4060 Ti" if "4060" in dev_desc else (dev_desc or drama_device)
        whisper_ok = WHISPER_PYTHON.exists()
        whisper = "Whisper ready" if whisper_ok else f"Whisper missing ({WHISPER_PYTHON})"
        self.tools_label.setText(
            f"Tools: "
            f'<span style="color:{"#166534" if strata_ok else "#b91c1c"};'
            f'font-weight:{"400" if strata_ok else "700"}">Strata {status.strata}</span>'
            f' &nbsp;·&nbsp; '
            f'<span style="color:{"#166534" if drama_ok else "#b91c1c"};'
            f'font-weight:{"400" if drama_ok else "700"}" title="Drama 3 device: {drama_device} ({dev_desc})">Drama 3 {status.drama3} ({dev_short})</span>'
            f' &nbsp;·&nbsp; '
            f'<span style="color:{"#166534" if whisper_ok else "#b91c1c"};'
            f'font-weight:{"400" if whisper_ok else "700"}">{whisper}</span>'
        )
        self.direct_missing_btn.setText(self._count_button(
            "Direct missing", status.need_direct, status.stale_direct))
        self.generate_missing_btn.setText(self._count_button(
            "Generate missing", status.need_audio, status.stale_audio))
        if self._spot_count_dirty:
            self._refresh_spot_count()
        self.transcribe_btn.setText(
            f"Transcribe WoW ({status.wow_missing:,})" if status.wow_missing else "Transcribe WoW")
        import_left = max(0, (status.examine_total or 0) - status.examine)
        self.import_examine_btn.setText(
            f"Import examine ({import_left:,} left)" if import_left else "Import examine")
        need_wav = max(0, status.examine - status.examine_audio)
        if status.examine:
            self.generate_examine_btn.setText(
                self._count_button("Generate examine", need_wav, status.examine_stale))
        else:
            self.generate_examine_btn.setText("Generate examine")
        float_import = max(0, (status.floats_total or 0) - status.floats)
        self.import_floats_btn.setText(
            f"Import idle barks ({float_import:,} left)" if float_import else "Import idle barks")
        need_float_wav = max(0, status.floats - status.floats_audio)
        if status.floats:
            self.generate_floats_btn.setText(
                self._count_button("Generate idle barks", need_float_wav, status.floats_stale))
        else:
            self.generate_floats_btn.setText("Generate idle barks")
        chain = overnight_steps(status)
        if chain:
            self.overnight_btn.setText(f"Run overnight ({len(chain)} steps)")
        else:
            self.overnight_btn.setText("Run overnight")
        busy = self.process is not None or self._overnight_on
        for w in self._pipeline_buttons():
            w.setEnabled(not busy)
        self.stop_job_btn.setEnabled(busy)
        if not busy:
            self.job_info.setText("Job: idle")
            self._highlight_next(key)
        else:
            self._highlight_next("")

    # ---------------- table ----------------

    def _refresh_spot_count(self):
        remaining = 0
        if self._unique_dialogs:
            remaining = len(spot_check_picks(
                self.plan, self._unique_dialogs, audio_dir=FISH_AUDIO_DIR,
                directions=self.directions, overrides=self.overrides,
                generation=self.generation, patcher_config=self.patcher_config,
                pronunciation=self.pronunciation))
        flagged = len(self._spot_flagged()[0])
        parts = [f"{remaining:,} left"] if remaining else []
        if flagged:
            parts.append(f"{flagged:,} flagged")
        self.spot_check_btn.setText(f"Spot check ({', '.join(parts)})" if parts else "Spot check")
        self._spot_count_dirty = False

    @staticmethod
    def _count_button(label: str, missing: int, stale: int) -> str:
        parts = []
        if missing:
            parts.append(f"{missing:,}")
        if stale:
            parts.append(f"{stale:,} stale")
        return f"{label} ({', '.join(parts)})" if parts else label

    def _line_voice(self, p: PlanRow) -> str:
        directed = getattr(self, "directions", {}).get(p.strref, {}).get("voice")
        return directed or p.voice or voice_for(p.speaker)

    def _spoken(self, p: PlanRow) -> str:
        return self.overrides.get(p.strref, p.text)

    def _has_drama3_speech(self, p: PlanRow) -> bool:
        segs = drama3_segments(self._spoken(p), p, self.patcher_config, self.pronunciation)
        return any(voice != NARRATOR for voice, _ in segs)

    def _has_audio(self, p: PlanRow) -> bool:
        return (FISH_AUDIO_DIR / f"{p.resref}.WAV").exists()

    def _current_mood(self, p: Optional[PlanRow] = None) -> str:
        p = p or self.current
        return self.moods.get(p.strref, {}).get("Mood") or self.directions.get(p.strref, {}).get("mood", "")

    def _directed(self, p: PlanRow) -> List[Dict[str, str]]:
        return directed_segments(p, self._spoken(p), self.patcher_config, self.pronunciation,
                                self.directions, self._current_mood(p))

    def _stale_audio_reasons(self, p: PlanRow) -> List[str]:
        if not self._has_audio(p):
            return []
        return audio_stale_reasons(p, self._spoken(p), self.patcher_config, self.pronunciation,
                                   self.directions, self._current_mood(p), self.generation.get(p.strref))

    def _stale_direct_reasons(self, p: PlanRow) -> List[str]:
        entry = self.directions.get(p.strref)
        if not entry:
            return []
        return direction_stale_reasons(p, self._spoken(p), self.patcher_config, self.pronunciation, entry)

    def _is_stale(self, p: PlanRow) -> bool:
        return bool(self._stale_audio_reasons(p))

    def _filtered(self) -> List[PlanRow]:
        speaker = self.speaker_box.currentText()
        voice = self.voice_box.currentText()
        query = self.search.text().strip().lower()
        mode = self.only_audio.currentText()
        rows = []
        spot_batch = set()
        if mode == "Spot check batch" and SPOT_LINES_FILE.exists():
            spot_batch = {int(x) for x in SPOT_LINES_FILE.read_text(encoding="utf-8").split() if x.isdigit()}
        for p in self.plan:
            if speaker != "All characters" and p.speaker != speaker:
                continue
            if voice != "All voices" and self._line_voice(p) != voice:
                continue
            if query:
                static = self._hay_static.get(p.strref)
                if static is None:
                    static = self._hay_static[p.strref] = f"{p.text} {p.strref} {p.original}".lower()
                note = self.notes_map.get(p.strref, "")
                codes = " ".join(f.code for f in self.flags.get(p.strref, []))
                if query not in static and query not in f"{note} {codes}".lower():
                    continue
            if mode == "Spot check batch" and p.strref not in spot_batch:
                continue
            if mode == "Needs review" and not self._needs_review(p):
                continue
            if mode == "Flagged" and p.strref not in self.flags:
                continue
            if mode == "Reviewed" and p.strref not in self.reviewed:
                continue
            has_audio = self._has_audio(p)
            directed = p.strref in self.directions
            if mode == "Need directing":
                if not needs_direction(p, self._spoken(p)):
                    continue
                if directed and not self._stale_direct_reasons(p):
                    continue
                if not directed and not self._has_drama3_speech(p):
                    continue    # sound effects / narrator-only: nothing for the director
            if mode == "Need generating" and has_audio and not self._is_stale(p):
                continue
            if mode == "Generated" and not has_audio:
                continue
            if mode == "Edited" and p.strref not in self.overrides:
                continue
            if mode == "Has notes" and p.strref not in self.notes_map:
                continue
            if mode == "Needs regen" and not self._is_stale(p):
                continue
            if mode == "Stale directions" and not self._stale_direct_reasons(p):
                continue
            if mode == "Examine lines" and not p.is_infopoint:
                continue
            if mode == "Idle barks" and not is_float(p):
                continue
            if mode == "Original VO" and not p.original:
                continue
            rows.append(p)
        return rows

    def _row_values(self, p: PlanRow) -> List[str]:
        has_audio = self._has_audio(p)
        audio = "⚠" if has_audio and self._is_stale(p) else ("✓" if has_audio else "")
        if p.original:
            directed = "OG"
        elif p.strref not in self.directions:
            directed = ""
        elif self._stale_direct_reasons(p):
            directed = "⚠"
        else:
            directed = "✓"
        return [str(p.strref), p.speaker, self._line_voice(p) or "", directed,
                audio, "✎" if p.strref in self.overrides else "",
                "📝" if p.strref in self.notes_map else "", self._review_mark(p), p.text.replace("\n", " ")]

    def _fill_row(self, i: int, p: PlanRow):
        for c, v in enumerate(self._row_values(p)):
            item = QTableWidgetItem(v)
            item.setData(Qt.UserRole, p.strref)
            self.table.setItem(i, c, item)

    def _on_header_clicked(self, col: int):
        """Click a column header: sort by it; click again to reverse; third click restores plan order."""
        if col != self._sort_col:
            self._sort_col, self._sort_order = col, Qt.AscendingOrder
        elif self._sort_order == Qt.AscendingOrder:
            self._sort_order = Qt.DescendingOrder
        else:
            self._sort_col = -1
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(self._sort_col >= 0)
        if self._sort_col >= 0:
            header.setSortIndicator(self._sort_col, self._sort_order)
        self._refresh_table(reload_pronunciation=False)

    def _sort_rows(self, rows: List[PlanRow]) -> List[PlanRow]:
        col = self._sort_col
        if col < 0:
            return rows
        if col == 0:
            key = lambda p: p.strref
        else:
            key = lambda p: self._row_values(p)[col].lower()
        return sorted(rows, key=key, reverse=self._sort_order == Qt.DescendingOrder)

    def _refresh_table(self, *_args, reload_pronunciation: bool = True):
        self._search_timer.stop()
        if reload_pronunciation:
            self.pronunciation = load_pronunciation()
        selected = self.current.strref if self.current else None
        rows = self._sort_rows(self._filtered())
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(rows))
            for i, p in enumerate(rows):
                self._fill_row(i, p)
                if p.strref == selected:
                    self.table.selectRow(i)
        finally:
            self.table.setUpdatesEnabled(True)

    def _sync_current_row(self):
        p = self.current
        if not p:
            return
        if p.strref not in {r.strref for r in self._filtered()}:
            self._refresh_table()
            return
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.data(Qt.UserRole) == p.strref:
                self._fill_row(i, p)
                return

    def _on_select(self):
        items = self.table.selectedItems()
        if not items:
            return
        strref = items[0].data(Qt.UserRole)
        if self.current and self.current.strref == strref:
            self._update_preview()
            self._update_play_buttons()
            return
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        self.current = next(p for p in self.plan if p.strref == strref)
        p = self.current
        self.title.setText(f"{p.speaker} · {p.strref} · {p.resref}")
        self.game_text.setPlainText(p.text)
        self.edit_text.blockSignals(True)
        self.edit_text.setPlainText(self._spoken(p))
        self.edit_text.blockSignals(False)
        self.notes.blockSignals(True)
        self.notes.setPlainText(self.notes_map.get(p.strref, ""))
        self.notes.blockSignals(False)
        self._update_preview()
        self._set_enabled(True)
        self._update_play_buttons()
        self._load_direction()
        self._load_cast()
        self._load_review()
        self._load_context()

    # ---------------- review ----------------

    def _reaudit(self, rows: Optional[List[PlanRow]] = None):
        """Recompute pst_review flags for every line (or just `rows`)."""
        found = audit(rows if rows is not None else self.plan, self.overrides, self.directions, self.generation,
                      self.notes_map, FISH_AUDIO_DIR, self.patcher_config, self.pronunciation, self.moods)
        if rows is None:
            self.flags = found
            return
        for p in rows:
            self.flags.pop(p.strref, None)
        self.flags.update(found)

    def _needs_review(self, p: PlanRow) -> bool:
        return needs_review(self.flags.get(p.strref, []), self.reviewed.get(p.strref))

    def _review_mark(self, p: PlanRow) -> str:
        if self._needs_review(p):
            return "⚑"
        if p.strref in self.reviewed:
            return "✓"
        return "·" if p.strref in self.flags else ""

    def _load_review(self):
        p = self.current
        if not p:
            self.review_info.setText("")
            return
        flags = self.flags.get(p.strref, [])
        parts = []
        for f in flags:
            color = "#b45309" if f.severity == "review" else "#6b6b70"
            parts.append(f'<span style="color:{color}">{"⚑" if f.severity == "review" else "·"} '
                         f'<b>{f.code}</b>: {self._esc(f.text)}</span>')
        mark = self.reviewed.get(p.strref)
        if mark:
            parts.append(f'<span style="color:#166534">✓ reviewed {mark.get("at", "")}</span>')
        self.review_info.setText("<br>".join(parts) or '<span style="color:#6b6b70">No review flags</span>')
        self.review_btn.setText("Unmark reviewed" if mark else "✓ Mark reviewed")

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _toggle_reviewed(self):
        p = self.current
        if not p:
            return
        if p.strref in self.reviewed:
            self.reviewed.pop(p.strref)
        else:
            mark_reviewed(self.reviewed, p.strref, self.flags.get(p.strref, []))
        save_reviewed(self.reviewed)
        self._load_review()
        self._sync_current_row()

    def _dialog_context(self):
        if self._context is None:
            import csv as _csv

            from pst_classify import DialogContext
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
                    self._context = DialogContext(list(_csv.DictReader(f)), Path(cfg.EXTRACT_DIR))
            finally:
                QGuiApplication.restoreOverrideCursor()
        return self._context

    def _load_context(self):
        p = self.current
        if not p or p.is_infopoint or is_float(p):
            self.context_view.setPlainText("(no conversation: examine line or idle bark)" if p else "")
            return
        try:
            steps = self._dialog_context().get(p.strref)[-CONTEXT_LINES:]
        except Exception as e:      # no extracted DLGs / dialog report
            self.context_view.setPlainText(f"(conversation unavailable: {e})")
            return
        lines = [f"{'▶ ' if who == PLAYER_LABEL else ''}{who or '?'}: {' '.join(text.split())}" for who, text in steps]
        self.context_view.setPlainText("\n".join(lines) or "(first line of the conversation)")
        self.context_view.verticalScrollBar().setValue(self.context_view.verticalScrollBar().maximum())

    def _open_conversation(self):
        p = self.current
        if not p:
            return
        dlg = self.dialog_of.get(p.strref, "")
        if not dlg:
            QMessageBox.information(self, "No conversation", "This line is not in a DLG (examine line or idle bark).")
            return
        if self._texts is None:
            self._texts = load_texts()
        out = conversation_html(dlg, self._texts, self.plan, self.directions, self.flags, focus=p.strref)
        if not out:
            QMessageBox.warning(self, "No DLG", f"{dlg}.DLG not found under {cfg.EXTRACT_DIR}.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(out.resolve())))
        self._log(f"Opened {out} (written to {CONVERSATION_DIR})")

    def _shown_rows(self) -> List[PlanRow]:
        return self._filtered()

    def _direct_shown(self):
        rows = [p for p in self._shown_rows() if needs_direction(p, self._spoken(p))]
        if not rows:
            QMessageBox.information(self, "Direct shown", "No line in the table has speech to direct.")
            return
        if QMessageBox.question(self, "Direct shown",
                                f"Ask Strata again for {len(rows):,} lines? Their current directions "
                                "(manual edits too) are replaced.") != QMessageBox.Yes:
            return
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        SHOWN_DIRECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHOWN_DIRECT_FILE.write_text("\n".join(str(p.strref) for p in rows), encoding="utf-8")
        self._run([sys.executable, "pst_direct.py", "--strref-file", str(SHOWN_DIRECT_FILE), "--force"], FISH_ENV,
                  f"Re-directing {len(rows):,} lines…", self._after_direct_missing)

    def _generate_shown(self):
        rows = [p for p in self._shown_rows() if p.speaker]
        if not rows:
            return
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            return
        if QMessageBox.question(self, "Regenerate shown",
                                f"Regenerate {len(rows):,} lines with Drama 3 (existing WAVs are replaced)?"
                                ) != QMessageBox.Yes:
            return
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        SHOWN_GENERATE_FILE.write_text("\n".join(str(p.strref) for p in rows), encoding="utf-8")
        self._run([sys.executable, "pst_fish.py", "--strref-file", str(SHOWN_GENERATE_FILE), "--force",
                   "--no-player"], FISH_ENV, f"Regenerating {len(rows):,} lines with Drama 3…",
                  self._after_generate)

    # ---------------- cast (hand-picked speaker / voice) ----------------

    def _fill_cast_choices(self):
        speakers = sorted(({p.speaker for p in self.plan} | profile_voices() | set(load_voice_aliases()))
                          - {NARRATOR})
        self.cast_speaker.blockSignals(True)
        self.cast_speaker.clear()
        self.cast_speaker.addItems(speakers)
        self.cast_speaker.blockSignals(False)
        self.cast_voice.blockSignals(True)
        self.cast_voice.clear()
        self.cast_voice.addItem(OWN_VOICE, "")
        for name in sorted(profile_voices() - {NARRATOR}):
            self.cast_voice.addItem(f"{name} (clone)", name)
        banned = set(load_rules().get("exclude_voices", []))
        for vid, v in sorted(self.library.items()):
            if vid in banned and vid != (self.current.voice if self.current else ""):
                continue    # pst-cast-rules.json exclude_voices (child voices, ...)
            label = v.get("name") or ""
            self.cast_voice.addItem(f"{vid} — {label}" if label and label != vid else vid, vid)
        self.cast_voice.blockSignals(False)

    def _picked_voice(self) -> str:
        """Voice id from the voice box ("" = speaker's own voice)."""
        text = self.cast_voice.currentText().strip()
        idx = self.cast_voice.findText(text)
        if idx >= 0:
            return self.cast_voice.itemData(idx) or ""
        if text in ("", OWN_VOICE):
            return ""
        return text.split(" — ", 1)[0].strip()

    def _cast_key(self, p: PlanRow):
        """("dialogs", DLG) for the line's conversation, or ("lines", strref) when it has none
        (examine lines, idle barks)."""
        dlg = self.dialog_of.get(p.strref, "")
        return ("dialogs", dlg.upper()) if dlg else ("lines", str(p.strref))

    def _cast_where(self, p: PlanRow) -> str:
        kind, key = self._cast_key(p)
        if kind == "lines":
            return f"line {p.strref} (no conversation)"
        count = sum(1 for row in self.plan if self.dialog_of.get(row.strref, "").upper() == key)
        return f"conversation {key} ({count:,} lines)"

    def _load_cast(self):
        p = self.current
        if not p:
            return
        self.cast_speaker.setCurrentText(p.speaker)
        idx = self.cast_voice.findData(p.voice or "")
        if idx >= 0:
            self.cast_voice.setCurrentIndex(idx)
        else:
            self.cast_voice.setCurrentText(p.voice)
        self._update_cast_info()

    def _cast_entries(self, p: PlanRow) -> List[Dict]:
        """Hand picks that touch this line: its conversation's, plus any older per-line pick."""
        kind, key = self._cast_key(p)
        found = []
        if kind == "dialogs" and key in self.cast_overrides["dialogs"]:
            found.append(self.cast_overrides["dialogs"][key])
        if str(p.strref) in self.cast_overrides["lines"]:
            found.append(self.cast_overrides["lines"][str(p.strref)])
        return found

    def _update_cast_info(self):
        p = self.current
        if not p:
            self.cast_info.setText("")
            return
        entries = self._cast_entries(p)
        if entries:
            e = entries[-1] if len(entries) == 1 else {**entries[0], **entries[1]}
            parts = [f"speaker {e['speaker']}" if e.get("speaker") else "",
                     f"voice {e['voice']}" if e.get("voice") else ""]
            head = "✋ Hand-picked: " + ", ".join(x for x in parts if x) + \
                   (f" (set {e['set_at']})" if e.get("set_at") else "")
        else:
            head = "From casting.json / dialog report"
        text = f"{head} · applies to {self._cast_where(p)} · speech voice now: {p.speech_voice}"
        self.cast_info.setStyleSheet("color: #1d4ed8;" if entries else "color: #6b6b70;")
        self.cast_info.setText(text)
        self.cast_remove_btn.setEnabled(bool(entries))

    def _drop_line_picks(self, dlg: str):
        """Per-line picks inside a conversation would beat the conversation pick; drop them."""
        for strref, d in self.dialog_of.items():
            if d.upper() == dlg:
                self.cast_overrides["lines"].pop(str(strref), None)

    def _apply_recast(self):
        p = self.current
        if not p:
            return
        if self.process is not None:
            QMessageBox.information(self, "Job running", "Wait for the current job to finish (or stop it) first.")
            return
        speaker = self.cast_speaker.currentText().strip()
        voice = self._picked_voice()
        own = profile_voices() | set(load_voice_aliases())
        if not speaker:
            QMessageBox.warning(self, "Recast", "Pick a speaker.")
            return
        if voice and voice not in self.library and voice not in own:
            QMessageBox.warning(self, "Recast", f"Unknown voice {voice!r}: pick a library voice or a clone.")
            return
        if not voice and speaker not in own:
            QMessageBox.warning(self, "Recast",
                                f"{speaker} has no voice of their own (no clone or alias); pick a voice too.")
            return
        kind, key = self._cast_key(p)
        where = self._cast_where(p)
        if kind == "dialogs":
            self._drop_line_picks(key)
        self.cast_overrides[kind][key] = {"speaker": speaker, "voice": voice or voice_for(speaker),
                                          "set_at": self._now_iso()}
        save_cast_overrides(self.cast_overrides)
        self._log(f"\u270b Recast {where}: {speaker} \u00b7 {voice or voice_for(speaker)} "
                  f"(saved to {CAST_OVERRIDES_PATH})")
        self._rebuild_after_recast(p.strref)

    def _remove_recast(self):
        p = self.current
        if not p:
            return
        if self.process is not None:
            QMessageBox.information(self, "Job running", "Wait for the current job to finish (or stop it) first.")
            return
        if not self._cast_entries(p):
            return
        kind, key = self._cast_key(p)
        where = self._cast_where(p)
        self.cast_overrides[kind].pop(key, None)
        if kind == "dialogs":
            self._drop_line_picks(key)
        self.cast_overrides["lines"].pop(str(p.strref), None)
        save_cast_overrides(self.cast_overrides)
        self._log(f"Removed the hand pick for {where}; back to casting.json")
        self._rebuild_after_recast(p.strref)

    def _rebuild_after_recast(self, strref: int):
        """Rebuild plan.csv in-process and reselect the line so stale markers update at once."""
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._flush_edits(refresh=False)
            rebuild_plan(extract_originals=False)
        except Exception as e:  # keep the window alive; the overrides file is already saved
            self._log(f"❌ Plan rebuild failed: {e}. Click Plan to retry.")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._after_plan(reselect=strref)
        p = self.current
        if p:
            reasons = self._stale_audio_reasons(p) + self._stale_direct_reasons(p)
            self._log(f"Now {p.speaker} · {p.speech_voice}"
                      + (f"; stale: {'; '.join(reasons)}" if reasons else "; nothing stale"))

    # ---------------- editing ----------------

    def _on_spoken_changed(self):
        self._update_preview()
        if self.direction_edit.toPlainText().strip() == self._direction_loaded.strip():
            self._load_direction()
        self._save_timer.start()

    def _on_notes_changed(self):
        self._save_timer.start()

    def _flush_edits(self, refresh: bool = True):
        if not self.current:
            return
        self._save_spoken()
        self._save_notes()
        self._save_direction()
        self._reaudit([self.current])
        self._load_review()
        if refresh:
            self._sync_current_row()

    def _save_spoken(self):
        p = self.current
        text = self.edit_text.toPlainText().strip()
        if text == p.text.strip():
            if p.strref not in self.overrides:
                return
            self.overrides.pop(p.strref, None)
        elif self.overrides.get(p.strref) == text:
            return
        else:
            self.overrides[p.strref] = text
        save_line_overrides(self.overrides)

    def _save_notes(self):
        p = self.current
        text = self.notes.toPlainText().strip()
        if not text:
            if p.strref not in self.notes_map:
                return
            self.notes_map.pop(p.strref, None)
        elif self.notes_map.get(p.strref) == text:
            return
        else:
            self.notes_map[p.strref] = text
        save_line_notes(self.notes_map)

    @staticmethod
    def _format_rules(rules: List[Dict]) -> str:
        parts = []
        for r in rules:
            parts.append(r.get("comment") or f"{r.get('pattern', '')} → {r.get('replacement', '')}")
        return "; ".join(parts) if parts else "none"

    def _update_preview(self):
        if not self.current:
            return
        p = self.current
        spoken = self.edit_text.toPlainText()
        segs = tts_segments(spoken, p.speaker, self.patcher_config, self.pronunciation, p.voice)
        lines = []
        if p.original:
            lines.append(f"Keep original {p.original} (game VO)")
        for voice, text in segs:
            if p.original and voice != NARRATOR:
                lines.append(f"{voice}: (original audio)")
            else:
                lines.append(f"{voice}: {text}")
        self.preview.setPlainText("\n".join(lines) or "(nothing to speak)")
        snap = generation_snapshot(p, spoken, self.patcher_config, self.pronunciation)
        rec = self.generation.get(p.strref)
        has_audio = self._has_audio(p)
        audio_reasons = self._stale_audio_reasons(p)
        direct_reasons = self._stale_direct_reasons(p)
        if rec:
            orig = rec.get("original") or ""
            if orig:
                extra = " + narrator" if any(s.get("source") != "original" for s in rec.get("segments") or []) else ""
                gen = f"Kept original {orig}{extra}"
            else:
                gen = (f"Generated as {rec.get('speaker')} · "
                       f"{rec.get('director') or rec.get('voice_mode') or 'Drama 3'}")
        elif has_audio:
            gen = "Generated (no generation.json record yet)"
        else:
            gen = "Not generated yet"
        now = (f"Now: {snap['speaker']} (voice {snap['voice']}); "
               f"pronunciation: {self._format_rules(snap['rules'])}")
        bits = []
        if audio_reasons:
            bits.append("Needs regen: " + "; ".join(audio_reasons))
        elif has_audio:
            bits.append("Audio matches current line")
        if direct_reasons:
            bits.append("Direction stale: " + "; ".join(direct_reasons))
        status = "\n".join(bits)
        self.gen_info.setStyleSheet("color: #b45309;" if (audio_reasons or direct_reasons) else "color: #6b6b70;")
        self.gen_info.setText("\n".join(t for t in (gen, now, status) if t))

    def _revert_override(self):
        self.edit_text.setPlainText(self.current.text)
        self._save_timer.stop()
        self._flush_edits()

    # ---------------- Drama 3 direction ----------------

    def _speech_segments(self) -> List[Dict[str, str]]:
        """Drama 3 segments of the current spoken text, speech only (narration has a fixed direction)."""
        return [s for s in self._directed(self.current) if s["voice"] != NARRATOR]

    def _load_direction(self):
        segs = self._speech_segments()
        text = "\n".join(s["text"] for s in segs)
        self._direction_loaded = text
        self.direction_edit.blockSignals(True)
        self.direction_edit.setPlainText(text)
        self.direction_edit.blockSignals(False)
        entry = self.directions.get(self.current.strref, {})
        sources = sorted({s["source"] for s in segs})
        parts = [", ".join(sources) or "no speech"]
        if any(src.startswith("director") for src in sources):
            parts += [entry.get("source", ""), entry.get("reason", "")]
        stale = self._stale_direct_reasons(self.current)
        if stale:
            parts.append("stale: " + "; ".join(stale))
        self.direction_info.setText(" · ".join(x for x in parts if x))

    def _on_direction_changed(self):
        self._save_timer.start()

    def _save_direction(self):
        text = self.direction_edit.toPlainText().strip()
        if text == self._direction_loaded.strip():
            return
        p = self.current
        segs = self._speech_segments()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) != len(segs):
            self.direction_info.setText(f"⚠ {len(segs)} speech segment(s) need {len(segs)} line(s); not saved")
            return
        self.directions[p.strref] = {
            **direction_fields(p, self._spoken(p), self.patcher_config, self.pronunciation),
            "source": "manual", "mood": self._current_mood(),
            "reason": "edited in PST Revoice", "valid": True,
            "segments": [{"voice": s["voice"], "base": s["base"], "text": line} for s, line in zip(segs, lines)]}
        save_directions(self.directions)
        self._direction_loaded = text
        changed = [str(i + 1) for i, (s, line) in enumerate(zip(segs, lines))
                   if not sanitize_directed(s["base"], line)[1]]
        self.direction_info.setText("manual" if not changed else
                                    f"manual · ⚠ line {', '.join(changed)} changes the words: only its "
                                    "[direction] will be used (edit the spoken text above to change words)")
        self._refresh_status()

    def _direct(self):
        p = self.current
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        self._run([sys.executable, "pst_direct.py", "--strref", str(p.strref), "--force"], FISH_ENV,
                  f"Asking Strata to direct {p.strref}…", self._after_direct_one)

    def _after_direct_one(self):
        self.directions = load_directions()
        self._spot_count_dirty = True
        self._load_direction()
        self._refresh_table()
        self._refresh_status()

    # ---------------- pipeline jobs ----------------

    def _open_config(self):
        QProcess.startDetached(sys.executable, ["config_gui.py"], os.getcwd())
        self._log("Opened game config. Point it at classic PST, save, then Dialog report.")

    def _dialog_report(self):
        self._run([sys.executable, "dialog-report-prepare.py"], FISH_ENV,
                  "Extracting the dialog report (slow)…", self._refresh_status)

    def _extract_voices(self):
        self._run([sys.executable, "profiles-prepare.py"], FISH_ENV,
                  "Extracting cloned voices into voices_pst/…", self._refresh_status)

    def _collect_moods(self):
        self._run([sys.executable, "pst_moods.py", "collect"], FISH_ENV,
                  "Collecting original clips for Drama 3 clone voices…", self._refresh_status)

    def _build_moods(self):
        self._run([sys.executable, "pst_moods.py", "build"], FISH_ENV,
                  "Building Drama 3 clone refs in voices_moods_pst/…", self._refresh_status)

    def _build_voicelib(self):
        self._run_steps(
            [([sys.executable, "voicelib_nwn.py"], "Building the NWN voice library…"),
             ([sys.executable, "voicelib_wow.py"], "Building the WoW voice library…")],
            self._refresh_status)

    def _cast_npcs(self):
        if strata_status() != "loaded":
            QMessageBox.information(
                self, "Strata is not loaded",
                "Start PolyStrata first (PolyStrata-main\\run-qwen36-ud-q4_k_m.bat), then click Cast NPCs.\n\n"
                "Already-cast dialogs are skipped.")
            self._refresh_status()
            return
        self._run_steps(
            [([sys.executable, "pst_npc_profiles.py"], "Gathering NPC dialog profiles…"),
             ([sys.executable, "pst_cast.py"], "Casting library voices (Strata; resumable)…"),
             ([sys.executable, "pst_cast_report.py"], "Writing pst_cast/report.html…")],
            self._after_cast)

    def _after_cast(self):
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self._open_cast_report()
        self._refresh_status()

    def _cast_report(self):
        self._run([sys.executable, "pst_cast_report.py"], FISH_ENV,
                  "Writing pst_cast/report.html…", self._open_cast_report)

    def _open_cast_report(self):
        path = Path("pst_cast/report.html")
        if path.exists():
            os.startfile(str(path.resolve()))
        self._refresh_status()

    def _run_steps(self, steps: List, on_done):
        if not steps:
            on_done()
            return
        self._queued = steps[1:]
        self._queue_done = on_done
        cmd, message = steps[0]
        self._run(cmd, FISH_ENV, message, self._continue_queue)

    def _continue_queue(self):
        if self._queued:
            cmd, message = self._queued.pop(0)
            self._run(cmd, FISH_ENV, message, self._continue_queue)
            return
        done = self._queue_done
        self._queue_done = None
        if done:
            done()

    def _write_plan(self):
        self._run([sys.executable, "pst_revoice.py", "plan"], FISH_ENV,
                  "Writing the revoice plan…", self._after_plan)

    def _after_plan(self, reselect: Optional[int] = None):
        self.plan = [p for p in read_plan(PLAN_PATH) if p.action == "generate"] if PLAN_PATH.exists() else []
        self.cast_overrides = load_cast_overrides()
        self.dialog_of = dialog_of_strrefs()
        self.current = None
        self.speaker_box.blockSignals(True)
        self.speaker_box.clear()
        self.speaker_box.addItem("All characters")
        self.speaker_box.addItems(sorted({p.speaker for p in self.plan}))
        self.speaker_box.blockSignals(False)
        self._fill_cast_choices()
        self._set_enabled(False)
        self.title.setText("Select a line")
        self._unique_dialogs = load_unique_dialogs()
        self._spot_count_dirty = True
        self._refresh_table()
        self._refresh_status()
        if reselect is not None:
            self.table.clearSelection()     # same row index would not re-fire selection
            self._select_strref(reselect)

    def _direct_missing(self):
        self._run([sys.executable, "pst_direct.py"], FISH_ENV,
                  "Directing missing and stale lines (overnight is fine)…",
                  self._after_direct_missing)

    def _after_direct_missing(self):
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.directions = load_directions()
        self._spot_count_dirty = True
        self._refresh_table()
        if self.current:
            self._load_direction()
        self._refresh_status()

    def _spot_check(self):
        if self.process is not None or self._overnight_on:
            self._log("Busy — wait for the current job to finish, or click Stop job")
            return
        if not self.plan:
            QMessageBox.information(self, "No plan yet", "Click Plan first, then Spot check.")
            return
        if not self._unique_dialogs:
            self._unique_dialogs = load_unique_dialogs()
        if not self._unique_dialogs:
            QMessageBox.information(
                self, "No unique conversations",
                "Need the dialog report (or pst_cast/npcs.json) so conversations can be grouped.")
            return
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            self._refresh_status()
            return
        picks = spot_check_picks(
            self.plan, self._unique_dialogs, audio_dir=FISH_AUDIO_DIR,
            directions=self.directions, overrides=self.overrides,
            generation=self.generation, patcher_config=self.patcher_config,
            pronunciation=self.pronunciation)
        flagged, force = self._spot_flagged()
        if not picks and not flagged:
            QMessageBox.information(
                self, "Nothing left to spot check",
                "Every unique conversation already has a current voiced line and no line is waiting "
                "for review. Recast a speaker, or wait until a direction goes stale, to audit again.")
            return
        picked = [pick.row.strref for pick in picks]
        self._spot_strrefs = picked + [s for s in flagged if s not in set(picked)]
        self._spot_force = force
        by_ref = {p.strref: p for p in self.plan}
        to_direct = [s for s in self._spot_strrefs if self._spot_needs_direct(by_ref[s])]
        self._log(
            f"Spot check: {len(picks):,} conversations to sample + {len(flagged):,} lines waiting for "
            f"review ({len(force):,} flawed takes redone); {len(to_direct):,} need a direction first")
        if to_direct:
            SPOT_DIRECT_FILE.parent.mkdir(parents=True, exist_ok=True)
            SPOT_DIRECT_FILE.write_text("\n".join(map(str, to_direct)), encoding="utf-8")
            self._run([sys.executable, "pst_direct.py", "--strref-file", str(SPOT_DIRECT_FILE)], FISH_ENV,
                      f"Spot check: directing {len(to_direct):,} lines…",
                      self._spot_after_direct)
            return
        self._spot_generate()

    def _spot_flagged(self):
        """
        Lines in the review queue (Needs review, not yet marked reviewed) that a spot check should
        direct and generate: ([strrefs], [strrefs whose current WAV must be redone: flawed take / pace]).
        """
        by_ref = {p.strref: p for p in self.plan}
        lines, force = [], []
        for strref, flags in self.flags.items():
            p = by_ref.get(strref)
            if p is None or not needs_review(flags, self.reviewed.get(strref)):
                continue
            if not tts_segments(self._spoken(p), p.speaker, self.patcher_config, self.pronunciation, p.voice):
                continue
            lines.append(strref)
            if self._has_audio(p) and {f.code for f in flags} & REDO_TAKE_CODES:
                force.append(strref)
        return sorted(lines), sorted(force)

    def _spot_needs_direct(self, p: PlanRow) -> bool:
        spoken = self._spoken(p)
        if not needs_direction(p, spoken):
            return False
        if p.strref not in self.directions:
            return True
        return bool(self._stale_direct_reasons(p))

    def _spot_after_direct(self):
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.directions = load_directions()
        if self.current:
            self._load_direction()
        self._spot_generate()

    def _spot_generate(self):
        strrefs = self._spot_strrefs
        if not strrefs:
            return
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            self._refresh_status()
            return
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        SPOT_GENERATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPOT_GENERATE_FILE.write_text("\n".join(map(str, strrefs)), encoding="utf-8")
        SPOT_LINES_FILE.write_text("\n".join(map(str, strrefs)), encoding="utf-8")
        self._run([sys.executable, "pst_fish.py", "--strref-file", str(SPOT_GENERATE_FILE),
                   "--stale", "--no-player"],
                  FISH_ENV, f"Spot check: generating {len(strrefs):,} lines with Drama 3…",
                  self._spot_force_generate)

    def _spot_force_generate(self):
        """Second batch: flagged lines whose WAV is current but flawed (cut-off take, odd pace)."""
        force, self._spot_force = getattr(self, "_spot_force", []), []
        if not force:
            self._spot_after_generate()
            return
        SPOT_FORCE_FILE.write_text("\n".join(map(str, force)), encoding="utf-8")
        self._run([sys.executable, "pst_fish.py", "--strref-file", str(SPOT_FORCE_FILE), "--force", "--no-player"],
                  FISH_ENV, f"Spot check: redoing {len(force):,} flawed takes…", self._spot_after_generate)

    def _spot_after_generate(self):
        self.generation = load_generation(FISH_AUDIO_DIR / "generation.json")
        self._spot_count_dirty = True
        if self._spot_strrefs:
            self._select_strref(self._spot_strrefs[0])
        self._refresh_table()
        if self.current:
            self._update_preview()
            self._update_play_buttons()
            if self._has_audio(self.current):
                self._play(FISH_AUDIO_DIR)
        self._refresh_status()

    def _select_strref(self, strref: int):
        p = next((row for row in self.plan if row.strref == strref), None)
        if not p:
            return
        self.speaker_box.blockSignals(True)
        self.only_audio.blockSignals(True)
        self.search.blockSignals(True)
        idx = self.speaker_box.findText(p.speaker)
        if idx >= 0:
            self.speaker_box.setCurrentIndex(idx)
        else:
            self.speaker_box.setCurrentIndex(0)
        self.only_audio.setCurrentIndex(0)
        self.voice_box.setCurrentIndex(0)
        self.search.clear()
        self.speaker_box.blockSignals(False)
        self.only_audio.blockSignals(False)
        self.search.blockSignals(False)
        self._refresh_table()
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item and item.data(Qt.UserRole) == strref:
                self.table.selectRow(i)
                self.table.scrollToItem(item)
                return
        self.current = p

    def _generate_missing(self):
        if self._farm_configured():   # same lines, spread over every serving PC (and this one)
            self._farm_run()
            return
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            self._refresh_status()
            return
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        self._run([sys.executable, "pst_fish.py", "--stale", "--no-player"], FISH_ENV,
                  "Generating missing and stale lines (can run for days; up-to-date files are skipped)…",
                  self._after_generate)

    def _after_generate(self):
        self.generation = load_generation(FISH_AUDIO_DIR / "generation.json")
        self._spot_count_dirty = True
        self._refresh_table()
        if self.current:
            self._update_preview()
            self._update_play_buttons()
        self._refresh_status()

    def _transcribe_wow(self):
        if not WHISPER_PYTHON.exists():
            QMessageBox.warning(
                self, "Whisper venv missing",
                f"Need {WHISPER_PYTHON} (torch + transformers) to transcribe WoW clips.")
            return
        self._run([str(WHISPER_PYTHON), "voicelib_transcribe.py"], FISH_ENV,
                  "Transcribing WoW library clips that have no text yet…",
                  self._after_transcribe)

    def _after_transcribe(self):
        self._refresh_status()

    def _import_examine(self):
        self._run([sys.executable, "pst_infopoints.py", "--merge"], FISH_ENV,
                  "Importing remaining Narrator examine lines into the plan…", self._after_plan)

    def _generate_examine(self):
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            self._refresh_status()
            return
        if not any(p.is_infopoint for p in self.plan):
            QMessageBox.information(self, "No examine lines in the plan",
                                    "Click Import examine first (adds remaining ?-click lines to the plan).")
            return
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        self._run([sys.executable, "pst_fish.py", "--speaker", NARRATOR, "--stale", "--no-player"], FISH_ENV,
                  "Generating missing and stale Narrator examine lines with Drama 3…",
                  self._after_generate_examine)

    def _after_generate_examine(self):
        self._after_generate()

    def _import_floats(self):
        self._run([sys.executable, "pst_floats.py", "--merge"], FISH_ENV,
                  "Importing remaining BCS idle barks into the plan…", self._after_plan)

    def _generate_floats(self):
        if drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready", drama3_status())
            self._refresh_status()
            return
        if not any(is_float(p) for p in self.plan):
            QMessageBox.information(self, "No idle barks in the plan",
                                    "Click Import idle barks first (adds remaining FloatMessage lines).")
            return
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        self._run([sys.executable, "pst_fish.py", "--floats", "--stale", "--no-player"], FISH_ENV,
                  "Generating missing and stale idle barks with Drama 3…",
                  self._after_generate_floats)

    def _after_generate_floats(self):
        self._after_generate()

    def _overnight(self):
        if self.process is not None or self._overnight_on:
            self._log("Busy — wait for the current job to finish, or click Stop job")
            return
        status = collect_status(
            self.plan, self.directions, self.overrides, FISH_AUDIO_DIR,
            load_library(), strata_status(), drama3_status(),
            generation=self.generation, moods=self.moods,
            patcher_config=self.patcher_config, pronunciation=self.pronunciation,
            plan_stale=bool(plan_stale_reasons()))
        blocker = overnight_blocker(status)
        if blocker:
            QMessageBox.information(self, "Overnight cannot start yet", blocker)
            self._refresh_status()
            return
        steps = overnight_steps(status)
        if not steps:
            QMessageBox.information(self, "Nothing to do overnight",
                                    "Directing and audio are caught up. Listen, then Build or Sync.")
            return
        previous = load_overnight_state()
        resume = previous.get("status") in ("paused", "running")
        self._overnight_on = True
        save_overnight_state({
            "status": "running",
            "started_at": previous.get("started_at") or self._now_iso(),
            "resumed_at": self._now_iso() if resume else "",
            "step": steps[0].key,
            "steps": [s.key for s in steps],
        })
        self._log("Overnight: resuming from current status…" if resume else
                  f"Overnight: {len(steps)} steps, starting with {steps[0].key}")
        self._overnight_next()

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _overnight_pause(self, reason: str):
        self._overnight_on = False
        self._strata_on_ready = None
        save_overnight_state({**load_overnight_state(), "status": "paused",
                              "paused_at": self._now_iso(), "reason": reason})
        self._log(reason)
        self._refresh_status()

    def _overnight_next(self):
        if not self._overnight_on:
            return
        self.directions = load_directions()
        self.generation = load_generation(FISH_AUDIO_DIR / "generation.json")
        if PLAN_PATH.exists():
            self.plan = [p for p in read_plan(PLAN_PATH) if p.action == "generate"]
        status = collect_status(
            self.plan, self.directions, self.overrides, FISH_AUDIO_DIR,
            load_library(), strata_status(), drama3_status(),
            generation=self.generation, moods=self.moods,
            patcher_config=self.patcher_config, pronunciation=self.pronunciation,
            plan_stale=bool(plan_stale_reasons()))
        steps = overnight_steps(status)
        if not steps:
            self._overnight_on = False
            save_overnight_state({**load_overnight_state(), "status": "done",
                                  "finished_at": self._now_iso(), "step": ""})
            self._log("Overnight chain finished. Up-to-date files were skipped.")
            self._after_plan()
            return
        step = steps[0]
        save_overnight_state({**load_overnight_state(), "status": "running",
                              "step": step.key, "steps": [s.key for s in steps]})
        self.job_info.setText(f"Overnight: {step.label}")
        self._log(f"Overnight → {step.key}: {step.label}")
        if step.key == "ensure_strata":
            self._ensure_strata(self._overnight_next)
            return
        if step.key == "free_gpu":
            self._log("Unloading Strata so the next job can use the GPU…")
            unload_strata()
            QTimer.singleShot(500, self._overnight_next)
            return
        if step.key == "cast":
            self._run_steps(
                [([sys.executable, "pst_npc_profiles.py"], "Gathering NPC dialog profiles…"),
                 ([sys.executable, "pst_cast.py"], "Casting library voices (Strata; resumable)…"),
                 ([sys.executable, "pst_cast_report.py"], "Writing pst_cast/report.html…")],
                self._overnight_after_job)
            return
        cmd = {
            "plan": [sys.executable, "pst_revoice.py", "plan"],
            "import_examine": [sys.executable, "pst_infopoints.py", "--merge"],
            "import_floats": [sys.executable, "pst_floats.py", "--merge"],
            "direct": [sys.executable, "pst_direct.py"],
            "transcribe": [str(WHISPER_PYTHON), "voicelib_transcribe.py"],
            "generate": self._farm_cmd() if self._farm_configured()
            else [sys.executable, "pst_fish.py", "--stale", "--no-player"],
        }.get(step.key)
        if not cmd:
            self._overnight_pause(f"Overnight: unknown step {step.key}")
            return
        if step.key == "generate":
            if drama3_status() != "ready":
                self._overnight_pause(
                    f"Overnight paused: Drama 3 is not ready ({drama3_status()}). "
                    "Install it, then click Run overnight.")
                return
            self.player.stop()
            self.player.setSource(QUrl())
        if step.key == "transcribe" and not WHISPER_PYTHON.exists():
            self._overnight_pause(
                f"Overnight paused: Whisper venv missing ({WHISPER_PYTHON}). "
                "Click Run overnight after it is installed, or skip Transcribe WoW.")
            return
        self._run(cmd, FISH_ENV, step.label, self._overnight_after_job)
        if step.key == "generate" and cmd[1] == "pst_farm.py":
            self._farm_running = self.process is not None

    def _overnight_after_job(self):
        if self._queued:
            return
        self._overnight_next()

    def _ensure_strata(self, on_ready):
        state = strata_status()
        if state == "loaded":
            on_ready()
            return
        if state == "not running":
            if not start_strata_server():
                cmd = strata_server_cmd()
                self._overnight_pause(
                    "Overnight: PolyStrata is not installed. "
                    f"Expected {cmd[0] if cmd else 'POLYSTRATA_ROOT/serve/server.py'}.")
                return
            self._log("Started PolyStrata; waiting for the server…")
            self._strata_wait_tries = 0
            self._strata_on_ready = on_ready
            self.job_info.setText("Overnight: waiting for PolyStrata to start…")
            QTimer.singleShot(OVERNIGHT_POLL_MS, self._wait_strata_server)
            return
        self._run(LOAD_STRATA_CMD, FISH_ENV,
                  "Loading the Strata model (about two minutes)…", on_ready)

    def _wait_strata_server(self):
        if not self._overnight_on:
            return
        state = strata_status()
        if state == "loaded":
            on_ready = self._strata_on_ready
            self._strata_on_ready = None
            if on_ready:
                on_ready()
            return
        if state == "server up":
            on_ready = self._strata_on_ready
            self._strata_on_ready = None
            self._run(LOAD_STRATA_CMD, FISH_ENV,
                      "Loading the Strata model (about two minutes)…", on_ready)
            return
        self._strata_wait_tries += 1
        if self._strata_wait_tries >= OVERNIGHT_SERVER_TRIES:
            self._overnight_pause(
                "Overnight: PolyStrata did not start in time. "
                "Run PolyStrata-main\\run-qwen36-ud-q4_k_m.bat, then click Run overnight.")
            return
        self.job_info.setText(
            f"Overnight: waiting for PolyStrata ({self._strata_wait_tries}/{OVERNIGHT_SERVER_TRIES})…")
        QTimer.singleShot(OVERNIGHT_POLL_MS, self._wait_strata_server)

    def _sync_gemrb(self):
        cmd = gemrb_sync_cmd(Path.cwd())
        if not cmd:
            QMessageBox.information(
                self, "GemRB sync not found",
                "Expected D:\\gemrb\\tools\\sync_vve_voices.py.\n\n"
                "It should read --audio-dir audio_fish only (OmniVoice output is "
                "archived) and keep patching TLK sound resrefs only.")
            return
        self._run(cmd, FISH_ENV,
                  "Syncing WAVs and TLK sound resrefs into the classic install…",
                  self._refresh_status)

    def _build_mod(self):
        wavs = [p for p in FISH_AUDIO_DIR.glob("*.WAV") if p.suffix.lower() == ".wav"]
        if not wavs:
            QMessageBox.information(self, "No Drama 3 audio",
                                    f"Nothing in {FISH_AUDIO_DIR} yet. Generate missing first.")
            return
        if QMessageBox.question(
                self, "Build mod",
                f"Stage PST_Revoiced from {len(wavs)} files in {FISH_AUDIO_DIR}?") != QMessageBox.Yes:
            return
        self._run([sys.executable, "pst_revoice.py", "build"], FISH_ENV,
                  "Staging the WeiDU mod from audio_fish…", self._after_build)

    def _after_build(self):
        self._log("Mod is in mod-pst-revoice/")
        self._refresh_status()

    # ---------------- farm ----------------

    def _farm_sync(self):
        nodes = [n for n in load_farm_config().get("nodes", []) if n.get("enabled", True)]
        if not nodes:
            QMessageBox.information(self, "No farm PCs", "Add PCs to pst-farm.json (name + share) first.")
            return
        names = ", ".join(f"{n['name']} ({n['share']})" for n in nodes)
        if QMessageBox.question(
                self, "Update farm PCs",
                f"Copy the worker, speech.cpp and the Drama 3 model to:\n{names}\n\n"
                "Only changed files are copied. The first time this includes the 21 GB model "
                "(a few minutes per PC on gigabit). A PC whose worker is running keeps its model "
                "file locked: stop serving there first if the model itself changed.") != QMessageBox.Yes:
            return
        self._run([sys.executable, "pst_farm.py", "sync"], FISH_ENV,
                  "Updating the farm PCs (copies only what changed)…", self._refresh_farm)

    @staticmethod
    def _farm_configured() -> bool:
        return any(n.get("enabled", True) for n in load_farm_config().get("nodes", []))

    def _farm_cmd(self) -> List[str]:
        return [sys.executable, "pst_farm.py", "run", "--stale",
                "--local" if self.farm_local_box.isChecked() else "--no-local"]

    def _farm_run(self):
        use_local = self.farm_local_box.isChecked()
        if use_local and drama3_status() != "ready":
            QMessageBox.warning(self, "Drama 3 is not ready on this PC", drama3_status())
            return
        if use_local:
            self._log("Unloading Strata so Drama 3 can use the GPU…")
            unload_strata()
        self.player.stop()
        self.player.setSource(QUrl())
        self._run(self._farm_cmd(), FISH_ENV, "Farm: generating missing and stale lines on every serving PC…",
                  self._after_generate)
        self._farm_running = self.process is not None
        QTimer.singleShot(500, self._refresh_farm)

    def _farm_stop(self):
        """Ask the dispatcher to stop cleanly (takes are kept); kill it if it does not."""
        FARM_STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FARM_STOP_FLAG.write_text("stop", encoding="utf-8")
        self._log("Stopping the farm: this PC finishes its current segment; farm PCs finish the batches they "
                  "already have (collected on the next Farm generate).")
        proc = self.process

        def force():
            if self.process is proc and proc is not None:
                self._log("Farm did not stop in time; killing it")
                proc.kill()
        QTimer.singleShot(FARM_STOP_GRACE_MS, force)

    def _probe_farm_nodes(self, nodes: List[Dict]):
        """Read each farm PC's heartbeat in the background (an offline share can block for many seconds)."""
        def run():
            out = {}
            for n in nodes:
                try:
                    out[n["name"]] = {"status": read_json(FarmPaths(farm_node_root(n["share"])).status),
                                      "reachable": True}
                except OSError:
                    out[n["name"]] = {"status": None, "reachable": False}
                self._farm_probe = {**self._farm_probe, n["name"]: out[n["name"]]}
        if self._farm_probe_thread is None or not self._farm_probe_thread.is_alive():
            self._farm_probe_thread = threading.Thread(target=run, daemon=True)
            self._farm_probe_thread.start()

    @staticmethod
    def _farm_phase_text(node: Dict) -> tuple:
        """(status text, background colour) for a farm row."""
        phase = node.get("phase") or "offline"
        if not node.get("online"):
            return ("offline" if node.get("reachable", True) else "share unreachable"), "#f4f4f5"
        if not node.get("serving"):
            return "online, not serving", "#e4e4e7"
        return {
            "working": ("speaking", "#dcfce7"),
            "idle": ("serving, waiting", "#dbeafe"),
            "loading": ("loading Drama 3…", "#fef3c7"),
            "error": ("error", "#fee2e2"),
        }.get(phase, (phase, "#ffffff"))

    @staticmethod
    def _duration(seconds: Optional[float]) -> str:
        if not seconds:
            return "?"
        hours, rest = divmod(int(seconds), 3600)
        if hours >= 48:
            return f"{hours / 24:.1f} days"
        return f"{hours} h {rest // 60:02d} min" if hours else f"{rest // 60} min"

    def _refresh_farm(self):
        config = load_farm_config()
        state = read_json(FARM_STATE_PATH) or {}
        live = bool(state.get("running")) and time.time() - float(state.get("time", 0)) < 30
        if live:
            nodes = state.get("nodes", [])
        else:
            self._probe_farm_nodes([n for n in config.get("nodes", []) if n.get("enabled", True)])
            nodes = []
            if self.farm_local_box.isChecked():
                nodes.append({"name": FARM_LOCAL_NAME, "local": True, "online": False, "reachable": True,
                              "gpus": [DRAMA3_DEVICE or "this GPU"], "idle_note": "starts with Farm generate"})
            for n in config.get("nodes", []):
                if not n.get("enabled", True):
                    continue
                probe = self._farm_probe.get(n["name"], {})
                st = probe.get("status") or {}
                online = is_online(st)
                nodes.append({"name": n["name"], "online": online, "reachable": probe.get("reachable", True),
                              "serving": online and st.get("serving"), "phase": st.get("phase", ""),
                              "gpus": st.get("gpus") or [n.get("gpu", "")], "current": st.get("current") or {},
                              "batch": st.get("batch") or {}, "error": st.get("error", ""),
                              "seen_ago": heartbeat_age(st) if st else None,
                              "lines_done": (st.get("counts") or {}).get("lines", 0)})
        serving = [n for n in nodes if n.get("serving")]
        self.farm_table.setRowCount(len(nodes))
        for i, n in enumerate(nodes):
            text, colour = self._farm_phase_text(n)
            if n.get("idle_note") and not live:
                text, colour = n["idle_note"], "#f4f4f5"
            cur, batch = n.get("current") or {}, n.get("batch") or {}
            working = ""
            if cur:
                working = (f"{cur.get('speaker', '')} {cur.get('strref')} "
                           f"[{cur.get('n', '?')}/{cur.get('of', '?')}]: {cur.get('text', '')}")
            elif n.get("error"):
                working = n["error"]
            elif batch:
                working = f"batch {batch.get('done', 0)}/{batch.get('segments', '?')}"
            values = [n["name"], text, ", ".join(g for g in n.get("gpus", []) if g) or n.get("device", ""),
                      working, str(n.get("in_flight", "")) if live else "",
                      str(n.get("lines_done", 0) or ""),
                      f"{n.get('lines_per_minute', 0):.1f}" if live and n.get("lines_per_minute") else ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 1:
                    item.setBackground(QColor(colour))
                self.farm_table.setItem(i, col, item)
        names = ", ".join(n["name"] for n in serving) or "none"
        if live:
            eta = self._duration(state.get("eta_seconds"))
            self.farm_summary.setText(
                f"<b>Farm running</b> · <b>{len(serving)}</b> PC{'s' if len(serving) != 1 else ''} serving "
                f"({names}) · <b>{state.get('remaining', 0):,}</b> lines left ({state.get('in_flight', 0)} out on "
                f"PCs) · {state.get('done_run', 0):,} done this run · {state.get('lines_per_minute', 0):.1f} "
                f"lines/min · ETA <b>{eta}</b> · {state.get('voiced', 0):,}/{state.get('total_plan', 0):,} voiced"
                + (f" · {state['given_up']} failed" if state.get("given_up") else ""))
        else:
            farm_pcs = [n for n in nodes if not n.get("local")]
            ready = [n for n in farm_pcs if n.get("serving")]
            self.farm_summary.setText(
                f"Farm idle · <b>{len(ready)}</b> of {len(farm_pcs)} farm PCs serving"
                + (f" ({', '.join(n['name'] for n in ready)})" if ready else "")
                + " · Farm generate uses every serving PC" + (" and this one" if self.farm_local_box.isChecked()
                                                               else ""))

    # ---------------- audio ----------------

    def _play(self, folder: Path):
        path = folder / f"{self.current.resref}.WAV"
        if not path.exists():
            self._log(f"No audio yet: {path}")
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.player.play()

    def _regenerate(self):
        p = self.current
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        self.player.stop()
        self.player.setSource(QUrl())
        self._log("Unloading Strata so Drama 3 can use the GPU…")
        unload_strata()
        self._run([sys.executable, "pst_fish.py", "--strref", str(p.strref), "--force", "--no-player"], FISH_ENV,
                  f"Regenerating {p.strref} with Drama 3…",
                  self._after_regenerate)

    def _after_regenerate(self):
        self.generation = load_generation(FISH_AUDIO_DIR / "generation.json")
        self._spot_count_dirty = True
        self._refresh_table()
        if self.current:
            self._update_preview()
            self._update_play_buttons()
            self._play(FISH_AUDIO_DIR)
        self._refresh_status()

    # ---------------- processes ----------------

    def _run(self, cmd: List[str], env: Dict[str, str], message: str, on_done):
        if self.process is not None:
            self._log("Busy — wait for the current job to finish, or click Stop job")
            return
        self._log(message)
        self._after_job = on_done
        self._job_label = message
        self.job_info.setText(f"Job: {message}")
        self.process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        for k, v in env.items():
            environment.insert(k, v)
        environment.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(os.getcwd())
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        for w in self._job_buttons():
            w.setEnabled(False)
        self.stop_job_btn.setEnabled(True)
        self.process.start(cmd[0], cmd[1:])

    def _stop_job(self):
        self._queued = []
        self._queue_done = None
        if self._overnight_on:
            self._overnight_pause("Overnight paused. Click Run overnight to continue from here.")
        if self.process is None:
            return
        if self._farm_running:
            self._farm_stop()
            return
        self._log("Stopping job…")
        self.process.kill()

    def _read_output(self):
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        keep = ("✅", "⚠️", "❌", "🐟", "🎬", "🎙️", "📦", "🎧", "📋", "Error", "error", "] ",
                "Directing", "Skipping", "clips to transcribe", "Rebuilt", "Staged",
                "examine", "idle bark", "Synced", "unique examine", "unique idle",
                "→ ", "Farm", "segments", "went silent", "stopped serving", "changed while", "Stop")
        for line in text.splitlines():
            if "📡 DIRECT|" in line:
                self._show_direct_progress(line)
            elif any(k in line for k in keep) and "Warning" not in line:
                self._log(line.strip()[-240:])

    def _show_direct_progress(self, line: str):
        """Job label while directing: servers up, lines done, ETA, lines per server."""
        try:
            alive, servers, done, total, eta, per = line.split("📡 DIRECT|", 1)[1].strip().split("|", 5)
            eta = int(eta)
        except ValueError:
            return
        hours, rest = divmod(eta, 3600)
        text = f"{hours}h {rest // 60:02d}m" if hours else f"{rest // 60}m {rest % 60:02d}s"
        self.job_info.setText(f"Job: directing {done}/{total} on {alive}/{servers} Strata servers · "
                              f"ETA {text} · {per.replace(',', ', ')}")

    def _finished(self, code: int, _status: int):
        self._log("Done" if code == 0 else f"Failed (exit code {code})")
        self.generation = load_generation(FISH_AUDIO_DIR / "generation.json")
        self.directions = load_directions()
        self._reaudit()
        on_done = self._after_job
        self.process = None
        self._after_job = None
        self._farm_running = False
        for w in self._pipeline_buttons():
            w.setEnabled(True)
        self.stop_job_btn.setEnabled(False)
        self._set_enabled(bool(self.current))
        if code == 0 and on_done:
            on_done()
        else:
            self._queued = []
            self._queue_done = None
            if self._overnight_on:
                self._overnight_pause(
                    f"Overnight paused after a failed job (exit {code}). "
                    "Fix the error if needed, then click Run overnight to continue.")
        self._refresh_status()

    def _log(self, message: str):
        self.status.appendPlainText(message)

    def closeEvent(self, event):
        self._save_timer.stop()
        self._flush_edits(refresh=False)
        if self._overnight_on:
            save_overnight_state({**load_overnight_state(), "status": "paused",
                                  "paused_at": self._now_iso(),
                                  "reason": "GUI closed"})
            self._overnight_on = False
        if self.process is not None:
            if self._farm_running:   # the dispatcher would orphan This PC's worker; let it stop cleanly
                FARM_STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
                FARM_STOP_FLAG.write_text("stop", encoding="utf-8")
                self.process.waitForFinished(15000)
            self.process.kill()
        super().closeEvent(event)


def main():
    os.chdir(Path(__file__).resolve().parent)
    app = QApplication(sys.argv)
    window = RevoiceWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
