# supersensor

A mini-[nvtop](https://github.com/Syllo/nvtop)-style **live terminal monitor** that puts your
GPUs, CPU, and liquid-cooling loop on one screen — including sensors the usual tools can't reach
(GPU **memory** temps on Blackwell, CPU **package power**, coolant **temp + flow**).

It's a single dependency-free Python script (stdlib only) and **degrades gracefully**: every data
source is optional, and anything unsupported on your machine simply shows `n/a` instead of failing.

```
supersensor  2026-07-23 00:18:36    refresh 1s

  GPU 0  NVIDIA RTX PRO 6000 Blackwell Workstation Edition
    util ░░░░░░░░░░░░░░░░░░░░░░   0%   180MHz
    vram ██████████████████████  98%   95860 / 97887 MiB
    temp  28C   mem-temp  30C   hotspot  31C   fan   0%   power 14/600W
  ... (one block per GPU) ...

  CPU  48 cores
    usage ░░░░░░░░░░░░░░░░░░░░░░   1%   3336MHz   temp  34C   power 62W (cores 1W)
     0 ░░░░░   2%   1 █░░░░  18%   2 ░░░░░   1%   3 ░░░░░   2%   4 ░░░░░   1%  ...
     7 ░░░░░   0%   8 ░░░░░   0%   9 ░░░░░   0%  10 ░░░░░   1%  ...        (one cell per core)

  Coolant  water 28.2C   flow 164.2 L/h

  Power  GPU 48W   CPU 62W   total 110W

  Ctrl-C to quit
```

Values are color-coded by load/temperature (green → yellow → red) in a real terminal.

## What it shows

- **Per GPU** — name, utilization, SM clock, VRAM used/total, **core temp**, **memory temp**,
  **hot spot**, fan %, power draw/limit.
- **CPU** — aggregate usage, average frequency, package temp, **package power** (+ core power),
  and a **per-core usage grid** (index · bar · %).
- **Coolant** — water temperature and flow rate (L/h), if a supported loop sensor is present.
- **Total power** — sum of GPU board power + CPU package power.

## Requirements

- **Python 3.6+** — standard library only, nothing to `pip install`.
- Everything below is **optional**; supersensor uses whatever is available:

| Source | Provides | Needs | If unavailable |
|---|---|---|---|
| `nvidia-smi` | GPU util, VRAM, core temp, power, fan, clock | NVIDIA driver | GPU section shows "no GPUs" |
| [`nvidia-gpu-sensors`](https://github.com/philipl/nvidia-gpu-sensors) | GPU **memory** & **hot-spot** temp | binary + root | those columns show `n/a` (nvidia-smi's mem temp still used if it has one) |
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
supersensor --gpu-sensors /path/to/nvidia-gpu-sensors
```

You can also run it without installing: `make monitor` (= `sudo python3 supersensor.py`), or
directly with `python3 supersensor.py`.

### Why sudo?

CPU package power (turbostat reads MSRs; RAPL's `energy_uj` is root-only since CVE-2020-8694) and
GPU memory/hot-spot temps (`nvidia-gpu-sensors` reads the card's BAR0) both require root. Run
`sudo supersensor` for the full picture; everything else — GPU stats, CPU usage, coolant — works
unprivileged.

## GPU memory & hot-spot temps

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
supersensor. It's a Dockerized PyTorch fp16 matmul loop with built-in temp sampling (`stress.py`).

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
| `Makefile` | `install` / `monitor` (supersensor) and `build` / `run` (stress test) |
| `Dockerfile` | image for the stress test |
