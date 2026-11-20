"""
aurora - minimal readers for BioWare Aurora engine (Neverwinter Nights) files.

Covers what the voice-library tools need: KEY/BIF archives, TLK V3.0 string
tables, 2DA V2.0 tables and SSF (soundset) files. Nothing here writes files
back into the game.
"""

import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Resource type ids (subset)
RES_WAV = 4
RES_2DA = 2017
RES_SSF = 2060

# NWN soundset (SSF) slots, in file order (nwnlexicon.com/Voice_Set_Overview)
SSF_SLOTS = [
    "Attack", "Battlecry 1", "Battlecry 2", "Battlecry 3", "Heal Me", "Help", "Enemies Sighted",
    "Flee", "Taunt", "Guard Me", "Hold", "Attack Grunt 1", "Attack Grunt 2", "Attack Grunt 3",
    "Pain Grunt 1", "Pain Grunt 2", "Pain Grunt 3", "Near Death", "Death", "Poisoned",
    "Spell Failed", "Weapon Ineffective", "Follow Me", "Look Here", "Group Party", "Move Over",
    "Pick Lock", "Search", "Go Stealthy", "Can Do", "Cannot Do", "Task Complete", "Encumbered",
    "Selected", "Hello", "Yes", "No", "Stop", "Rest", "Bored", "Goodbye", "Thank You", "Laugh",
    "Cuss", "Cheer", "Something To Say", "Good Idea", "Bad Idea", "Threaten",
]


def _resref(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").lower()


class ResourceIndex:
    """
    Every resource reachable through one or more KEY files (later keys win),
    plus loose override folders (searched first).
    """

    def __init__(self, game_dir: Path, keys: Iterable[str], override_dirs: Iterable[Path] = ()):
        self.game_dir = Path(game_dir)
        self.overrides = [Path(d) for d in override_dirs if Path(d).is_dir()]
        # (resref, type) -> (bif path, resource index)
        self.entries: Dict[Tuple[str, int], Tuple[Path, int]] = {}
        self._bif_tables: Dict[Path, Dict[int, Tuple[int, int]]] = {}
        for key in keys:
            self._load_key(self.game_dir / key)

    def _load_key(self, path: Path) -> None:
        data = path.read_bytes()
        if data[:8] != b"KEY V1  ":
            raise ValueError(f"{path}: not a KEY V1 file")
        bif_count, key_count, file_off, key_off = struct.unpack_from("<IIII", data, 8)
        bifs: List[Path] = []
        for i in range(bif_count):
            _size, name_off, name_size, _drives = struct.unpack_from("<IIHH", data, file_off + i * 12)
            name = data[name_off:name_off + name_size].split(b"\x00", 1)[0].decode("ascii")
            bifs.append(self.game_dir / name.replace("\\", "/"))
        for i in range(key_count):
            base = key_off + i * 22
            resref = _resref(data[base:base + 16])
            res_type, res_id = struct.unpack_from("<HI", data, base + 16)
            self.entries[(resref, res_type)] = (bifs[res_id >> 20], res_id & 0xFFFFF)

    def _bif_table(self, bif: Path) -> Dict[int, Tuple[int, int]]:
        table = self._bif_tables.get(bif)
        if table is None:
            with bif.open("rb") as f:
                head = f.read(20)
                if head[:8] != b"BIFFV1  ":
                    raise ValueError(f"{bif}: not a BIFF V1 file")
                var_count, _fix, var_off = struct.unpack_from("<III", head, 8)
                f.seek(var_off)
                raw = f.read(var_count * 16)
            table = {}
            for i in range(var_count):
                res_id, offset, size, _t = struct.unpack_from("<IIII", raw, i * 16)
                table[res_id & 0xFFFFF] = (offset, size)
            self._bif_tables[bif] = table
        return table

    def names(self, res_type: int) -> List[str]:
        """Every resref of one type."""
        found = {r for r, t in self.entries if t == res_type}
        ext = {RES_WAV: ".wav", RES_2DA: ".2da", RES_SSF: ".ssf"}.get(res_type)
        if ext:
            for d in self.overrides:
                found |= {p.stem.lower() for p in d.iterdir() if p.suffix.lower() == ext}
        return sorted(found)

    def get(self, resref: str, res_type: int) -> Optional[bytes]:
        """Resource bytes, or None if it does not exist."""
        resref = resref.lower()
        ext = {RES_WAV: ".wav", RES_2DA: ".2da", RES_SSF: ".ssf"}.get(res_type)
        if ext:
            for d in self.overrides:
                p = d / f"{resref}{ext}"
                if p.exists():
                    return p.read_bytes()
        loc = self.entries.get((resref, res_type))
        if loc is None:
            return None
        bif, index = loc
        offset, size = self._bif_table(bif)[index]
        with bif.open("rb") as f:
            f.seek(offset)
            return f.read(size)


def read_tlk(path: Path, encoding: str = "cp1252") -> List[str]:
    """All strings of a TLK V3.0 file, indexed by strref."""
    data = Path(path).read_bytes()
    if data[:8] != b"TLK V3.0":
        raise ValueError(f"{path}: not a TLK V3.0 file")
    _lang, count, str_off = struct.unpack_from("<III", data, 8)
    result = []
    for i in range(count):
        flags, = struct.unpack_from("<I", data, 20 + i * 40)
        off, size = struct.unpack_from("<II", data, 20 + i * 40 + 28)
        result.append(data[str_off + off:str_off + off + size].decode(encoding, errors="replace")
                      if flags & 1 else "")
    return result


def read_2da(data: bytes) -> List[Dict[str, str]]:
    """Rows of a 2DA V2.0 table ('****' becomes ''), each with its row index as '_row'."""
    lines = data.decode("cp1252", errors="replace").splitlines()
    body = [ln for ln in lines[1:] if ln.strip()]
    if body and body[0].strip().upper().startswith("DEFAULT"):
        body = body[1:]
    cols = _split_2da(body[0])
    rows = []
    for ln in body[1:]:
        parts = _split_2da(ln)
        row = {"_row": parts[0]}
        for c, v in zip(cols, parts[1:]):
            row[c] = "" if v == "****" else v
        rows.append(row)
    return rows


def _split_2da(line: str) -> List[str]:
    out, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def read_ssf(data: bytes) -> List[Tuple[str, int]]:
    """(sound resref, strref) per soundset slot; strref -1 / 0xFFFFFFFF means none."""
    if data[:4] != b"SSF ":
        raise ValueError("not an SSF file")
    count, table_off = struct.unpack_from("<II", data, 8)
    entries = []
    for i in range(count):
        off, = struct.unpack_from("<I", data, table_off + i * 4)
        resref = _resref(data[off:off + 16])
        strref, = struct.unpack_from("<i", data, off + 16)
        entries.append((resref, strref))
    return entries


def wav_payload(data: bytes) -> bytes:
    """
    Strip NWN's fake 470-byte WAV header when present (it precedes the real
    RIFF or MP3 data); otherwise return the data unchanged.
    """
    if data[:4] == b"RIFF" and len(data) > 474 and data[470:474] == b"RIFF":
        return data[470:]
    if data[:4] == b"RIFF" and len(data) > 472 and data[470:472] in (b"\xff\xfb", b"\xff\xf3", b"ID"):
        return data[470:]
    return data
