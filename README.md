# supersensor

A mini-[nvtop](https://github.com/Syllo/nvtop)-style **live terminal monitor** that puts your
GPUs, CPU, and liquid-cooling loop on one screen — including sensors the usual tools can't reach
(GPU **memory** temps on Blackwell, CPU **package power**, coolant **temp + flow**).

It's a single dependency-free Python script (stdlib only) and **degrades gracefully**: every data
source is optional, and anything unsupported on your machine simply shows `n/a` instead of failing.

**NVIDIA and AMD GPUs are both detected automatically**, in any combination, with no configuration
and no ROCm/rocm-smi install.

![supersensor in action](supersensor.gif)

*Four GPUs under a live vLLM workload while a CPU load ramps across all cores. Values are
color-coded by load/temperature (green → yellow → red); each GPU shows core/memory/hot-spot temps
and power, the CPU shows aggregate + per-core usage, and the loop shows coolant temp + flow.*

## What it shows

- **Per GPU** — name, utilization, SM clock, VRAM used/total, **core temp**, **memory temp**,
  **hot spot**, fan, power draw/limit. Works for NVIDIA and AMD cards at the same time.
- **CPU** — aggregate usage, average frequency, package temp, **package power** (+ core power),
  and a **per-core usage grid** (index · bar · %).
- **Coolant** — water temperature and flow rate (L/h), if a supported loop sensor is present.
- **Total power** — sum of GPU board power + CPU package power.

## Requirements

- **Python 3.6+** — standard library only, nothing to `pip install`.
- Everything below is **optional**; supersensor uses whatever is available:

| Source | Provides | Needs | If unavailable |
|---|---|---|---|
| `nvidia-smi` | NVIDIA GPU util, VRAM, core temp, power, fan, clock | NVIDIA driver | falls back to sysfs (below) |
| sysfs `/sys/class/drm`, hwmon | **AMD** GPU util, VRAM, temps, power, fan, clock | `amdgpu` driver | AMD cards absent |
| [`nvidia-gpu-sensors`](https://github.com/philipl/nvidia-gpu-sensors) | NVIDIA GPU **memory** & **hot-spot** temp | binary + root | those columns show `n/a` (nvidia-smi's mem temp still used if it has one) |
| `turbostat` | CPU **package**/core power, avg freq | `turbostat` + root + MSR | falls back to RAPL |
| RAPL (`/sys/class/powercap`) | CPU package power | powercap + root | power shows `n/a` |
| `/proc/stat` | CPU usage (aggregate + per-core) | Linux | usage shows `n/a` |
| hwmon (`/sys/class/hwmon`) | CPU package temp | `lm-sensors`/kernel | temp shows `n/a` |
| Aquacomputer `highflownext` | coolant temp + flow | `aquacomputer` kernel module | Coolant section is hidden |

## Install

```sh
make install                 # -> /usr/local/bin/supersensor
make install-gpu-sensors     # optional: nvidia-gpu-sensors -> /usr/local/bin (see below)
```

Override the location with `PREFIX=` or `BINDIR=` (e.g. `make install PREFIX=$HOME/.local`).
`make uninstall` removes it. Installing copies the script, so re-run `make install` after edits.

## Usage

```sh
sudo supersensor              # full data (package power + GPU mem temps need root)
supersensor                   # works too — those root-only sources show n/a
supersensor --interval 2      # refresh every 2s (default 1s)
supersensor --once            # print a single frame and exit (good for logs/pipes)
supersensor --no-color        # plain text
supersensor --vendor amd      # only AMD cards (default: auto-detect both vendors)
supersensor --gpu-sensors /path/to/nvidia-gpu-sensors
```

You can also run it without installing: `make monitor` (= `sudo python3 supersensor.py`), or
directly with `python3 supersensor.py`.

### Why sudo?

CPU package power (turbostat reads MSRs; RAPL's `energy_uj` is root-only since CVE-2020-8694) and
GPU memory/hot-spot temps (`nvidia-gpu-sensors` reads the card's BAR0) both require root. Run
`sudo supersensor` for the full picture; everything else — GPU stats (NVIDIA *and* AMD), CPU usage,
coolant — works unprivileged. AMD sensors come from sysfs and need no privileges at all.

## AMD GPUs

AMD cards are read straight from **sysfs** — no `rocm-smi`/`amd-smi`, no ROCm install. Cards are
found by walking `/sys/bus/pci` for display-class devices with the `amdgpu` driver, which works on
integrated APUs and headless compute hosts as well as discrete cards (RX 9000 series, **RX 9700 AI
PRO / Radeon AI PRO R9700**, Radeon Pro, Instinct, …). A host with both vendors shows both, side by
side, with each card tagged by vendor.

Temperatures come from the driver's hwmon channels, matched by **label** rather than index so a
kernel that orders them differently still lines up: `edge` → core temp, `junction` → hot spot,
`mem` → memory temp. Power is `power1_input` against `power1_cap`, VRAM is `mem_info_vram_*`,
utilization is `gpu_busy_percent` (falling back to DRM `fdinfo` engine counters on kernels that do
not expose it), and the clock is the current `pp_dpm_sclk` state. **Fan is shown in RPM** for AMD
where NVIDIA reports duty %.

Sensors are tied to a card by resolving the hwmon/DRM node's real sysfs path against that card's
PCI device, so a multi-AMD-card host attributes every reading correctly rather than handing every
card the first one's numbers.

AMD needs none of the row-alignment machinery the NVIDIA memory/hot-spot path uses: sysfs is exact,
so AMD never shows `learning`.

`--vendor nvidia` or `--vendor amd` restricts collection to one vendor; the default `auto` detects
both.

## NVIDIA: when `nvidia-smi` cannot reach the driver

A driver/userspace version mismatch, a container without the device nodes, or a wedged module makes
`nvidia-smi` fail outright — indistinguishable from "no GPU here", which is why the GPU section
used to vanish. `nvidia-smi` is still preferred when it works, but if it returns nothing the card is
read from sysfs instead: name (from `/proc/driver/nvidia`, falling back to `pci.ids`), temperature,
power and VRAM. SM utilization and fan duty stay `n/a` in that state, since NVIDIA exposes them only
through the driver's own libraries.

## NVIDIA GPU memory & hot-spot temps (nvidia-gpu-sensors)

`nvidia-smi` reports `temperature.memory` as `N/A` on many cards (e.g. RTX PRO 6000 Blackwell).
[`nvidia-gpu-sensors`](https://github.com/philipl/nvidia-gpu-sensors) reads it straight off the die:

```sh
sudo apt install -y git build-essential meson ninja-build
git clone https://github.com/philipl/nvidia-gpu-sensors && cd nvidia-gpu-sensors
meson setup build && ninja -C build
```

supersensor auto-detects the binary via `$PATH`, `$SUPERSENSOR_GPU_SENSORS`,
`~/nvidia-gpu-sensors/build/`, and `/usr/local/bin`. Run `make install-gpu-sensors` (from the repo
root, with the build present) to copy it onto `$PATH` for good.

If **hot spot** shows `n/a` with a note about `mmap of BAR0`, your kernel has
`CONFIG_IO_STRICT_DEVMEM=y`; boot with `iomem=relaxed` on the kernel command line to enable it.
(Core and memory temps do not need this.)

**Build an up-to-date `nvidia-gpu-sensors`.** Builds before the
`Core Temp | Hot Spot | Mem Temp` column order print a header that disagrees with the data they
emit, transposing memory and hot spot. supersensor detects that (hot spot can never read below
core temp) and corrects it, but a current build avoids the guesswork.

**Memory and hot spot are matched to GPUs via the driver.** `nvidia-gpu-sensors` enumerates
GPUs with RM's `GPU_GET_PROBED_IDS` and prints no PCI bus id, so its row numbers are not
nvidia-smi's indices -- on a 4x RTX PRO 6000 host here its row 2 is nvidia-smi's GPU 1. Joining
them by index shows one card's memory temperature against another.

RM returns GPUs in `deviceInstance` order, which is the `/dev/nvidiaN` minor, so the Nth row is
the GPU with minor N. `/proc/driver/nvidia/gpus/<bus>/information` gives bus -> minor and
nvidia-smi gives index -> bus, which pins every row exactly, immediately, with the cards in use
and at whatever temperature they happen to be. A core temperature that flatly disagrees is taken
as the assumption having broken, and the fallback below takes over.

Without `/proc/driver/nvidia` -- a container that does not mount it -- supersensor falls back to
matching rows by core temperature, which only distinguishes a card while it sits at a temperature
no other card shares. Each sample then pins whichever cards it can, the result accumulates in
`~/.cache/supersensor/rowmap.json` keyed by the set of PCI bus ids present, and the last row
falls out by elimination. Unidentified cards show `learning` and a progress line until their turn
comes; identified ones show real readings straight away. On startup supersensor will also load
each unidentified GPU briefly via `stress.py` to force the issue, if a CUDA-capable torch is
present -- often it is not, even on a machine running GPUs, and it says so rather than waiting.
`--no-autolearn` skips that; `$SUPERSENSOR_STATE` relocates the cache.

## CPU package power

Best data comes from `turbostat` (part of `linux-cpupower` / `linux-tools`); supersensor streams it
in the background and shows `PkgWatt`, `CorWatt`, and `Bzy_MHz`. If turbostat isn't installed it
falls back to RAPL powercap counters, and if neither is readable, power shows `n/a`. CPU **usage**
(aggregate and per-core) comes from `/proc/stat` and needs no privileges.

## Coolant loop

Water temperature and flow are read from the kernel `aquacomputer` hwmon driver
(Aquacomputer **High Flow Next**: coolant temp + flow in dL/h, converted to L/h). If no such sensor
is present the Coolant section is simply omitted.

---

## GPU stress test (secondary)

The repo also ships a small CUDA burn-in used to exercise the GPUs while you watch temps in
supersensor. It's a Dockerized PyTorch fp16 matmul loop with built-in temp sampling (`stress.py`),
and it samples **AMD cards too** (via sysfs) when run against a ROCm torch.

```sh
make build                          # build the image (nvcr.io/nvidia/pytorch base)
make run                            # stress ALL GPUs for 5 min
make run TIMEOUT=600 GPUS='"device=0,1"'
make run ARGS="--size 8192 --interval 2"
```

Run it in one terminal and `sudo supersensor` in another to watch the loop heat up.

## Project layout

| File | Role |
|---|---|
| `supersensor.py` | **the monitor** (primary) — single-file, stdlib only |
| `stress.py` | GPU stress test (secondary) |
| `test_amd.py` | fixture tests for the AMD / NVIDIA-sysfs readers (no hardware needed) |
| `Makefile` | `install` / `monitor` (supersensor) and `build` / `run` (stress test) |
| `Dockerfile` | image for the stress test |

`python3 test_amd.py` builds a throwaway sysfs tree — including a Radeon AI PRO R9700 — and checks
that each sensor lands in the right field, so the AMD path is exercised without an AMD card.
