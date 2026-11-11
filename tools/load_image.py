#!/usr/bin/env python3
"""Build a program image for the chip's loadable program memory.

Usage: load_image.py [--sched A,B,C,D] OUT_PREFIX IN1.asm [IN2.asm ...]

Assembles the sources like tools/asm.py (labels are global across files), checks that the
program fits in the 256-word program memory, pads it with HALT, and writes
  OUT_PREFIX.bin   512 bytes, in the order the chip expects them (low byte of each word first);
                   with --sched, a 513th byte follows: the thread schedule (thread owning slot 0,1,2,3)
  OUT_PREFIX.hex   256 lines of 16-bit hex words, for $readmemh and for inspection
See docs/loading.md for how to send the bytes to the chip.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asm  # noqa: E402

RAM_WORDS = 256
HALT_WORD = 0xF000


def build_image(sources):
    text = "\n".join(Path(s).read_text() for s in sources)
    words, _ = asm.assemble(text)
    if len(words) > RAM_WORDS:
        raise SystemExit(f"program is {len(words)} words; program memory holds {RAM_WORDS}")
    return words + [HALT_WORD] * (RAM_WORDS - len(words))


def image_bytes(words):
    out = bytearray()
    for w in words:
        out += bytes([w & 0xFF, w >> 8])
    return bytes(out)


def sched_byte(text):
    """'0,1,0,2' -> the schedule byte (slot s in bits [2s+1:2s]); rejects illegal schedules."""
    s = [int(v) for v in text.split(",")]
    if len(s) != 4 or any(not 0 <= v <= 3 for v in s):
        raise SystemExit("--sched needs four thread numbers 0..3, e.g. 0,1,0,2")
    if any(s[i] == s[(i + 1) % 4] for i in range(4)):
        raise SystemExit("illegal schedule: a thread may not own two slots that follow each other "
                         "(slot 3 and slot 0 follow each other too)")
    return s[0] | s[1] << 2 | s[2] << 4 | s[3] << 6


def main():
    args = sys.argv[1:]
    sched = None
    if len(args) >= 2 and args[0] == "--sched":
        sched, args = sched_byte(args[1]), args[2:]
    if len(args) < 2:
        raise SystemExit(__doc__)
    prefix, srcs = args[0], args[1:]
    words = build_image(srcs)
    data = image_bytes(words) + (bytes([sched]) if sched is not None else b"")
    Path(prefix + ".bin").write_bytes(data)
    Path(prefix + ".hex").write_text("".join(f"{w:04x}\n" for w in words))
    used = sum(1 for w in words if w != HALT_WORD)
    extra = f", schedule byte {sched:#04x}" if sched is not None else ""
    print(f"image: {used} of {RAM_WORDS} words used{extra} -> {prefix}.bin, {prefix}.hex")


if __name__ == "__main__":
    main()
