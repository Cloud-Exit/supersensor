#!/usr/bin/env python3
import argparse, glob, itertools, os, shutil, subprocess, sys, threading, time

PROG = "supersensor"

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
        description="supersensor: mini-nvtop live monitor — GPU (nvidia-smi), CPU usage/power "
                    "(turbostat/RAPL), and Aquacomputer High Flow Next coolant temp + flow")
    p.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1)")
    p.add_argument("--once", action="store_true", help="print a single frame and exit")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--gpu-sensors", metavar="PATH", default=None,
                   help="path to nvidia-gpu-sensors binary (for GPU mem/hot-spot temps; "
                        "auto-detected, or set SUPERSENSOR_GPU_SENSORS)")
    return p.parse_args()


# ---- GPU (nvidia-smi) -----------------------------------------------------
# Full field set, then a minimal one: older drivers reject unknown query fields
# with a non-zero exit (losing everything), so we retry with just the essentials.
GPU_FIELDS = ["index", "name", "utilization.gpu", "memory.used", "memory.total",
              "temperature.gpu", "temperature.memory", "power.draw", "power.limit",
              "fan.speed", "clocks.sm"]
GPU_FIELDS_MIN = ["index", "name", "utilization.gpu", "memory.used", "memory.total",
                  "temperature.gpu", "power.draw", "power.limit"]


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
                "name": d.get("name") or "GPU",
                "util": num(d.get("utilization.gpu")),
                "mem_used": num(d.get("memory.used")), "mem_total": num(d.get("memory.total")),
                "temp": num(d.get("temperature.gpu")), "mem_temp": num(d.get("temperature.memory")),
                "power": num(d.get("power.draw")), "power_max": num(d.get("power.limit")),
                "fan": num(d.get("fan.speed")), "clock": num(d.get("clocks.sm")),
            })
        if gpus:
            return gpus
    return []


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


def _align(rows, gpus, tol=3.0):
    """Map nvidia-gpu-sensors rows onto nvidia-smi GPUs by core temperature.

    The two tools enumerate independently -- nvidia-gpu-sensors walks RM's
    GPU_GET_PROBED_IDS and never prints a PCI bus id -- so equal indices need not be
    the same card, and a straight index join can attribute one GPU's memory reading to
    another. Both report core temperature, so join on that: take the identity mapping
    when it fits, else the unique permutation that does, else report nothing rather
    than something wrong."""
    if not rows or not gpus or len(rows) != len(gpus) or len(gpus) > 8:
        return {}
    smi = [g.get("temp") for g in gpus]
    idx = sorted(rows)
    if any(t is None for t in smi) or any(rows[i]["core"] is None for i in idx):
        return {}

    def fits(order):
        return all(abs(rows[r]["core"] - t) <= tol for r, t in zip(order, smi))

    if not fits(idx):
        ok = [p for p in itertools.permutations(idx) if fits(p)]
        if len(ok) != 1:
            return {}          # ambiguous or impossible -- do not guess
        idx = ok[0]
    return {g["index"]: rows[r] for g, r in zip(gpus, idx)}


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
        L.append(c("  nvidia-smi returned no GPUs (is it installed / are GPUs visible?)", "red"))
    for g in gpus:
        prefix = "  GPU %d  " % g["index"]          # visible prefix width (no ANSI)
        avail = max(12, width - len(prefix) - 1)
        name = g["name"]
        if len(name) > avail: name = name[:avail - 1] + "…"
        label = c(bold("GPU " + str(g["index"])), "blue")
        L.append("  " + label + "  " + name)

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
              if g["power"] is not None and g["power_max"] is not None else "n/a")
        fan = f"{g['fan']:.0f}%" if g["fan"] is not None else "n/a"
        hs = g.get("hotspot")
        hs_part = ("   hotspot " + c(fmt_temp(hs), temp_color(hs))) if hs is not None else ""
        L.append(f"    temp {c(fmt_temp(g['temp']), temp_color(g['temp']))}"
                 f"   mem-temp {c(fmt_temp(g['mem_temp']), temp_color(g['mem_temp']))}"
                 f"{hs_part}   fan {fan:>4}   power {pw}")
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
            gpus = read_gpu_info()
            extra = read_gpu_extra(gpu_sensors, gpus)  # mem/hot-spot temps nvidia-smi lacks
            if gpu_sensors and gpus:
                if extra is None:                 # binary unusable, not merely unaligned
                    gs_fails += 1
                    if gs_fails >= 3:             # never works here — stop spawning it
                        gpu_sensors = None
                else:
                    gs_fails = 0
            extra = extra or {}
            for g in gpus:
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
