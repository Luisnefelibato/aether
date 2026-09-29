"""Normalize text so ElevenLabs speaks naturally in Spanish."""

from __future__ import annotations

import re


def speakable(text: str) -> str:
    out = text or ""

    def _unit(num: str, word: str) -> str:
        return f"{num} {word}"

    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*mgb/?s\b",
        lambda m: _unit(m.group(1).replace(",", "."), "megabits por segundo"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*mb/?s\b",
        lambda m: _unit(m.group(1).replace(",", "."), "megabytes por segundo"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*gb/?s\b",
        lambda m: _unit(m.group(1).replace(",", "."), "gigabytes por segundo"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*(mgb|mbit)s?\b",
        lambda m: _unit(m.group(1).replace(",", "."), "megabits"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*mb\b",
        lambda m: _unit(m.group(1).replace(",", "."), "megabytes"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*gb\b",
        lambda m: _unit(m.group(1).replace(",", "."), "gigabytes"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*kb\b",
        lambda m: _unit(m.group(1).replace(",", "."), "kilobytes"),
        out,
    )
    out = re.sub(
        r"(?i)\b(\d+(?:[.,]\d+)?)\s*tb\b",
        lambda m: _unit(m.group(1).replace(",", "."), "terabytes"),
        out,
    )

    pairs = [
        (r"(?i)\bAETHER\b", "ADAM"),
        (r"(?i)\bAether\b", "Adam"),
        (r"(?i)\baether\b", "Adam"),
        (r"(?i)\bmgb/?s\b", "megabits por segundo"),
        (r"(?i)\bmb/?s\b", "megabytes por segundo"),
        (r"(?i)\bgb/?s\b", "gigabytes por segundo"),
        (r"(?i)\bmbps\b", "megabits por segundo"),
        (r"(?i)\bgbps\b", "gigabits por segundo"),
        (r"(?i)\bmgb\b", "megabit"),
        (r"(?i)\bmbit\b", "megabit"),
        (r"(?i)\bmb\b", "megabyte"),
        (r"(?i)\bgb\b", "gigabyte"),
        (r"(?i)\bkb\b", "kilobyte"),
        (r"(?i)\btb\b", "terabyte"),
        (r"(?i)\bcpu\b", "procesador"),
        (r"(?i)\bgpu\b", "tarjeta gráfica"),
        (r"(?i)\bram\b", "memoria ram"),
        (r"(?i)\bssd\b", "disco sólido"),
        (r"(?i)\bhdd\b", "disco duro"),
        (r"(?i)\burl\b", "enlace"),
        (r"(?i)\bapi\b", "interfaz de programación"),
        (r"(?i)\bok\b", "okey"),
        (r"(?i)\bwifi\b", "uai fai"),
        (r"(?i)\bhttps?://\S+", "enlace web"),
    ]
    for pattern, repl in pairs:
        out = re.sub(pattern, repl, out)

    return re.sub(r"\s{2,}", " ", out).strip()
