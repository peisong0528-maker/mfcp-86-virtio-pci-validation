#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare actual MFCP #86 mapping records from two Twister serial logs."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

CAPABILITIES = {1: "common", 2: "notify", 3: "ISR", 4: "device-specific"}
EVENTS = {"init", "notify", "irq"}
MAP_PATTERN = re.compile(
    r"MFCP86 MAP bdf=(0x[0-9a-f]+) cap=(\d+) phys=(0x[0-9a-f]+) "
    r"len=(\d+) flags=(0x[0-9a-f]+)(?: p=([01]) rw=([01]) pcd=([01]) pwt=([01]))?\s*$"
)
EVENT_PATTERN = re.compile(r"MFCP86 EVENT bdf=(0x[0-9a-f]+) kind=(init|notify|irq)\s*$")


@dataclass(frozen=True)
class Mapping:
    phys: int
    length: int
    flags: int
    pte: tuple[int, ...]


def read_capture(path, expected_flags, require_pte):
    mappings = {}
    events = {}
    expected_pte = (1, 1, int(expected_flags == 0xA), 0)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := MAP_PATTERN.search(line):
            bdf, capability, phys, length, flags, *pte_bits = match.groups()
            key = (int(bdf, 16), int(capability))
            mapping = Mapping(
                int(phys, 16), int(length), int(flags, 16),
                tuple(int(bit) for bit in pte_bits if bit is not None),
            )
            if key in mappings:
                raise ValueError(f"{path}: duplicate map for BDF {key[0]:#x}, cap {key[1]}")
            if mapping.flags != expected_flags or mapping.length <= 0:
                raise ValueError(f"{path}: unexpected flags or length for {key}: {mapping}")
            if (require_pte or mapping.pte) and mapping.pte != expected_pte:
                raise ValueError(f"{path}: expected P/RW/PCD/PWT {expected_pte}: {mapping}")
            mappings[key] = mapping
        elif match := EVENT_PATTERN.search(line):
            bdf, event = match.groups()
            events.setdefault(int(bdf, 16), set()).add(event)
        elif "MFCP86 MAP " in line or "MFCP86 EVENT " in line:
            raise ValueError(f"{path}: malformed capture line: {line}")

    devices = {bdf for bdf, _ in mappings}
    complete = {
        bdf for bdf in devices
        if {cap for dev, cap in mappings if dev == bdf} == CAPABILITIES.keys()
        and EVENTS <= events.get(bdf, set())
    }
    if not complete:
        raise ValueError(f"{path}: no device has all four mappings and all three events")
    return mappings, events, complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--require-pte", action="store_true")
    args = parser.parse_args()
    try:
        before, before_events, before_devices = read_capture(args.before, 0x8, args.require_pte)
        after, after_events, after_devices = read_capture(args.after, 0xA, args.require_pte)
        if before.keys() != after.keys() or before_devices != after_devices:
            raise ValueError("Before and after observed different PCI capabilities or devices")
        for key, original in sorted(before.items()):
            fixed = after[key]
            if (original.phys, original.length) != (fixed.phys, fixed.length):
                raise ValueError(f"Physical address or length changed for {key}")
            if bool(original.pte) != bool(fixed.pte):
                raise ValueError(f"PTE capture is asymmetric for {key}")
            bdf, capability = key
            label = CAPABILITIES.get(capability, str(capability))
            print(
                f"{bdf:#x} {label}: phys={fixed.phys:#x} length={fixed.length} "
                f"flags={original.flags:#x} -> {fixed.flags:#x}"
                + (f" PCD={original.pte[2]} -> {fixed.pte[2]}" if fixed.pte else "")
            )
        for bdf in sorted(before_devices):
            print(
                f"{bdf:#x}: before events={','.join(sorted(before_events[bdf]))}; "
                f"after events={','.join(sorted(after_events[bdf]))}"
            )
        print("PASS: real driver mapping capture before and after")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

