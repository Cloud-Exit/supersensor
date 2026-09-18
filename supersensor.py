#!/usr/bin/env python3
import argparse, glob, itertools, json, os, re, shutil, subprocess, sys, threading, time

PROG = "supersensor"

# Vendors we know how to read. Everything is optional and detected at runtime, so a
# machine with one vendor, the other, or both works with no configuration.
VENDOR_NVIDIA = 0x10DE
VENDOR_AMD = 0x1002

# ---- ANSI helpers ---------------------------------------------------------
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
FG = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m",
      "cyan": "\033[36m", "blue": "\033[34m", "grey": "\033[90m"}
COLOR = True  # toggled off for non-tty / --no-color


def c(text, color):
    return f"{FG[color]}{text}{RESET}" if COLOR and color in FG else text


def bold(text):
    return f"{BOLD}{text}{RESET}" if COLOR else text


def parse_args():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="supersensor: mini-nvtop live monitor — GPU (NVIDIA nvidia-smi, "
                    "auto-detected AMD), CPU usage/power (turbostat/RAPL), and "
                    "Aquacomputer High Flow Next coolant temp + flow")
    p.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1)")
    p.add_argument("--once", action="store_true", help="print a single frame and exit")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--gpu-sensors", metavar="PATH", default=None,
                   help="path to nvidia-gpu-sensors binary (for GPU mem/hot-spot temps; "
                        "auto-detected, or set SUPERSENSOR_GPU_SENSORS)")
    p.add_argument("--no-autolearn", action="store_true",
                   help="skip the one-time per-GPU load used to identify which sensor row "
                        "belongs to which GPU (those columns stay blank until it runs)")
    p.add_argument("--vendor", choices=("auto", "nvidia", "amd"), default="auto",
                   help="limit GPU collection to one vendor (default: auto-detect both)")
    return p.parse_args()


# ---- PCI discovery --------------------------------------------------------
# Both vendors are found by walking /sys/bus/pci, which exists wherever the GPUs do
# -- including a container with /sys mounted and no vendor userspace at all. Display
# class (0x03) covers VGA and 3D controllers; audio/USB functions of a card are 0x04
# and 0x0c and are filtered out by it.
_PCI_IDS_FILES = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids",
                  "/usr/share/pciids/pci.ids", "/var/lib/pciutils/pci.ids")
_pci_names = None


def _read_text(path):
    """Stripped file contents, or None if unreadable/empty (a sensor that is absent)."""
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _read_int(path):
    v = _read_text(path)
    if v is None:
        return None
    try:
        return int(v, 0)
    except ValueError:
        return None


def _load_pci_names():
    """Parse pci.ids into {(vendor, device): name}, plus a subtree fallback.

    pci.ids is the same table lspci uses. Kernel sysfs gives only numeric ids, and a
    card's marketing name is the one thing that makes the display readable, so read it
    from the file rather than printing "1002:7551". Absent/odd file -> {} and ids are
    shown instead."""
    global _pci_names
    if _pci_names is not None:
        return _pci_names
    names = {}
    for path in _PCI_IDS_FILES:
        try:
            with open(path, "r", errors="replace") as fh:
                vend = dev = None
                for line in fh:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line[0].isspace():                  # vendor line: "1002  AMD/ATI"
                        parts = line.split(None, 1)
                        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{4}", parts[0]):
                            vend, dev = int(parts[0], 16), None
                            names.setdefault(("v", vend), parts[1].strip())
                        else:
                            vend = dev = None
                    elif vend is not None and line.startswith("\t") and not line.startswith("\t\t"):
                        parts = line.strip().split(None, 1)    # device line: "7551  Navi 48 [...]"
                        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{4}", parts[0]):
                            dev = int(parts[0], 16)
                            names[(vend, dev)] = parts[1].strip()
        except OSError:
            continue
        if names:
            break
    _pci_names = names
    return names


def _pci_name(vendor, device, subvendor=None, subdevice=None):
    """Marketing name for a PCI id from pci.ids; '' if the table has no entry.

    A subsystem entry ("1da2 e490  Sapphire Pulse ...") is preferred over the chip
    name because it is what the card actually is, but most cards only have the chip
    entry, which still beats a bare hex id."""
    names = _load_pci_names()
    if subvendor is not None and subdevice is not None:
        # Subsystem table nests as vendor -> device -> "subvend subdev  name".
        for path in _PCI_IDS_FILES:
            try:
                with open(path, "r", errors="replace") as fh:
                    want_v = "\t%04x  " % vendor
                    want_d = "\t%04x  " % device
                    in_v = in_d = False
                    for line in fh:
                        if not line.strip():
                            continue
                        if not line[0].isspace():          # vendor line
                            in_v = line.startswith(want_v)
                            in_d = False
                        elif in_v and line.startswith("\t") and not line.startswith("\t\t"):
                            in_d = line.startswith(want_d)  # device line
                        elif in_v and in_d and line.startswith("\t\t"):
                            parts = line.strip().split(None, 2)
                            if len(parts) == 3:
                                try:
                                    if (int(parts[0], 16), int(parts[1], 16)) == (subvendor, subdevice):
                                        return parts[2].strip()
                                except ValueError:
                                    pass
            except OSError:
                continue
            break
    return names.get((vendor, device), "")


def _pci_display_devices():
    """[(devpath, bus, vendor, device, subvendor, subdevice, driver)] for display-class
    PCI devices.

    NVIDIA and AMD cards are both reached this way, so autodetection needs no notion of
    "which vendor is present" up front: whichever ones enumerate are read, and a host
    with both shows both. devpath is returned rather than reconstructed from bus, so the
    readers find each card's hwmon/DRM nodes by path identity."""
    out = []
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        cls = _read_int(os.path.join(dev, "class"))
        if cls is None or (cls >> 16) != 0x03:        # 0x03xxxx = display controller
            continue
        sub = _read_int(os.path.join(dev, "subsystem_vendor"))
        subdev = _read_int(os.path.join(dev, "subsystem_device"))
        try:
            # driver is a symlink to .../drivers/amdgpu; take the leaf of its real target.
            driver = os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
        except OSError:
            driver = ""
        if driver == "driver":                         # unreadable/odd link: fall back to name
            driver = _read_text(os.path.join(dev, "driver", "module", "name")) or ""
        out.append((dev, os.path.basename(dev), _read_int(os.path.join(dev, "vendor")),
                    _read_int(os.path.join(dev, "device")), sub, subdev, driver))
    return out


def _hwmon_for(devpath):
    """hwmon directory belonging to the PCI device at `devpath`, or None.

    A card's sensors hang off its own PCI device, so matching on the resolved sysfs
    path ties a hwmon node to exactly one card. Name-based matching (any hwmon called
    "amdgpu") would be wrong on a multi-AMD-card host: it would hand every card the
    first card's temperatures."""
    real = os.path.realpath(devpath)
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if os.path.realpath(os.path.join(hw, "device")) == real:
                return hw
        except OSError:
            continue
    return None


def _drm_for(devpath):
    """DRM card node belonging to the PCI device at `devpath`, or None."""
    real = os.path.realpath(devpath)
    for card in glob.glob("/sys/class/drm/card[0-9]*"):
        if "-" in os.path.basename(card):             # connector node, not the card
            continue
        try:
            if os.path.realpath(os.path.join(card, "device")) == real:
                return card
        except OSError:
            continue
    return None


# ---- GPU (AMD, sysfs) -----------------------------------------------------
def _fdinfo_engine_ns(card):
    """Busy nanoseconds on the card's graphics engine across all DRM clients, or None.

    amdgpu's gpu_busy_percent is absent on some kernels and on APUs, and reads 0 when no
    client in this namespace holds the device open. The per-client DRM fdinfo counters
    are what nvtop uses, and they cover the case gpu_busy_percent does not: work
    submitted by a process (or container) elsewhere on the machine."""
    want = _canon_bus(os.path.basename(os.path.realpath(os.path.join(card, "device"))))
    total, seen = 0, False
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        d = "/proc/%s/fdinfo" % pid
        try:
            fds = os.listdir(d)
        except OSError:
            continue
        for fd in fds:
            try:
                with open(os.path.join(d, fd)) as fh:
                    dev = engine = None
                    for line in fh:
                        if line.startswith("drm-pdev:"):
                            dev = line.split(":", 1)[1].strip()
                        elif line.startswith("drm-engine-gfx:"):
                            engine = line.split(":", 1)[1].strip().split()[0]
                        elif line.startswith("drm-engine-compute:") and engine is None:
                            engine = line.split(":", 1)[1].strip().split()[0]
            except (OSError, ValueError, IndexError):
                continue
            if dev and engine and _canon_bus(dev) == want:
                try:
                    total += int(engine)
                    seen = True
                except ValueError:
                    pass
    return total if seen else None


class _FdinfoBusy:
    """Turns the cumulative fdinfo engine counter into a busy percentage over time.

    One instance per card. The first sample has no interval to divide by, so it reports
    nothing rather than a bogus number; from the second frame on the delta over the real
    elapsed time gives a genuine utilisation figure."""
    def __init__(self):
        self.prev = None

    def update(self, card):
        now = _fdinfo_engine_ns(card)
        t = time.monotonic()
        if now is None:
            return None
        prev_pair, self.prev = self.prev, (now, t)
        if prev_pair is None:
            return None                                # first sample: no interval yet
        prev, pt = prev_pair
        if t <= pt or now < prev:                      # counter reset, or clock skew
            return None
        return max(0.0, min(100.0, 100.0 * (now - prev) / ((t - pt) * 1e9)))


def _hwmon_temp(hw, wanted):
    """Temperature in C for the temp channel whose label matches `wanted`.

    amdgpu labels its channels "edge", "junction", "mem"; matching the label rather
    than the index keeps working if a kernel orders or omits channels differently."""
    for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
        if (_read_text(tf.replace("_input", "_label")) or "").lower() == wanted:
            v = _read_int(tf)
            if v is not None and v > 0:
                return v / 1000.0
    return None


def read_amd_sysfs(busy=None):
    """Per-GPU dicts for AMD cards via sysfs. [] if there are none.

    Deliberately no rocm-smi/amd-smi: sysfs is present on every kernel with the amdgpu
    driver, needs no ROCm install, and covers integrated APUs that rocm-smi often
    misreports. Every field degrades to None on its own, so a card exposing only some
    sensors still shows what it has.

    `busy` is an optional {bus: _FdinfoBusy} cache so the fdinfo counters -- the only
    utilisation source on kernels without gpu_busy_percent -- are differenced over time
    rather than reported raw."""
    busy = busy if busy is not None else {}
    gpus = []
    for devpath, bus, vendor, device, subv, subd, driver in _pci_display_devices():
        if vendor != VENDOR_AMD or driver not in ("amdgpu", "radeon"):
            continue
        hw = _hwmon_for(devpath)
        name = _pci_name(vendor, device, subv, subd) or "AMD GPU"
        g = {
            "index": len(gpus), "vendor": "amd", "name": name, "bus": bus,
            "util": None, "mem_used": None, "mem_total": None,
            "temp": _hwmon_temp(hw, "edge") if hw else None,
            "mem_temp": _hwmon_temp(hw, "mem") if hw else None,
            "hotspot": _hwmon_temp(hw, "junction") if hw else None,
            "power": None, "power_max": None, "fan": None, "clock": None,
            "fan_unit": "rpm",
        }
        if hw:
            pw = _read_int(os.path.join(hw, "power1_input"))
            cap = _read_int(os.path.join(hw, "power1_cap"))
            if pw is not None and pw >= 0:
                g["power"] = pw / 1e6
            if cap:
                g["power_max"] = cap / 1e6
            g["fan"] = _read_int(os.path.join(hw, "fan1_input"))
            sclk = _read_int(os.path.join(hw, "freq1_input"))
            if sclk:
                g["clock"] = sclk / 1e6
        card = _drm_for(devpath)
        if card:
            busy_pct = _read_int(os.path.join(card, "device", "gpu_busy_percent"))
            if busy_pct is None:
                # Absent on some kernels; difference the fdinfo counters instead.
                busy_pct = busy.setdefault(bus, _FdinfoBusy()).update(card)
            g["util"] = busy_pct
            used = _read_int(os.path.join(card, "device", "mem_info_vram_used"))
            tot = _read_int(os.path.join(card, "device", "mem_info_vram_total"))
            if used is not None and tot:
                g["mem_used"], g["mem_total"] = used / 1048576.0, tot / 1048576.0
            if g["clock"] is None:
                m = re.search(r"^\s*\d+:\s*(\d+)\s*Mhz\s*\*", _read_text(
                    os.path.join(card, "device", "pp_dpm_sclk")) or "", re.M)
                if m:
                    g["clock"] = float(m.group(1))
        gpus.append(g)
    return gpus


# ---- GPU (nvidia-smi) -----------------------------------------------------
# Full field set, then a minimal one: older drivers reject unknown query fields
# with a non-zero exit (losing everything), so we retry with just the essentials.
GPU_FIELDS = ["index", "name", "pci.bus_id", "utilization.gpu", "memory.used", "memory.total",
              "temperature.gpu", "temperature.memory", "power.draw", "power.limit",
              "fan.speed", "clocks.sm"]
GPU_FIELDS_MIN = ["index", "name", "pci.bus_id", "utilization.gpu", "memory.used",
                  "memory.total", "temperature.gpu", "power.draw", "power.limit"]


def read_gpu_info():
    """Return list of per-GPU dicts via nvidia-smi. [] if no GPUs / nvidia-smi absent.
    Missing/unsupported fields degrade to None rather than dropping the GPU."""
    def num(x):
        try: return float(x)
        except (ValueError, TypeError): return None  # "N/A", "[Not Supported]", missing

    for fields in (GPU_FIELDS, GPU_FIELDS_MIN):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=" + ",".join(fields),
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            continue  # binary missing, or a field this driver rejects -> try minimal
        gpus = []
        for line in out.strip().splitlines():
            vals = [p.strip() for p in line.split(",")]
            if len(vals) != len(fields):
                continue
            d = dict(zip(fields, vals))
            idx = d.get("index", "")
            gpus.append({
                "index": int(idx) if idx.isdigit() else len(gpus),
                "vendor": "nvidia",
                "name": d.get("name") or "GPU",
                "bus": d.get("pci.bus_id") or "",
                "util": num(d.get("utilization.gpu")),
                "mem_used": num(d.get("memory.used")), "mem_total": num(d.get("memory.total")),
                "temp": num(d.get("temperature.gpu")), "mem_temp": num(d.get("temperature.memory")),
                "power": num(d.get("power.draw")), "power_max": num(d.get("power.limit")),
                "fan": num(d.get("fan.speed")), "clock": num(d.get("clocks.sm")),
                "fan_unit": "%",
            })
        if gpus:
            return gpus
    return []


def read_nvidia_sysfs():
    """Per-GPU dicts for NVIDIA cards from sysfs alone, [] if there are none.

    A fallback for when nvidia-smi cannot talk to the driver -- version skew between
    the userspace tools and the loaded module, a container without the device nodes, or
    the driver wedged -- which is otherwise indistinguishable from "no GPU here". The
    kernel still reports name, temperature, power, clock and VRAM in that state, so a
    card stays on screen instead of vanishing behind a "no GPUs" line.

    What sysfs cannot give without the driver's own libraries is SM utilization and fan
    duty, so those stay n/a (NVIDIA's hwmon node is built by the driver and disappears
    with it). When nvidia-smi works it is preferred, since it has both."""
    gpus = []
    for devpath, bus, vendor, device, subv, subd, driver in _pci_display_devices():
        if vendor != VENDOR_NVIDIA:
            continue
        hw = _hwmon_for(devpath)
        g = {
            "index": len(gpus), "vendor": "nvidia",
            "name": _pci_name(vendor, device, subv, subd) or "NVIDIA GPU", "bus": bus,
            "util": None, "mem_used": None, "mem_total": None,
            "temp": None, "mem_temp": None, "hotspot": None,
            "power": None, "power_max": None, "fan": None, "clock": None,
            "fan_unit": "%",
        }
        if hw:
            for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
                if (_read_text(tf.replace("_input", "_label")) or "").lower() == "junction":
                    v = _read_int(tf)                      # nvidia exposes junction only
                    if v:
                        g["hotspot"] = v / 1000.0
            v = _read_int(os.path.join(hw, "temp1_input"))
            if v:
                g["temp"] = v / 1000.0
            v = _read_int(os.path.join(hw, "power1_input"))
            if v:
                g["power"] = v / 1e6
        # /proc/driver/nvidia survives a wedged or version-skewed nvidia-smi and carries
        # the card's real name; prefer it over the generic pci.ids chip name.
        for line in (_read_text("/proc/driver/nvidia/gpus/%s/information" % bus) or "").splitlines():
            if line.lower().startswith("model:"):
                g["name"] = line.split(":", 1)[1].strip() or g["name"]
                break
        card = _drm_for(devpath)
        if card:
            used = _read_int(os.path.join(card, "device", "mem_info_vram_used"))
            tot = _read_int(os.path.join(card, "device", "mem_info_vram_total"))
            # /sys/.../nvidia-smi/... is provided by the driver's own sysfs group when the
            # module is loaded; VRAM figures live on the DRM node for the open module.
            if used is not None and tot:
                g["mem_used"], g["mem_total"] = used / 1048576.0, tot / 1048576.0
        gpus.append(g)
    return gpus


def read_gpus(prefer_smi=True, vendor="auto", busy=None):
    """All GPUs on the machine, NVIDIA and AMD, in one list with unique indices.

    Autodetection is by enumeration, not by configuration: whichever vendors have
    display-class PCI devices are read, so an NVIDIA-only, AMD-only or mixed host all
    work, and a mixed host shows both side by side. NVIDIA keeps its nvidia-smi indices
    so an existing learned rowmap stays valid; AMD is numbered after it.

    Pass the same `busy` dict on every call so AMD fdinfo utilisation is differenced
    across frames rather than recomputed from scratch."""
    gpus = []
    if prefer_smi and vendor in ("auto", "nvidia"):
        gpus = read_gpu_info()                        # nvidia-smi, with its own indices
        if gpus:
            for g in gpus:
                g.setdefault("vendor", "nvidia")
                g.setdefault("fan_unit", "%")
    if not gpus and vendor in ("auto", "nvidia"):
        gpus = read_nvidia_sysfs()                    # smi absent or unable to reach the driver
    # AMD is numbered after whatever NVIDIA contributed, and every index is reassigned
    # from the final order so the two readers cannot both claim 0 on a mixed host.
    for i, g in enumerate(gpus):
        g["index"] = i
    if vendor in ("auto", "amd"):
        gpus += read_amd_sysfs(busy)
    for i, g in enumerate(gpus):                      # read_amd_sysfs numbered from 0
        g["index"] = i
    return gpus


# ---- GPU memory / hot-spot temps (nvidia-gpu-sensors, needs root) --------
# nvidia-smi reports temperature.memory as N/A on many cards (e.g. Blackwell
# RTX PRO 6000); philipl/nvidia-gpu-sensors reads it straight off the die.
def find_gpu_sensors(explicit=None):
    """Locate the nvidia-gpu-sensors binary; None if not found."""
    cands = [explicit, os.environ.get("SUPERSENSOR_GPU_SENSORS"),
             shutil.which("nvidia-gpu-sensors")]
    homes = []
    if os.environ.get("SUDO_USER"):
        homes.append("/home/" + os.environ["SUDO_USER"])   # invoking user under sudo
    homes.append(os.path.expanduser("~"))
    for h in homes:
        cands.append(os.path.join(h, "nvidia-gpu-sensors", "build", "nvidia-gpu-sensors"))
    cands += ["/usr/local/bin/nvidia-gpu-sensors", "/usr/bin/nvidia-gpu-sensors",
              os.path.join(os.getcwd(), "nvidia-gpu-sensors", "build", "nvidia-gpu-sensors")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _parse_gpu_sensors(text):
    """Parse the nvidia-gpu-sensors table -> {gpu_index: {core, mem, hotspot}} in C."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = next((ln for ln in lines if "GPU" in ln and "Temp" in ln), None)
    if header is None:
        return {}
    cols = sorted((header.find(n), n) for n in
                  ("Core Temp", "Mem Temp", "Hot Spot", "NVVDD", "MSVDD") if n in header)
    names = [n for _, n in cols]
    res = {}
    for ln in lines[lines.index(header) + 1:]:
        toks = ln.split()
        if not toks or not toks[0].isdigit():
            continue
        vals, i = [], 1
        while i < len(toks) and len(vals) < len(names):   # values are "n/a" or "<num> <unit>"
            t = toks[i]
            if t == "n/a":
                vals.append(None); i += 1
            else:
                try: v = float(t)
                except ValueError: i += 1; continue
                i += 2 if (i + 1 < len(toks) and toks[i + 1] in ("C", "V", "W", "°C")) else 1
                vals.append(v)
        row = dict(zip(names, vals))
        res[int(toks[0])] = {"core": row.get("Core Temp"), "mem": row.get("Mem Temp"),
                             "hotspot": row.get("Hot Spot")}
    return _untranspose(res)


def _untranspose(rows):
    """Undo the Mem/Hot Spot column swap of older nvidia-gpu-sensors builds.

    Those print the header as Core|Mem|Hot Spot while emitting Core|Hot Spot|Mem, so
    parsing by header position lands each value in the other's column. Hot spot is the
    max over the on-die sensor array and so can never read below core temp; one row
    breaking that proves the build swaps them, and the fix applies to the whole table.
    Only swap when doing so makes every row consistent, so one odd reading cannot flip
    a correct table."""
    vals = [r for r in rows.values() if None not in (r["core"], r["mem"], r["hotspot"])]
    if not vals or not any(r["hotspot"] < r["core"] for r in vals):
        return rows
    if not all(r["mem"] >= r["core"] for r in vals):
        return rows                      # swapping would not fix it, so leave it alone
    for r in rows.values():
        r["mem"], r["hotspot"] = r["hotspot"], r["mem"]
    return rows


MAP_FILE = "rowmap.json"


def _state_dir():
    """Where the learned row map lives. Under sudo, prefer the invoking user's cache
    so the map survives and stays readable outside the root session."""
    env = os.environ.get("SUPERSENSOR_STATE")
    if env:
        return env
    home = ("/home/" + os.environ["SUDO_USER"]) if os.environ.get("SUDO_USER") \
        else os.path.expanduser("~")
    return os.path.join(home, ".cache", "supersensor")


def _map_key(gpus):
    """Identify this exact set of cards, so a map is not reused across a reshuffle."""
    return "|".join(sorted(g.get("bus") or str(g["index"]) for g in gpus))


def _load_map(gpus):
    try:
        with open(os.path.join(_state_dir(), MAP_FILE)) as fh:
            saved = json.load(fh).get(_map_key(gpus))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(saved, dict):
        return None
    try:
        return {int(row): int(idx) for row, idx in saved.items()}
    except (TypeError, ValueError):
        return None


def _save_map(gpus, mapping):
    d = _state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, MAP_FILE)
        try:
            with open(path) as fh:
                all_maps = json.load(fh)
            if not isinstance(all_maps, dict):
                all_maps = {}
        except (OSError, ValueError):
            all_maps = {}
        all_maps[_map_key(gpus)] = {str(r): i for r, i in mapping.items()}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(all_maps, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        if os.environ.get("SUDO_UID"):
            # Hand the directory as well as the file back to the invoking user: created
            # root-owned by a sudo run, it would otherwise be unwritable ever after.
            uid = int(os.environ["SUDO_UID"])
            gid = int(os.environ.get("SUDO_GID", -1))
            for target in (d, path):
                try:
                    os.chown(target, uid, gid)
                except OSError:
                    pass
    except OSError:
        pass                                  # read-only fs: fall back to re-deriving


def _canon_bus(bus):
    """PCI id without the domain padding: nvidia-smi says 00000000:01:00.0, /proc says
    0000:01:00.0."""
    return bus.strip().lower().split(":", 1)[-1]


def _driver_map(gpus):
    """Exact row -> nvidia-smi index map from the driver, or None.

    nvidia-gpu-sensors enumerates with RM's GPU_GET_PROBED_IDS, which hands back GPUs
    in deviceInstance order -- the same number as the /dev/nvidiaN minor -- so its Nth
    row is the GPU with minor N. /proc/driver/nvidia/gpus/<bus>/information gives
    bus -> minor and nvidia-smi gives index -> bus, which closes the loop with no
    guessing and no waiting for the cards to differ in temperature.

    Returns None if /proc is not mounted (a container without it) or any GPU is
    missing, leaving the temperature-matching path to cope."""
    minors = {}
    for info in glob.glob("/proc/driver/nvidia/gpus/*/information"):
        bus = _canon_bus(os.path.basename(os.path.dirname(info)))
        try:
            with open(info) as fh:
                for line in fh:
                    if line.lower().startswith("device minor"):
                        minors[bus] = int(line.split(":", 1)[1])
                        break
        except (OSError, ValueError):
            continue
    if not minors:
        return None
    out = {}
    for g in gpus:
        minor = minors.get(_canon_bus(g.get("bus") or ""))
        if minor is None or minor in out:
            return None
        out[minor] = g["index"]
    return out if len(out) == len(gpus) else None


def _pins(rows, gpus, tol):
    """Rows that can be tied to a GPU from this sample alone.

    A row pins a GPU when each is within tol of the other's core temperature and of
    nothing else -- in practice, a card at a distinct temperature, which usually means
    one under load. Cards sitting together at idle pin nothing, which is correct: they
    are genuinely indistinguishable by temperature."""
    pins = {}
    for row, vals in rows.items():
        core = vals["core"]
        if core is None:
            continue
        near_gpu = [g for g in gpus
                    if g.get("temp") is not None and abs(g["temp"] - core) <= tol]
        if len(near_gpu) != 1:
            continue
        g = near_gpu[0]
        near_row = [r for r, v in rows.items()
                    if v["core"] is not None and abs(v["core"] - g["temp"]) <= tol]
        if len(near_row) == 1:
            pins[row] = g["index"]
    return pins


def _nvidia_only(gpus):
    """The NVIDIA subset of `gpus`.

    nvidia-gpu-sensors enumerates NVIDIA cards only, so on a mixed host the AMD cards
    must be filtered out before comparing row counts: otherwise two AMD GPUs alongside
    two NVIDIA ones look like a four-row table and every row is misaligned. Entries
    without a vendor predate the distinction and are treated as NVIDIA."""
    return [g for g in gpus if g.get("vendor", "nvidia") == "nvidia"]


def _align(rows, gpus, tol=3.0):
    """Map nvidia-gpu-sensors rows onto nvidia-smi GPUs, learning the map over time.

    The two tools enumerate independently -- nvidia-gpu-sensors walks RM's
    GPU_GET_PROBED_IDS and prints no PCI bus id -- so equal indices need not be the
    same card, and a straight index join can show one GPU's memory against another.

    Core temperature is the only quantity both report, and it identifies a card only
    while that card sits at a distinct temperature, so a single sample rarely resolves
    every row. Instead each sample pins whichever cards it can, the result accumulates
    in a small cache keyed by the set of PCI bus ids, and the last row falls out by
    elimination. Normal use -- one GPU busy at a time -- fills the map in; it is then
    reused while the machine is idle and everything looks alike. A pin that disagrees
    with the cache means the cards moved, so the map is relearned from scratch."""
    gpus = _nvidia_only(gpus)
    if not rows or not gpus or len(rows) != len(gpus) or len(gpus) > 8:
        return {}

    exact = _driver_map(gpus)
    if exact and sorted(exact) == sorted(rows):
        # The driver states the order outright; only distrust it if a core temperature
        # actively disagrees, which would mean the minor/probe-order assumption broke.
        order = [exact[r] for r in sorted(rows)]
        bad = any(rows[r]["core"] is not None and g_t is not None
                  and abs(rows[r]["core"] - g_t) > tol
                  for r, g_t in zip(sorted(rows),
                                    [dict((g["index"], g.get("temp")) for g in gpus)[i]
                                     for i in order]))
        if not bad:
            return {idx: rows[row] for row, idx in exact.items()}

    known = _load_map(gpus) or {}
    pins = _pins(rows, gpus, tol)
    if any(known.get(row, idx) != idx for row, idx in pins.items()):
        known = {}                                   # cards moved -- start over
    learned = dict(known)
    learned.update(pins)

    rows_left = [r for r in rows if r not in learned]
    idx_left = [g["index"] for g in gpus if g["index"] not in learned.values()]
    if len(rows_left) == 1 and len(idx_left) == 1:    # only one way left to assign it
        learned[rows_left[0]] = idx_left[0]

    if len(set(learned.values())) != len(learned):     # two rows on one GPU: unusable
        learned = pins if len(set(pins.values())) == len(pins) else {}
    if learned != known:
        _save_map(gpus, learned)
    # Report whatever is known rather than nothing: each pin was established on its
    # own, so a half-learned map is half useful, and readings appear card by card as
    # the rest is worked out.
    return {idx: rows[row] for row, idx in learned.items() if row in rows}


def _unknown(gpus):
    """NVIDIA GPU indices whose nvidia-gpu-sensors row is not yet identified.

    AMD cards read their memory and junction temperatures straight from hwmon, with no
    row to line up, so they are never "unknown" and are filtered out here -- otherwise
    autolearn would try to load them with CUDA torch and wait out a budget for a map
    that is already complete.

    The driver states the order outright where /proc is available, in which case
    nothing is ever unknown and no learning happens at all."""
    gpus = _nvidia_only(gpus)
    known = set((_driver_map(gpus) or {}).values())
    known |= set((_load_map(gpus) or {}).values())
    return [g["index"] for g in gpus if g["index"] not in known]


def _load_gen():
    """(interpreter, stress.py) able to load a single GPU, or None.

    stress.py needs a CUDA-capable torch, which a monitoring host often does not have
    even when it runs GPUs -- a CPU-only torch, or none outside a container. Check
    before spawning, so a machine without one is told plainly instead of waiting out a
    load that never happens."""
    stress = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stress.py")
    if not os.path.isfile(stress):
        return None
    probe = "import torch, sys; sys.exit(0 if torch.cuda.device_count() else 1)"
    for py in (sys.executable, shutil.which("python3")):
        if not py:
            continue
        try:
            if subprocess.run([py, "-c", probe], timeout=60,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return py, stress
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def autolearn(binary, budget=75.0, size=16384, quiet=False):
    """Identify each GPU's sensor row, and remember it.

    Rows are matched to GPUs by core temperature, which distinguishes a card only while
    it sits at a temperature no other card shares. Every sample pins whatever it can
    and the result persists, so uneven load during ordinary use fills the map in by
    itself -- no load of our own required, and nothing is repeated once a card is
    known.

    When a CUDA-capable torch is available this also loads each still-unknown GPU
    briefly to force the issue, which turns "eventually" into "now". Without one, the
    passive route is all there is: the memory and hot-spot columns stay blank for the
    cards not yet recognised."""
    if not binary:
        return
    gpus = read_gpus()
    if not gpus:
        return
    read_gpu_extra(binary, gpus)                 # a free pin if the machine is uneven
    missing = _unknown(read_gpus())
    if not missing:
        return
    say = (lambda m: None) if quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    gen = _load_gen()
    if not gen:
        say(f"{PROG}: sensor row unknown for GPU(s) {', '.join(map(str, missing))}; "
            f"will be learned when they run at different temperatures "
            f"(no CUDA-capable torch here to force it)")
        return
    py, stress = gen
    say(f"{PROG}: identifying sensor rows for GPU(s) {', '.join(map(str, missing))} "
        f"\u2014 one-time, up to {budget:.0f}s each, stopping as soon as each is known")
    nv = _nvidia_only(gpus)
    for index in missing:
        # stress.py addresses cards by CUDA device, which counts NVIDIA cards only, while
        # `index` is our display index over both vendors. On a mixed host those differ, so
        # translate; on an NVIDIA-only host the mapping is the identity.
        cuda = [i for i, g in enumerate(nv) if g["index"] == index]
        cuda = cuda[0] if cuda else index
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID")
        try:
            proc = subprocess.Popen(
                [py, stress, "--gpu", str(cuda), "--size", str(size),
                 "--timeout", str(int(budget) + 5), "--interval", "1e9"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except (OSError, subprocess.SubprocessError) as err:
            say(f"{PROG}: could not start stress.py ({err})")
            return
        try:
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                if proc.poll() is not None:      # died early: report why, do not wait
                    why = (proc.stderr.read() or "").strip().splitlines()
                    say(f"{PROG}: stress.py exited: {why[-1] if why else 'no output'}")
                    return
                time.sleep(2.0)
                live = read_gpus()
                read_gpu_extra(binary, live)     # pins and persists as a side effect
                if live and index not in _unknown(live):
                    say(f"{PROG}: GPU {index} identified")
                    break
            else:
                say(f"{PROG}: GPU {index} did not become distinguishable")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def read_gpu_extra(binary, gpus=()):
    """Run nvidia-gpu-sensors once and align its rows to `gpus` (from nvidia-smi).

    Returns None when the binary itself is unusable (missing / not root / no table),
    so the caller can stop respawning it, versus {} when it ran but its rows could not
    be attributed to a GPU with confidence."""
    if not binary:
        return None
    try:
        out = subprocess.run([binary], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = _parse_gpu_sensors(out)
    if not rows:
        return None
    return _align(rows, list(gpus))


# ---- CPU temperature (sysfs hwmon) ---------------------------------------
def read_cpu_temp():
    """Representative CPU package temp in C from sysfs (host-mounted /sys in Docker)."""
    preferred, fallback = [], []
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            name = open(os.path.join(hw, "name")).read().strip()
        except OSError:
            continue
        is_cpu = name in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "cpu-thermal")
        is_gpu = name in ("amdgpu", "nouveau", "nvidia")
        for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            try:
                val = int(open(tf).read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            label = ""
            try: label = open(tf.replace("_input", "_label")).read().strip()
            except OSError: pass
            if is_cpu and (label in ("Tctl", "Tdie") or label.startswith("Package id")):
                preferred.append(val)
            elif is_cpu:
                fallback.append(val)
            elif not is_gpu and val > 0:
                fallback.append(val)
    if preferred: return max(preferred)
    if fallback:  return max(fallback)
    return None


# ---- Aquacomputer High Flow Next (sysfs hwmon) ---------------------------
def read_flow_sensor():
    """High Flow Next via the 'highflownext' hwmon driver: (water_temp_C, flow_L_per_h).
    Coolant temp is temp1_input (m°C); flow is a 'fan' channel reported in dL/h.
    Returns (None, None) if the sensor isn't present."""
    def label(input_path):
        try: return open(input_path.replace("_input", "_label")).read().strip()
        except OSError: return ""

    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(os.path.join(hw, "name")).read().strip() != "highflownext":
                continue
        except OSError:
            continue

        water = None
        for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            try: v = int(open(tf).read().strip()) / 1000.0
            except (OSError, ValueError): continue
            lab = label(tf).lower()
            if "coolant" in lab or "water" in lab:
                water = v; break
            if water is None:            # fall back to first temp (temp1 = coolant)
                water = v

        flow, first = None, None
        for ff in sorted(glob.glob(os.path.join(hw, "fan*_input"))):
            try: raw = int(open(ff).read().strip())
            except (OSError, ValueError): continue
            if first is None: first = raw / 10.0
            lab = label(ff).lower()
            if "flow" in lab:            # value is dL/h -> L/h
                flow = raw / 10.0 if "dl/h" in lab else float(raw)
                break
        if flow is None: flow = first
        return water, flow
    return None, None


# ---- CPU usage (/proc/stat deltas) ---------------------------------------
class CpuMeter:
    """Compute aggregate + per-core CPU busy% from successive /proc/stat reads."""
    def __init__(self):
        self.prev = self._read()

    @staticmethod
    def _read():
        times = {}
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if not line.startswith("cpu"):
                        break
                    parts = line.split()
                    vals = [int(x) for x in parts[1:]]
                    total = sum(vals[:8])          # user..steal
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle+iowait
                    times[parts[0]] = (total, idle)
        except OSError:
            pass
        return times

    def update(self):
        cur = self._read()
        def pct(name):
            if name not in self.prev or name not in cur:
                return None
            dt = cur[name][0] - self.prev[name][0]
            di = cur[name][1] - self.prev[name][1]
            return max(0.0, min(100.0, 100.0 * (dt - di) / dt)) if dt > 0 else 0.0
        agg = pct("cpu")
        cores = [pct(k) for k in sorted((k for k in cur if k != "cpu"),
                                        key=lambda s: int(s[3:]))]
        self.prev = cur
        return agg, cores


# ---- CPU package power (RAPL energy counters) ----------------------------
class RaplMeter:
    """Watts for CPU package(s) from /sys/class/powercap RAPL energy deltas."""
    def __init__(self):
        self.domains = self._find()      # [(energy_uj_path, max_energy_range_uj)]
        self.prev = self._energy()       # ([uj_per_domain], monotonic_time)

    @staticmethod
    def _find():
        doms = []
        for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
            if os.path.basename(d).count(":") != 1:   # skip subdomains intel-rapl:N:M
                continue
            try:
                name = open(os.path.join(d, "name")).read().strip()
            except OSError:
                continue
            if not name.startswith("package"):        # package = whole-socket power
                continue
            try:
                mx = int(open(os.path.join(d, "max_energy_range_uj")).read().strip())
            except (OSError, ValueError):
                mx = None
            doms.append((os.path.join(d, "energy_uj"), mx))
        return doms

    def _energy(self):
        vals = []
        for path, _ in self.domains:
            try:
                vals.append(int(open(path).read().strip()))
            except (OSError, ValueError):
                vals.append(None)
        return vals, time.monotonic()

    def watts(self):
        cur = self._energy()
        prev_vals, p_t = self.prev
        cur_vals, c_t = cur
        self.prev = cur
        dt = c_t - p_t
        if dt <= 0 or not self.domains:
            return None
        total, ok = 0.0, False
        for i, (_, mx) in enumerate(self.domains):
            pv, cv = prev_vals[i], cur_vals[i]
            if pv is None or cv is None:
                continue
            d = cv - pv
            if d < 0 and mx:      # 32/64-bit counter wrapped since last read
                d += mx
            if d < 0:
                continue
            total += d / 1e6 / dt
            ok = True
        return total if ok else None


# ---- CPU package power via turbostat (streamed in a daemon thread) -------
class TurbostatMeter:
    """Stream summary CPU power/freq from turbostat. Needs root + MSR access;
    stays silently inert (snapshot() -> None) if turbostat is missing or fails."""
    def __init__(self, interval):
        self.latest = None      # {"pkg","cor","busy","mhz"}
        self._proc = None
        binary = shutil.which("turbostat")
        for cand in ("/usr/sbin/turbostat", "/sbin/turbostat"):
            if binary is None and os.path.exists(cand):
                binary = cand
        if binary is None:
            return
        try:
            self._proc = subprocess.Popen(
                [binary, "--quiet", "--Summary", "--interval", "%g" % max(0.5, interval),
                 "--show", "Busy%,Bzy_MHz,CorWatt,PkgWatt"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self._proc = None
            return
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        header = None
        for line in self._proc.stdout:
            fields = line.split()
            if "PkgWatt" in fields:          # (re)appearing header row
                header = fields
                continue
            if not header or len(fields) != len(header):
                continue
            row = dict(zip(header, fields))
            def g(k):
                try: return float(row[k])
                except (KeyError, ValueError): return None
            snap = {"pkg": g("PkgWatt"), "cor": g("CorWatt"),
                    "busy": g("Busy%"), "mhz": g("Bzy_MHz")}
            if snap["pkg"] is not None:
                self.latest = snap

    def snapshot(self):
        return self.latest

    def stop(self):
        if self._proc:
            try: self._proc.terminate()
            except OSError: pass


# ---- formatting -----------------------------------------------------------
def fmt_temp(v):
    return "  n/a" if v is None else f"{v:>3.0f}C"


def temp_color(v):
    if v is None: return "grey"
    return "green" if v <= 60 else "yellow" if v < 80 else "red"


def frac_color(f):
    if f is None: return "grey"
    return "green" if f <= 0.5 else "yellow" if f <= 0.85 else "red"


def bar(pct, width, color):
    if pct is None:
        return c("░" * width, "grey")
    fill = max(0, min(width, int(round(pct / 100.0 * width))))
    return c("█" * fill, color) + c("░" * (width - fill), "grey")


def core_grid(cores, width):
    """Per-core usage as 'NN [bar] PPP%' cells, wrapped to the terminal width."""
    BAR_W = 5
    cells = []
    for i, p in enumerate(cores):
        col = frac_color(None if p is None else p / 100)
        pct = " n/a" if p is None else "%3.0f%%" % p
        cells.append("%2d %s %s" % (i, bar(p, BAR_W, col), c(pct, col)))
    cw = 2 + 1 + BAR_W + 1 + 4          # visible width of one cell (no ANSI)
    per_row = max(1, (width - 2) // (cw + 2))
    return ["    " + "  ".join(cells[i:i + per_row]) for i in range(0, len(cells), per_row)]


# ---- frame ----------------------------------------------------------------
def build_frame(gpus, cpu, cooling, interval, width):
    L = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ncpu = len(cpu["cores"])
    L.append(f"{c(bold(PROG), 'cyan')}  {c(ts, 'grey')}"
             f"    {c('refresh %gs' % interval, 'grey')}")
    L.append("")

    if not gpus:
        L.append(c("  no GPUs found (nvidia-smi / /sys/class/drm both empty — "
                   "driver loaded, /sys mounted?)", "red"))
    for g in gpus:
        prefix = "  GPU %d  " % g["index"]          # visible prefix width (no ANSI)
        avail = max(12, width - len(prefix) - 1)
        name = g["name"]
        if len(name) > avail: name = name[:avail - 1] + "…"
        label = c(bold("GPU " + str(g["index"])), "blue")
        vtag = c(" [%s]" % g["vendor"], "grey") if g.get("vendor") else ""
        L.append("  " + label + "  " + name + vtag)

        clk = ("%.0fMHz" % g["clock"]) if g["clock"] is not None else "n/a"
        util = g["util"]
        utilstr = ("%3.0f%%" % util) if util is not None else " n/a"
        L.append("    util " + bar(util, 22, "cyan") + " " + utilstr
                 + "   " + c(clk, "grey"))

        used, tot = g["mem_used"], g["mem_total"]
        f = (used / tot) if (used is not None and tot) else None
        memtxt = f"{used:.0f} / {tot:.0f} MiB" if f is not None else "n/a"
        pctstr = f"{f * 100:3.0f}%" if f is not None else " n/a"
        L.append(f"    vram {bar(None if f is None else f * 100, 22, frac_color(f))} "
                 f"{pctstr}   {c(memtxt, 'grey')}")

        pw = (f"{g['power']:.0f}/{g['power_max']:.0f}W"
              if g["power"] is not None and g["power_max"] is not None
              else (f"{g['power']:.0f}W" if g["power"] is not None else "n/a"))
        # AMD hwmon reports fan RPM where NVIDIA reports duty %, so the unit travels with
        # the value rather than being assumed.
        if g["fan"] is None:
            fan = "n/a"
        elif g.get("fan_unit") == "rpm":
            fan = "%.0frpm" % g["fan"]
        else:
            fan = "%.0f%%" % g["fan"]
        hs = g.get("hotspot")
        learning = g.get("sensor_learning")
        hs_cell = c(fmt_temp(hs), temp_color(hs)) if hs is not None \
            else (c("learning", "yellow") if learning else None)
        hs_part = ("   hotspot " + hs_cell) if hs_cell else ""
        mem_cell = c("learning", "yellow") if (g["mem_temp"] is None and learning) \
            else c(fmt_temp(g["mem_temp"]), temp_color(g["mem_temp"]))
        L.append(f"    temp {c(fmt_temp(g['temp']), temp_color(g['temp']))}"
                 f"   mem-temp {mem_cell}"
                 f"{hs_part}   fan {fan:>6}   power {pw}")
        L.append("")

    pending = [g["index"] for g in gpus if g.get("sensor_learning")]
    if pending:
        done = len(gpus) - len(pending)
        L.append(f"  {c(bold('Sensor map'), 'blue')}  "
                 f"{bar(100.0 * done / max(len(gpus), 1), 22, 'yellow')}  "
                 f"{done}/{len(gpus)} GPUs identified"
                 f"   {c('learning: ' + ', '.join('GPU %d' % i for i in pending), 'yellow')}")
        L.append(c("    nvidia-gpu-sensors numbers its rows differently to nvidia-smi; a GPU is "
                   "matched once it runs", "grey"))
        L.append(c("    warmer than the others. Memory and hot-spot temps appear for it then, "
                   "and are remembered.", "grey"))
        L.append("")

    # CPU section
    L.append(f"  {c(bold('CPU'), 'blue')}  {ncpu} cores")
    agg = cpu["usage"]
    cpu_power = cpu.get("power")
    aggstr = f"{agg:3.0f}%" if agg is not None else " n/a"
    extras = []
    if cpu.get("mhz") is not None:
        extras.append(c("%.0fMHz" % cpu["mhz"], "grey"))
    extras.append("temp " + c(fmt_temp(cpu["temp"]), temp_color(cpu["temp"])))
    if cpu_power is not None:
        pwtxt = "%.0fW" % cpu_power
        if cpu.get("cores_power") is not None:
            pwtxt += " (cores %.0fW)" % cpu["cores_power"]
        extras.append("power " + c(pwtxt, "cyan"))
    else:
        extras.append("power " + c("n/a", "grey"))
    L.append(f"    usage {bar(agg, 22, frac_color(None if agg is None else agg / 100))} "
             f"{aggstr}   " + "   ".join(extras))
    if cpu["cores"]:
        L.extend(core_grid(cpu["cores"], width))

    # Coolant loop (Aquacomputer High Flow Next)
    w, fl = cooling.get("water"), cooling.get("flow")
    if w is not None or fl is not None:
        L.append("")
        wtxt = c("%.1fC" % w, temp_color(w)) if w is not None else c("n/a", "grey")
        fltxt = c("%.1f L/h" % fl, "cyan") if fl is not None else c("n/a", "grey")
        L.append(f"  {c(bold('Coolant'), 'blue')}  water {wtxt}   flow {fltxt}")
    L.append("")

    # total power: sum of GPU board power + CPU package power
    gpu_pw = [g["power"] for g in gpus if g["power"] is not None]
    if gpu_pw or cpu_power is not None:
        gp = sum(gpu_pw)
        parts = []
        if gpu_pw:                parts.append(f"GPU {gp:.0f}W")
        if cpu_power is not None:  parts.append(f"CPU {cpu_power:.0f}W")
        total = gp + (cpu_power or 0.0)
        parts.append(bold(f"total {total:.0f}W"))
        L.append(f"  {c(bold('Power'), 'blue')}  " + c("   ".join(parts), "cyan"))
        L.append("")

    L.append(c("  Ctrl-C to quit", "grey"))
    return L


def render(lines):
    # cursor home, redraw each line clearing to EOL, then clear below
    out = "\033[H" + "".join(line + "\033[K\n" for line in lines) + "\033[J"
    sys.stdout.write(out); sys.stdout.flush()


def main():
    global COLOR
    args = parse_args()
    tty = sys.stdout.isatty()
    COLOR = tty and not args.no_color
    once = args.once or not tty

    meter = CpuMeter()
    rapl = RaplMeter()
    turbo = TurbostatMeter(args.interval)
    gpu_sensors = find_gpu_sensors(args.gpu_sensors)
    gs_fails = 0   # disable the helper after repeated empties (unsupported / not root)
    busy_cache = {}  # per-bus fdinfo differencers, kept across frames (AMD utilisation)
    if not args.no_autolearn:
        # One-time: work out which sensor row is which GPU, then remember it. No-op
        # once the map is complete, so only the first run on a machine pays for it.
        # In the live view the frame carries the progress; only speak up when there
        # is no frame to repeat it (--once, or piped output).
        autolearn(gpu_sensors, quiet=not once)
    # prime CPU/power deltas so the first frame shows real values, not noise
    time.sleep(min(args.interval, 0.3))
    if once and turbo.snapshot() is None:
        deadline = time.monotonic() + args.interval + 1.0   # let turbostat emit one row
        while turbo.snapshot() is None and time.monotonic() < deadline:
            time.sleep(0.1)

    if not once:
        sys.stdout.write("\033[2J\033[?25l")  # clear screen, hide cursor
    try:
        while True:
            width = shutil.get_terminal_size((100, 40)).columns
            gpus = read_gpus(vendor=args.vendor, busy=busy_cache)
            extra = read_gpu_extra(gpu_sensors, gpus)  # mem/hot-spot temps nvidia-smi lacks
            if gpu_sensors and gpus:
                if extra is None:                 # binary unusable, not merely unaligned
                    gs_fails += 1
                    if gs_fails >= 3:             # never works here — stop spawning it
                        gpu_sensors = None
                else:
                    gs_fails = 0
            extra = extra or {}
            pending = set(_unknown(gpus)) if (gpu_sensors and gpus) else set()
            for g in gpus:
                g["sensor_learning"] = g["index"] in pending
                ex = extra.get(g["index"])
                if not ex:
                    continue
                if g["mem_temp"] is None and ex["mem"] is not None:
                    g["mem_temp"] = ex["mem"]
                if ex.get("hotspot") is not None:
                    g["hotspot"] = ex["hotspot"]
            agg, cores = meter.update()
            water, flow = read_flow_sensor()
            # prefer turbostat for package power (accurate); fall back to RAPL
            snap = turbo.snapshot()
            if snap and snap.get("pkg") is not None:
                pkg_w, cor_w, mhz = snap["pkg"], snap.get("cor"), snap.get("mhz")
            else:
                pkg_w, cor_w, mhz = rapl.watts(), None, None
            cpu = {"usage": agg, "cores": cores, "temp": read_cpu_temp(),
                   "power": pkg_w, "cores_power": cor_w, "mhz": mhz}
            cooling = {"water": water, "flow": flow}
            frame = build_frame(gpus, cpu, cooling, args.interval, width)
            if once:
                print("\n".join(frame))
                break
            render(frame)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        turbo.stop()
        if not once:
            sys.stdout.write("\033[?25h\n")  # show cursor
            sys.stdout.flush()


main()
