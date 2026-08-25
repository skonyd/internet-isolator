"""Kalıcı veri kullanım günlüğü (gün bazlı kova).

state.json (`/run`) yeniden başlatmada/reboot'ta silinir; "bu ay ne kadar
tether verisi harcadım" sorusuna cevap vermek için oturumlar arası kalıcı bir
kayıt gerekir. Bu modül günlük rx/tx toplamlarını kullanıcının veri dizininde
küçük bir JSON dosyasında tutar (config.json ile aynı 0600 + chown duruşu).
"""
from __future__ import annotations

import json
import logging
import os
import time

from . import system
from .config import _data_dir_for

log = logging.getLogger("tether.usage")

_RETENTION_DAYS = 90


def _path() -> str:
    return os.path.join(_data_dir_for(system.real_user()), "usage.json")


def load() -> dict:
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        if system.is_root():
            import pwd
            try:
                pw = pwd.getpwnam(system.real_user())
                os.chown(os.path.dirname(path), pw.pw_uid, pw.pw_gid)
                os.chown(path, pw.pw_uid, pw.pw_gid)
            except (KeyError, OSError):
                pass
    except OSError as e:
        log.warning("kullanım günlüğü yazılamadı: %s", e)


def add(rx_delta: int, tx_delta: int) -> dict:
    """Bugünün kovasına rx/tx bayt ekler ve güncel veriyi döndürür."""
    if rx_delta <= 0 and tx_delta <= 0:
        return load()
    data = load()
    day = time.strftime("%Y-%m-%d")
    bucket = data.get(day, {"rx": 0, "tx": 0})
    bucket["rx"] = bucket.get("rx", 0) + max(0, rx_delta)
    bucket["tx"] = bucket.get("tx", 0) + max(0, tx_delta)
    data[day] = bucket
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for k in list(data.keys()):
        try:
            t = time.mktime(time.strptime(k, "%Y-%m-%d"))
        except ValueError:
            del data[k]
            continue
        if t < cutoff:
            del data[k]
    _save(data)
    return data


def today_bytes(data: dict | None = None) -> tuple[int, int]:
    data = load() if data is None else data
    b = data.get(time.strftime("%Y-%m-%d"), {})
    return b.get("rx", 0), b.get("tx", 0)


def month_bytes(data: dict | None = None) -> tuple[int, int]:
    data = load() if data is None else data
    prefix = time.strftime("%Y-%m")
    rx = sum(v.get("rx", 0) for k, v in data.items() if k.startswith(prefix))
    tx = sum(v.get("tx", 0) for k, v in data.items() if k.startswith(prefix))
    return rx, tx
