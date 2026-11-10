# Hardware build results

From the LibreLane build in CI (`gds` workflow) of the full four-thread design: CPU, 256-word ROM with the
built-in firmware, 256-word loadable program memory and loader. IHP 130 nm CMOS5L, 8x4 tiles (the size
confirmed for the March 2027 shuttle), 20 ns clock (50 MHz).

| | |
|---|---|
| Standard cells (excluding fill and antenna diodes) | 31,911 |
| of which flip-flops | 4,625 (about 4,100 are the loadable program memory; the four threads' state is about 250) |
| Standard-cell area (excluding fill) | 539,613 µm² |
| Core area (8x4 tiles) | about 1,208,000 µm², so about 45 % used |
| Setup timing at 20 ns | no violations in any corner (nom_slow 1.08 V 125 C, nom_typ, nom_fast) |
| Hold timing | no violations in any corner |
| Antenna, LVS, DRC, precheck | pass |
| Gate-level simulation (the full 17-test cocotb suite on the synthesized netlist) | 17/17 pass |

For comparison, the earlier single-thread design with a 128-word program memory was 16,051 cells and about
272,000 µm² (6x4 tiles). Doubling the loadable memory to 256 words accounts for most of the growth; the
per-thread state of the four-thread core is small next to the program memory.

## What limited the clock

With the 128-word program memory added, the first layout failed setup timing at 20 ns: it only met 25 ns.
The post-route report showed most of the critical path was not logic but a chain of weak buffers fanning
the program counter out to the memory's read multiplexers (thousands of select pins). The fix was one copy
of `PC[6:0]` per instruction bit (`src/pc_rep.v`, marked `keep_hierarchy` so synthesis does not merge the
copies), so each copy drives a single bit slice. The change does not alter any instruction's timing.

Other options that were tried: a 64-word memory (met 20 ns), a slower 25 to 30 ns clock (met timing), and
fetching both candidate next instructions in the same cycle (kept the 128 words but the layout tools did
not finish within the CI time limit).

## Earlier build, for reference

Before the move to 8x4 and the 256-word memory, the same four-thread core built at 6x4 with a 128-word loadable
memory: about 18,100 cells, 2,561 flip-flops and 301,600 µm² (about 33 % of that smaller area), also with no
setup or hold violations at 20 ns.
