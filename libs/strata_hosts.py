"""
Strata (PolyStrata) servers the director can use: the local one plus any listed in pst-strata.json.

pst-strata.json: {"extra": ["http://192.168.1.17:8081", {"url": "http://host:8082", "api_key": "..."}]}. Each server is woken (model loaded) before use
and the model name is read from its /v1/models, so the servers may run different quantizations.
"""
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

HOSTS_PATH = Path("pst-strata.json")
DEFAULT_EXTRA = ["http://192.168.1.17:8081", "http://192.168.1.168:8082"]
LOAD_TIMEOUT = 600
API_KEYS: Dict[str, str] = {}     # server base -> api key, filled by load_hosts


def base_of(url: str) -> str:
    """'http://host:port/v1' -> 'http://host:port'."""
    return url.rstrip("/").rsplit("/v1", 1)[0].rstrip("/")


def auth_headers(url: str) -> Dict[str, str]:
    """Authorization header for a server that needs an API key (url may end in /v1)."""
    key = API_KEYS.get(base_of(url))
    return {"Authorization": f"Bearer {key}"} if key else {}


def load_hosts(local_url: str, path: Path = HOSTS_PATH) -> List[str]:
    """Server bases, local first, then the extras (written with the default on first use)."""
    extra = DEFAULT_EXTRA
    if path.exists():
        try:
            extra = list(json.loads(path.read_text(encoding="utf-8")).get("extra", []))
        except (ValueError, AttributeError):
            extra = DEFAULT_EXTRA
    else:
        path.write_text(json.dumps({"extra": DEFAULT_EXTRA}, indent=2) + "\n", encoding="utf-8")
    hosts: List[str] = []
    for entry in [local_url] + extra:
        url = entry.get("url", "") if isinstance(entry, dict) else entry
        base = base_of(url)
        if isinstance(entry, dict) and entry.get("api_key"):
            API_KEYS[base] = entry["api_key"]
        if base and base not in hosts:
            hosts.append(base)
    return hosts


def host_status(base: str) -> str:
    """'loaded', 'server up' (model unloaded) or 'not running'."""
    try:
        response = requests.get(f"{base}/health", headers=auth_headers(base), timeout=3)
        if not response.ok:
            return "not running"
        try:
            data = response.json()
        except ValueError:
            data = {}
        return "server up" if data.get("loaded") is False else "loaded"
    except requests.RequestException:
        return "not running"


def host_model(base: str) -> Optional[str]:
    """Model id the server serves, or None."""
    try:
        data = requests.get(f"{base}/v1/models", headers=auth_headers(base), timeout=5).json()["data"]
        return data[0]["id"] if data else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    try:                                    # /health is open even when /v1 needs a key
        return requests.get(f"{base}/health", timeout=3).json().get("model")
    except (requests.RequestException, ValueError):
        return None


def wake_host(base: str, log: Callable[[str], None] = print,
              start: Optional[Callable[[], bool]] = None) -> Optional[Tuple[str, str]]:
    """
    Make one server ready: start it if `start` is given and it is down, load the model if unloaded.

    Returns (base, model), or None if the server is unreachable or would not load.
    """
    state = host_status(base)
    if state == "not running" and start is not None and start():
        log(f"Started {base}; waiting for the server...")
        import time
        for _ in range(60):
            time.sleep(2)
            state = host_status(base)
            if state != "not running":
                break
    if state == "not running":
        log(f"Strata {base}: not reachable, skipping")
        return None
    if state == "server up":
        log(f"Strata {base}: waking the model (about two minutes)...")
        try:
            requests.post(f"{base}/load", json={}, headers=auth_headers(base), timeout=LOAD_TIMEOUT)
        except requests.RequestException as e:
            log(f"Strata {base}: load failed ({e}), skipping")
            return None
        if host_status(base) != "loaded":
            log(f"Strata {base}: still not loaded, skipping")
            return None
    model = host_model(base)
    if not model:
        log(f"Strata {base}: no model reported, skipping")
        return None
    log(f"Strata {base}: ready ({model})")
    return base, model
