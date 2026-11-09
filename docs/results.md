# Hardware build results

**These numbers are for the earlier single-thread version of the core (same memory system). The four-thread
version is being re-run through the same flow; this page is updated when it has finished.**

From the LibreLane build in CI (`gds` workflow) of the full design: CPU, 256-word ROM with the built-in
firmware, 128-word loadable program memory and loader. IHP 130 nm CMOS5L, 6x4 tiles, 20 ns clock (50 MHz).

| | |
|---|---|
| Standard cells (excluding fill) | 16,051 |
| of which flip-flops | 2,313 (mostly the program memory) |
| Standard-cell area (excluding fill) | about 272,000 µm² |
| Core area available (6x4 tiles) | 902,417 µm² (about 30% used) |
| Setup timing, worst corner (1.08 V, 125 C) | +2.34 ns slack, no violations in any corner |
| Hold timing | +0.13 ns worst slack, no violations |
| Antenna, LVS, DRC, precheck | pass |
| Gate-level simulation (the full cocotb suite on the synthesized netlist) | pass |

## What limited the clock

With the 128-word program memory added, the first layout failed setup timing at 20 ns: it only met 25 ns.
The post-route report showed most of the critical path was not logic but a chain of weak buffers fanning
the program counter out to the memory's read multiplexers (thousands of select pins). The fix was one copy
of `PC[6:0]` per instruction bit (`src/pc_rep.v`, marked `keep_hierarchy` so synthesis does not merge the
copies), so each copy drives a single bit slice. The change does not alter any instruction's timing.

Other options that were tried: a 64-word memory (met 20 ns), a slower 25 to 30 ns clock (met timing), and
fetching both candidate next instructions in the same cycle (kept the 128 words but the layout tools did
not finish within the CI time limit).
