#!/usr/bin/env python3
"""Fixture checks for supersensor's AMD/NVIDIA sysfs readers.

Builds a throwaway sysfs-shaped tree (real symlinks, as the kernel has) and points the
module's path roots at it, so the readers are exercised on cards that are not present --
an RX 9700 AI PRO among them -- without needing the hardware."""
import importlib.util, glob as globmod, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("ss", os.path.join(HERE, "supersensor.py"))
    m = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["supersensor"]
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    sys.argv = argv
    return m


def mk(path, val):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(str(val))


def symlink(target, link):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(target, link)


def build_tree(root):
    """A sysfs fragment: two NVIDIA cards (one healthy, one with only /proc) and an
    R9700 with the full amdgpu sensor set."""
    devices = []

    def card(bus, vendor, device, subv, subd, driver, cards=(), hwmon=None):
        dev = os.path.join(root, "pci", bus)
        mk(os.path.join(dev, "class"), "0x030000")
        mk(os.path.join(dev, "vendor"), hex(vendor))
        mk(os.path.join(dev, "device"), hex(device))
        mk(os.path.join(dev, "subsystem_vendor"), hex(subv))
        mk(os.path.join(dev, "subsystem_device"), hex(subd))
        # driver symlink exactly as the kernel exposes it
        drv = os.path.join(root, "bus", "pci", "drivers", driver)
        os.makedirs(drv, exist_ok=True)
        symlink(drv, os.path.join(dev, "driver"))
        if hwmon:
            # Real layout: device/hwmon/hwmonN is the sensor node, /sys/class/hwmon/hwmonN
            # links to it, and hwmonN/device links back to the owning PCI device.
            hw = os.path.join(dev, "hwmon", hwmon)
            mk(os.path.join(hw, "name"), driver)
            symlink(hw, os.path.join(root, "hwmon", hwmon))
            symlink(dev, os.path.join(hw, "device"))
        for cname, attrs in cards:
            cd = os.path.join(dev, "drm", cname)
            # cardN/device -> PCI device; attributes therefore live on the PCI device dir
            symlink(dev, os.path.join(cd, "device"))
            for k, v in attrs.items():
                mk(os.path.join(dev, k), v)
        devices.append(dev)
        return dev

    # Healthy NVIDIA card: hwmon with junction temp + power, and a DRM node with VRAM.
    nv = card("0000:41:00.0", 0x10DE, 0x2B85, 0x10DE, 0x16A1, "nvidia", hwmon="hwmon7",
              cards=[("card5", {"mem_info_vram_used": 1073741824,
                                "mem_info_vram_total": 34359738368})])
    nhw = os.path.join(nv, "hwmon", "hwmon7")
    mk(os.path.join(nhw, "temp1_input"), 52000)
    mk(os.path.join(nhw, "temp1_label"), "junction")
    mk(os.path.join(nhw, "power1_input"), 350000000)
    # NVIDIA card with the driver wedged: no hwmon, no DRM VRAM.
    card("0000:01:00.0", 0x10DE, 0x2B85, 0x10DE, 0x16A1, "nvidia")

    # AMD Radeon AI PRO R9700 (1002:7551) with every sensor amdgpu can expose.
    r9 = card("0000:03:00.0", 0x1002, 0x7551, 0x1DA2, 0xE490, "amdgpu", hwmon="hwmon9",
              cards=[("card6", {"gpu_busy_percent": 87,
                                "mem_info_vram_used": 12884901888,
                                "mem_info_vram_total": 34359738368})])
    hw = os.path.join(root, "hwmon", "hwmon9")
    for chan, (label, val) in {"1": ("edge", 48000), "2": ("junction", 61000),
                               "3": ("mem", 58000)}.items():
        mk(os.path.join(hw, "temp%s_input" % chan), val)
        mk(os.path.join(hw, "temp%s_label" % chan), label)
    mk(os.path.join(hw, "power1_input"), 215000000)
    mk(os.path.join(hw, "power1_cap"), 300000000)
    mk(os.path.join(hw, "fan1_input"), 1750)
    mk(os.path.join(hw, "freq1_input"), 2400000000)
    del r9
    return devices


def patch(m, root, devices):
    real_glob = globmod.glob

    def fake_glob(pat, *a, **k):
        if pat == "/sys/bus/pci/devices/*":
            return list(devices)
        if pat == "/sys/class/hwmon/hwmon*":
            return real_glob(os.path.join(root, "hwmon", "hwmon*"))
        if pat == "/sys/class/drm/card[0-9]*":
            return [p for p in real_glob(os.path.join(root, "pci", "*", "drm", "card*"))
                    if real_glob(p)]
        if os.path.isabs(pat) and not pat.startswith(root) and not pat.startswith("/sys"):
            return real_glob(pat)
        return real_glob(pat, *a, **k)

    m.glob = type("G", (), {"glob": staticmethod(fake_glob)})()
    return m


def main():
    root = tempfile.mkdtemp(prefix="supersensor-fixture-")
    try:
        m = patch(load(), root, build_tree(root))
        fails = []

        def check(cond, msg):
            print(("  ok   " if cond else "  FAIL ") + msg)
            if not cond:
                fails.append(msg)

        print("AMD R9700 (autodetected from sysfs):")
        amd = m.read_amd_sysfs()
        check(len(amd) == 1, "exactly one AMD card found")
        if amd:
            g = amd[0]
            check(g["vendor"] == "amd", "vendor tagged amd")
            check("R9700" in g["name"], "name from pci.ids: %r" % g["name"])
            check(g["temp"] == 48.0, "edge temp -> temp (%s)" % g["temp"])
            check(g["hotspot"] == 61.0, "junction -> hotspot (%s)" % g["hotspot"])
            check(g["mem_temp"] == 58.0, "mem channel -> mem_temp (%s)" % g["mem_temp"])
            check(abs(g["power"] - 215.0) < 1e-6, "power1_input -> W (%s)" % g["power"])
            check(g["power_max"] == 300.0, "power1_cap -> limit (%s)" % g["power_max"])
            check(g["fan"] == 1750 and g["fan_unit"] == "rpm", "fan in rpm")
            check(g["util"] == 87, "gpu_busy_percent -> util")
            check(abs(g["mem_total"] - 32768) < 1, "vram total MiB (%s)" % g["mem_total"])
            check(g["clock"] == 2400.0, "clock MHz (%s)" % g["clock"])

        print("NVIDIA sysfs fallback:")
        nv = m.read_nvidia_sysfs()
        check(len(nv) == 2, "both NVIDIA cards found with nvidia-smi dead")
        healthy = [g for g in nv if g["bus"] == "0000:41:00.0"]
        if healthy:
            g = healthy[0]
            check(g["hotspot"] == 52.0, "junction -> hotspot (%s)" % g["hotspot"])
            check(abs(g["power"] - 350.0) < 1e-6, "power -> W (%s)" % g["power"])
            check(abs(g["mem_total"] - 32768) < 1, "vram from DRM node")

        print("combined read_gpus (smi unavailable):")
        m.read_gpu_info = lambda: []
        allg = m.read_gpus()
        check(len(allg) == 3, "all three cards collected (%d)" % len(allg))
        check([g["index"] for g in allg] == [0, 1, 2], "indices are unique and dense")
        check([g["vendor"] for g in allg] == ["nvidia", "nvidia", "amd"],
              "NVIDIA keeps its order, AMD numbered after")
        check(len(m._nvidia_only(allg)) == 2, "_nvidia_only filters AMD out of row alignment")
        # The NVIDIA fixture cards have no /proc/driver/nvidia, so they are legitimately
        # unknown; the AMD card is not, because hwmon needs no row alignment at all.
        check(m._unknown(allg) == [0, 1], "only NVIDIA cards are ever 'unknown' (%s)"
              % m._unknown(allg))
        check(2 not in m._unknown(allg), "the AMD card is never 'unknown'")
        check(m.read_gpus(vendor="amd") and
              all(g["vendor"] == "amd" for g in m.read_gpus(vendor="amd")),
              "--vendor amd restricts to AMD")

        print("fdinfo utilisation fallback (kernels without gpu_busy_percent):")
        clock = [1000.0]
        counter = {"ns": 0}

        def fake_ns(card):
            return counter["ns"]

        real_ns = m._fdinfo_engine_ns
        real_mono = m.time.monotonic
        m._fdinfo_engine_ns = fake_ns
        m.time = type("T", (), {"monotonic": staticmethod(lambda: clock[0])})()
        try:
            b = m._FdinfoBusy()
            check(b.update("card") is None, "first sample has no interval -> n/a")
            clock[0] += 1.0
            counter["ns"] += 500000000                    # 0.5s busy in 1s wall
            check(abs(b.update("card") - 50.0) < 0.01, "half a second busy -> 50%")
            clock[0] += 1.0
            counter["ns"] += 2000000000                   # 2s busy in 1s wall
            check(abs(b.update("card") - 100.0) < 0.01, "over-subscribed clamps to 100%")
            counter["ns"] = 0                             # counter reset
            clock[0] += 1.0
            check(b.update("card") is None, "counter reset is not reported as negative")
            m.glob = type("G", (), {"glob": staticmethod(
                lambda pat, *a, **k: [] if pat == "/sys/bus/pci/devices/*" else
                real_glob(pat, *a, **k))})()
        finally:
            m._fdinfo_engine_ns = real_ns
            m.time = type("T", (), {"monotonic": staticmethod(real_mono)})()

        print("\n%d check(s) failed" % len(fails))
        return 1 if fails else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())