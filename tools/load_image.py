#!/usr/bin/env python3
"""Build a program image for the chip's loadable program memory.

Usage: load_image.py OUT_PREFIX IN1.asm [IN2.asm ...]

Assembles the sources like tools/asm.py (labels are global across files), checks that the
program fits in the 256-word program memory, pads it with HALT, and writes
  OUT_PREFIX.bin   512 bytes, in the order the chip expects them (low byte of each word first)
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


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    prefix, srcs = sys.argv[1], sys.argv[2:]
    words = build_image(srcs)
    Path(prefix + ".bin").write_bytes(image_bytes(words))
    Path(prefix + ".hex").write_text("".join(f"{w:04x}\n" for w in words))
    used = sum(1 for w in words if w != HALT_WORD)
    print(f"image: {used} of {RAM_WORDS} words used -> {prefix}.bin, {prefix}.hex")


if __name__ == "__main__":
    main()
