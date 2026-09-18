import argparse, glob, os, subprocess, threading, time
import torch

def parse_args():
    p = argparse.ArgumentParser(description="Multi-GPU matmul stress test with temp monitoring")
    p.add_argument("--timeout", type=int, default=300, help="run duration in seconds (default: 300 = 5 min)")
    p.add_argument("--gpu", type=int, action="append", default=None,
                   help="GPU id to stress, repeatable (default: all GPUs)")
    p.add_argument("--interval", type=float, default=5.0, help="temp sample interval in seconds (default: 5)")
    p.add_argument("--size", type=int, default=16384, help="matmul dimension (default: 16384)")
    return p.parse_args()

def read_gpu_temps():
    """Return {gpu_index: (gpu_temp_C, mem_temp_C)} for whatever GPUs are present.

    NVIDIA comes from nvidia-smi (always present in NGC images). AMD has no such tool
    guaranteed, so it is read from the amdgpu hwmon nodes, which report edge/junction/mem
    labelled channels. Cards from both vendors are returned in one dict, AMD numbered
    after NVIDIA, matching supersensor's own ordering."""
    res = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu,temperature.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        out = ""
    def f(x):
        try: return float(x)
        except ValueError: return None  # "N/A"
    n = 0
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        idx, g, m = parts
        res[int(idx)] = (f(g), f(m))
        n = max(n, int(idx) + 1)
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        try:
            if open(os.path.join(dev, "vendor")).read().strip() != "0x1002":
                continue
            if open(os.path.join(dev, "class")).read().strip()[:4] != "0x03":
                continue
        except OSError:
            continue
        edge = mem = None
        for hw in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
            for tf in glob.glob(os.path.join(hw, "temp*_input")):
                try: lab = open(tf.replace("_input", "_label")).read().strip()
                except OSError: continue
                try: val = int(open(tf).read().strip()) / 1000.0
                except (OSError, ValueError): continue
                if lab == "edge": edge = val
                elif lab == "mem": mem = val
        res[n] = (edge, mem)
        n += 1
    return res

def read_cpu_temp():
    """Representative CPU package temp in C from sysfs (works in Docker, /sys is host-mounted)."""
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

class Sampler:
    def __init__(self, interval):
        self.interval = interval
        self.samples = []  # (timestamp, {gpu: (gpu_t, mem_t)}, cpu_t)
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:    gpus = read_gpu_temps()
            except Exception: gpus = {}
            cpu = read_cpu_temp()
            self.samples.append((time.time(), gpus, cpu))
            parts = [time.strftime("[%H:%M:%S]")]
            for i in sorted(gpus):
                g, m = gpus[i]
                parts.append(f"GPU{i} {fmt(g)}/mem {fmt(m)}")
            parts.append(f"CPU {fmt(cpu)}")
            print("  ".join(parts), flush=True)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

def fmt(v):
    return "N/A" if v is None else f"{v:.0f}C"

def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals: return "N/A"
    return f"min {min(vals):.0f}C / avg {sum(vals)/len(vals):.1f}C / max {max(vals):.0f}C"

def main():
    args = parse_args()
    n = torch.cuda.device_count()
    if n == 0:
        raise SystemExit("No CUDA devices visible")
    gpus = args.gpu if args.gpu else list(range(n))
    bad = [g for g in gpus if g < 0 or g >= n]
    if bad:
        raise SystemExit(f"Invalid GPU id(s) {bad}, only {n} GPU(s) visible (0..{n-1})")

    print(f"torch {torch.__version__} | cuda {torch.version.cuda} | {n} GPU(s) visible")
    print(f"Stressing GPU(s) {gpus} for {args.timeout}s, size {args.size}x{args.size} fp16, sampling every {args.interval}s")

    sampler = Sampler(args.interval)
    mon = threading.Thread(target=sampler.run, daemon=True)
    mon.start()

    errors = {}
    def burn(dev):
        try:
            torch.cuda.set_device(dev)
            name = torch.cuda.get_device_name(dev)
            a = torch.randn((args.size, args.size), device=f"cuda:{dev}", dtype=torch.float16)
            b = torch.randn((args.size, args.size), device=f"cuda:{dev}", dtype=torch.float16)
            c = torch.empty_like(a)
            torch.cuda.synchronize(dev)
            print(f"GPU {dev} ({name}): burn started", flush=True)
            end = time.time() + args.timeout
            iters = 0
            while time.time() < end:
                torch.mm(a, b, out=c)
                a, c = c, a
                iters += 1
            torch.cuda.synchronize(dev)
            print(f"GPU {dev}: {iters} matmuls done", flush=True)
        except Exception as e:
            errors[dev] = e
            print(f"GPU {dev}: FAILED: {e}", flush=True)

    threads = [threading.Thread(target=burn, args=(g,)) for g in gpus]
    for t in threads: t.start()
    for t in threads: t.join()
    sampler.stop(); mon.join()

    print("\n===== SUMMARY =====")
    for g in gpus:
        gpu_t = [s[1][g][0] for s in sampler.samples if g in s[1]]
        mem_t = [s[1][g][1] for s in sampler.samples if g in s[1]]
        status = f"FAILED ({errors[g]})" if g in errors else "OK"
        print(f"GPU {g} [{status}]")
        print(f"  core temp:    {stats(gpu_t)}")
        print(f"  memory temp:  {stats(mem_t)}")
    print(f"CPU temp:       {stats([s[2] for s in sampler.samples])}")
    if errors:
        raise SystemExit(1)

main()

