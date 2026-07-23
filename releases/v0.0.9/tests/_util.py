"""Testler için paylaşılan yardımcılar (harici bağımlılık yok).

- IsolatedPaths: config/state dosyalarını geçici dizine yönlendirir; gerçek
  kullanıcı yapılandırması ASLA test tarafından değiştirilmez.
- FakeRun: system.run yerine geçen, çalıştırılan komutları kaydeden ve
  yapılandırılabilir sonuç döndüren sahte çalıştırıcı (hiçbir gerçek komut
  çalışmaz → testler root/gerçek donanım gerektirmez).
"""
from __future__ import annotations

import tempfile
import os

from tether_isolator import config as cfg
from tether_isolator import state as st
from tether_isolator.system import RunResult


class IsolatedPaths:
    """Config ve state yollarını geçici dizine taşıyan bağlam.

    setUp/tearDown içinde kullanılır; orijinal modül sabitlerini geri yükler.
    """

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._orig = {
            "CONFIG_DIR": cfg.CONFIG_DIR,
            "CONFIG_FILE": cfg.CONFIG_FILE,
            "STATE_DIR": st.STATE_DIR,
            "STATE_FILE": st.STATE_FILE,
            "FALLBACK": st._FALLBACK,
        }
        cfg.CONFIG_DIR = os.path.join(d, "config")
        cfg.CONFIG_FILE = os.path.join(cfg.CONFIG_DIR, "config.json")
        st.STATE_DIR = os.path.join(d, "run")
        st.STATE_FILE = os.path.join(st.STATE_DIR, "state.json")
        st._FALLBACK = os.path.join(d, "state-fallback.json")
        return self

    def __exit__(self, *exc):
        cfg.CONFIG_DIR = self._orig["CONFIG_DIR"]
        cfg.CONFIG_FILE = self._orig["CONFIG_FILE"]
        st.STATE_DIR = self._orig["STATE_DIR"]
        st.STATE_FILE = self._orig["STATE_FILE"]
        st._FALLBACK = self._orig["FALLBACK"]
        self._tmp.cleanup()
        return False


class FakeRun:
    """system.run yerine geçer; komutları kaydeder, sonuç döndürür."""

    def __init__(self, default: RunResult | None = None):
        self.calls: list[list[str]] = []
        self.default = default or RunResult(0, "", "")
        self.rules: list[tuple] = []   # (predicate(cmd)->bool, RunResult)

    def add_rule(self, predicate, result: RunResult):
        self.rules.append((predicate, result))

    def __call__(self, cmd, *, check=True, timeout=30, dry_run=False):
        self.calls.append(list(cmd))
        if dry_run:
            return RunResult(0, "", "")
        for pred, res in self.rules:
            if pred(list(cmd)):
                return res
        return self.default

    def ran(self, *tokens) -> bool:
        """Kaydedilen çağrılardan herhangi biri verilen TÜM belirteçleri içeriyor mu?"""
        for c in self.calls:
            if all(t in c for t in tokens):
                return True
        return False

    def count(self, *tokens) -> int:
        return sum(1 for c in self.calls if all(t in c for t in tokens))
