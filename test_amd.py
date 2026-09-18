#!/usr/bin/env python3
"""Fixture checks for supersensor's AMD/NVIDIA sysfs readers.

Builds a throwaway sysfs-shaped tree (real symlinks, as the kernel has) and points the
module's path roots at it, so the readers are exercised on cards that are not present --
an RX 9700 AI PRO among them -- without needing the hardware."""
import importlib.util, glob as globmod, os, re, shutil, sys, tempfile, time

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

    # Healthy NVIDIA card. The kernel exposes no mem_info_vram_* for the nvidia driver
    # (nvidia-smi gets VRAM over libnvidia-ml ioctls), so the fixture must not write
    # them either. Two labelled channels, with junction on temp1, so a reader that
    # blindly treats temp1_input as the core temperature is caught reporting one sensor
    # as both core and hot spot.
    nv = card("0000:41:00.0", 0x10DE, 0x2B85, 0x10DE, 0x16A1, "nvidia", hwmon="hwmon7",
              cards=[("card5", {})])
    nhw = os.path.join(nv, "hwmon", "hwmon7")
    mk(os.path.join(nhw, "temp2_input"), 44000)
    mk(os.path.join(nhw, "temp2_label"), "edge")
    mk(os.path.join(nhw, "temp1_input"), 52000)
    mk(os.path.join(nhw, "temp1_label"), "junction")
    mk(os.path.join(nhw, "power1_input"), 350000000)
    # NVIDIA card with the driver wedged: no hwmon at all.
    card("0000:01:00.0", 0x10DE, 0x2B85, 0x10DE, 0x16A1, "nvidia")
    # A card bound to vfio-pci for passthrough: display class, NVIDIA vendor ids, but
    # neither nvidia nor amdgpu bound, so it must not show as an all-n/a phantom.
    card("0000:02:00.0", 0x10DE, 0x2B85, 0x10DE, 0x16A1, "vfio-pci")
    # A nouveau card. No nvidia-smi exists on such a host, so this reader is the only
    # source; nouveau's hwmon node is an *unlabelled* temp1_input.
    nov = card("0000:04:00.0", 0x10DE, 0x1B80, 0x10DE, 0x11A3, "nouveau", hwmon="hwmon8")
    mk(os.path.join(nov, "hwmon", "hwmon8", "temp1_input"), 61000)

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

    # An old radeon card: same vendor, but an unlabelled temp1_input, no junction/mem/fan
    # channels, and a DRM node with no gpu_busy_percent -- so it is also the card that
    # exercises the fdinfo fallback.
    rd = card("0000:05:00.0", 0x1002, 0x67DF, 0x1002, 0x0B37, "radeon", hwmon="hwmon10",
              cards=[("card7", {"mem_info_vram_used": 1073741824,
                                "mem_info_vram_total": 8589934592})])
    mk(os.path.join(rd, "hwmon", "hwmon10", "temp1_input"), 73000)
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


def load_stress_reader():
    """stress.py's read_gpu_temps, with torch stubbed out (it need not be installed)."""
    src = open(os.path.join(HERE, "stress.py")).read().split("class Sampler")[0]
    src = src.replace("import torch", "")
    ns = {}
    exec(compile(src, "stress.py", "exec"), ns)
    return ns["read_gpu_temps"]


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
        check(len(amd) == 2, "the amdgpu and radeon cards, and nothing else (%d)" % len(amd))
        rd = [g for g in amd if g["bus"] == "0000:05:00.0"]
        check(bool(rd), "the radeon card is collected")
        if rd:
            check(rd[0]["temp"] == 73.0,
                  "radeon's unlabelled temp1_input -> temp (%s)" % rd[0]["temp"])
            check(rd[0]["hotspot"] is None and rd[0]["mem_temp"] is None,
                  "radeon invents no junction/mem channel it does not have")
        amd = [g for g in amd if g["bus"] == "0000:03:00.0"]
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
        check(len(nv) == 3, "both bound NVIDIA cards plus the nouveau card (%d)" % len(nv))
        check(all(g["bus"] != "0000:02:00.0" for g in nv),
              "the vfio-pci passthrough card is not a phantom GPU")
        nov = [g for g in nv if g["bus"] == "0000:04:00.0"]
        check(bool(nov), "a nouveau host (no nvidia-smi at all) still lists its card")
        if nov:
            check(nov[0]["temp"] == 61.0,
                  "nouveau's unlabelled temp1_input -> temp (%s)" % nov[0]["temp"])
            check(nov[0]["hotspot"] is None, "unlabelled channel does not invent a hot spot")
        healthy = [g for g in nv if g["bus"] == "0000:41:00.0"]
        if healthy:
            g = healthy[0]
            check(g["hotspot"] == 52.0, "junction -> hotspot (%s)" % g["hotspot"])
            # junction sits on temp1, so this catches temp being read from temp1_input
            # unconditionally and mirroring the hot spot.
            check(g["temp"] == 44.0, "edge -> temp, not temp1 (%s)" % g["temp"])
            check(abs(g["power"] - 350.0) < 1e-6, "power -> W (%s)" % g["power"])
            check(g["mem_total"] is None and g["mem_used"] is None,
                  "nvidia has no mem_info_vram_* in sysfs, so VRAM stays n/a")

        print("combined read_gpus (smi unavailable):")
        m.read_gpu_info = lambda: []
        allg = m.read_gpus()
        # 3 readable NVIDIA-vendor cards (nvidia x2, nouveau) + 2 AMD (amdgpu, radeon);
        # the vfio card is out.
        check(len(allg) == 5, "every readable card collected (%d)" % len(allg))
        check([g["index"] for g in allg] == [0, 1, 2, 3, 4], "indices are unique and dense")
        check([g["vendor"] for g in allg] == ["nvidia"] * 3 + ["amd"] * 2,
              "NVIDIA-vendor cards first, AMD numbered after")
        check(all(g["bus"] != "0000:02:00.0" for g in allg),
              "the vfio-pci card is excluded from every vendor")
        check(len(m._nvidia_only(allg)) == 3, "_nvidia_only filters AMD out of row alignment")
        # The NVIDIA-vendor fixture cards have no /proc/driver/nvidia, so they are
        # legitimately unknown; the AMD card is not, because its hwmon needs no row
        # alignment at all.
        check(m._unknown(allg) == [0, 1, 2], "only NVIDIA cards are ever 'unknown' (%s)"
              % m._unknown(allg))
        check(3 not in m._unknown(allg), "the AMD card is never 'unknown'")
        check(m.read_gpus(vendor="amd") and
              all(g["vendor"] == "amd" for g in m.read_gpus(vendor="amd")),
              "--vendor amd restricts to AMD")
        check(set(g["bus"] for g in m.read_gpus(vendor="nvidia")) ==
              {"0000:41:00.0", "0000:01:00.0", "0000:04:00.0"},
              "--vendor nvidia includes the nouveau card")

        print("fdinfo utilisation fallback (kernels without gpu_busy_percent):")
        clock = [1000.0]
        counter = {"ns": 0}
        real_mono = m.time.monotonic
        m.time = type("T", (), {"monotonic": staticmethod(lambda: clock[0])})()
        try:
            b = m._FdinfoBusy()
            check(b.update(None) is None, "unreadable counter -> n/a")
            check(b.update(counter["ns"]) is None, "first sample has no interval -> n/a")
            clock[0] += 1.0
            counter["ns"] += 500000000                    # 0.5s busy in 1s wall
            check(abs(b.update(counter["ns"]) - 50.0) < 0.01, "half a second busy -> 50%")
            clock[0] += 1.0
            counter["ns"] += 2000000000                   # 2s busy in 1s wall
            check(abs(b.update(counter["ns"]) - 100.0) < 0.01,
                  "over-subscribed clamps to 100%")
            counter["ns"] = 0                             # counter reset
            clock[0] += 1.0
            check(b.update(counter["ns"]) is None, "counter reset is not negative")
        finally:
            m.time = time

        # The scan is keyed by canonical bus and covers every card in one walk.
        print("fdinfo scan maps cards by bus:")
        real_ns, real_canon = m._fdinfo_engine_ns, m._canon_bus
        try:
            seen = []

            def fake_scan(cards):
                seen.append(len(cards))
                return {"79:00.0": 4242}

            m._fdinfo_engine_ns = fake_scan
            amd = m.read_amd_sysfs()
            # The R9700 has gpu_busy_percent (87) and must keep it; the radeon card has
            # no such attribute, so it is the one card that needs the scan.
            by_bus = {g["bus"]: g["util"] for g in amd}
            check(seen == [1], "exactly one scan, covering only the card that needs it (%s)"
                  % seen)
            check(by_bus.get("0000:03:00.0") == 87,
                  "gpu_busy_percent still wins where present")
            check(by_bus.get("0000:05:00.0") is None,
                  "first fallback sample has no interval yet -> n/a")
        finally:
            m._fdinfo_engine_ns, m._canon_bus = real_ns, real_canon

        # supersensor and stress.py must agree on which index is which card, or every
        # label in a stress log names the wrong GPU. stress.py reads the real /sys, so
        # point supersensor back at the real /sys too.
        print("stress.py index agreement:")
        try:
            stress = load_stress_reader()
            real_temps = stress()
            m.glob = globmod
            real_gpus = {g["index"]: g for g in m.read_gpus()}
            # stress.py reports whatever it can read a temperature for -- NVIDIA cards
            # too whenever nvidia-smi works -- so compare the AMD subset, which is the
            # numbering that can actually diverge. Asserting on every sampled card would
            # only hold on the host this was written on.
            amd_idx = [i for i, g in real_gpus.items() if g["vendor"] == "amd"]
            amd_sampled = [i for i in real_temps if i in amd_idx]
            if not amd_idx:
                print("  skip  no AMD cards on this host")
            else:
                check(bool(amd_sampled), "stress.py sampled the AMD card(s) %s" % amd_idx)
                check(all(i in real_gpus for i in real_temps),
                      "every stress.py index %s exists in supersensor's %s"
                      % (sorted(real_temps), sorted(real_gpus)))
                check(all(abs(real_temps[i][0] - real_gpus[i]["temp"]) < 1.5
                          for i in amd_sampled if real_gpus[i]["temp"] is not None),
                      "stress.py reports the same AMD card's temperature")
        except Exception as err:
            check(False, "stress.py reader raised: %r" % err)

        print("_pci_display_devices is enumerated once, not twice:")
        real_disp2 = m._pci_display_devices
        calls = []
        try:
            def counting(*a, **k):
                calls.append(1)
                return real_disp2(*a, **k)
            m._pci_display_devices = counting
            m.read_amd_sysfs()
            check(len(calls) == 1, "one enumeration per read_amd_sysfs, got %d" % len(calls))
        finally:
            m._pci_display_devices = real_disp2

        print("pci.ids subsystem lookup is reachable (regression: it was dead code):")
        # Vendor lines sit at column 0, so a tab-prefixed match never fired; and a
        # comment line at column 0 cleared in_v, hiding every device below it.
        board = m._pci_name(0x1002, 0x7550, 0x1da2, 0xe490)
        check(board is not None and "Sapphire" in board,
              "board name resolves, not the chip name (%r)" % board)
        check(m._pci_name(0x1002, 0x7550, 0x1849, 0x5403) != board,
              "a different subsystem yields a different name")
        check("Navi 48" in (m._pci_name(0x1002, 0x7551, 0x1da2, 0xe490) or ""),
              "an unknown subsystem still falls back to the chip name")
        # 7550 sits below a mid-vendor comment block; if comments cleared in_v it would
        # be missed. Assert the deep entry that the comment bug used to hide.
        deep = m._pci_name(0x1002, 0x7550, 0x1458, 0x2437)
        check(deep is not None and "9070" in deep, "an entry below a comment resolves (%r)" % deep)

        print("vendor tag fits the width budget:")
        strip_ansi = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
        gg = {"index": 0, "name": "Navi 48 XTX [Sapphire Pulse Radeon RX 9070 XT]",
              "vendor": "amd", "util": 3.0, "mem_used": 100.0, "mem_total": 200.0,
              "temp": 42.0, "mem_temp": 42.0, "hotspot": 46.0, "power": None,
              "power_max": None, "fan": 896, "clock": 41.0, "fan_unit": "rpm"}
        cpu_stub = {"usage": 1.0, "cores": [], "temp": 50.0, "power": 59.0,
                    "cores_power": None, "mhz": None}

        def frame_for(gpu, w):
            """build_frame gained a `mem` argument after this test was written; accept
            either arity so it runs on both sides of that change."""
            try:
                return m.build_frame([gpu], cpu_stub, {}, {}, 1.0, w)
            except TypeError:
                return m.build_frame([gpu], cpu_stub, {}, 1.0, w)

        for w in (40, 60, 80):
            line = strip_ansi([l for l in frame_for(gg, w) if "GPU 0" in strip_ansi(l)][0])
            check(len(line) <= w, "width %d: header is %d chars" % (w, len(line)))
            check("[amd]" in line, "width %d: the vendor tag is still shown" % w)
        # A card with no vendor must not have its name shortened needlessly.
        gg2 = dict(gg, vendor=None)
        line = strip_ansi([l for l in frame_for(gg2, 80) if "GPU 0" in strip_ansi(l)][0])
        check(line.endswith("]") and "[" not in line.split("GPU 0")[1][:2],
              "no tag, no wasted budget (%r)" % line[-20:])

        print("stress.py reserves only cards supersensor actually lists:")
        # A vfio-bound NVIDIA card and a driverless one are excluded by supersensor, so
        # reserving a slot for them would shift every AMD index by two.
        stress = load_stress_reader()
        real_disp = stress.__globals__["_display_devices"]
        real_vend, real_drv = stress.__globals__["_vendor"], stress.__globals__["_driver"]
        real_glob_stress = globmod.glob
        fake = {"0000:01:00.0": ("0x10de", "nvidia"),      # listed
                "0000:02:00.0": ("0x10de", "vfio-pci"),    # excluded
                "0000:03:00.0": ("0x10de", ""),            # excluded (no driver)
                "0000:79:00.0": ("0x1002", "amdgpu")}
        try:
            ns = stress.__globals__
            ns["_display_devices"] = lambda: sorted(fake)
            ns["_vendor"] = lambda d: fake[d][0]
            ns["_driver"] = lambda d: fake[d][1]
            # nvidia-smi dead, every amdgpu hwmon unreadable -> indices still reserved
            ns["subprocess"].run = lambda *a, **k: (_ for _ in ()).throw(OSError())
            ns["glob"].glob = lambda p: [] if "hwmon" in p else real_glob_stress(p)
            temps = stress()
            check(list(temps) == [1], "one nvidia card listed -> AMD is index 1, got %s"
                  % sorted(temps))
        finally:
            ns["_display_devices"], ns["_vendor"], ns["_driver"] = real_disp, real_vend, real_drv
            ns["glob"].glob = real_glob_stress

        print("\n%d check(s) failed" % len(fails))
        return 1 if fails else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())