#!/usr/bin/env python3
"""GEMM application for the ZCU104: correctness test, then latency and power, then result files.

Usage (hwflow runs it like this; it also works by hand):
  python3 run_gemm.py --bit /path/gemm.bit --out results/ [--sizes 64,128,256]

Steps
  1. load the overlay (gemm.bit + gemm.hwh in the same folder), check the PL clock
  2. functional test: random A, B for several shapes, C compared with numpy
  3. per benchmark size
       a. idle power with the IP stopped
       b. latency: back-to-back runs, ap_start -> ap_done timed on the PS (no power sampling)
       c. power: back-to-back runs again while a thread samples the INA226 rails
       d. output of the last run checked again
  4. write <out>/result.csv, <out>/raw/, and <out>/meta.json last

Latency and power are measured in separate phases so the sampling thread cannot
delay the ap_done polling. energy_mj = latency_ms * power_w (one run).
"""

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

AP_START, AP_DONE = 0x1, 0x2


# ---------------------------------------------------------------- arguments
def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bit", required=True, help="gemm.bit (gemm.hwh must be next to it)")
    p.add_argument("--out", required=True, help="results folder (result.csv, meta.json, raw/)")
    p.add_argument("--build-id", default="", help="copied to meta.json so the server can match it")
    p.add_argument("--bit-md5", default="", help="expected md5 of --bit; mismatch stops the run")
    p.add_argument("--clock-mhz", type=float, default=None, help="expected FCLK0; a warning if it differs")
    p.add_argument("--ip", default="gemm_0", help="IP instance name in the block design")
    p.add_argument("--sizes", default="64,128,256",
                   help="benchmark shapes: 128 means M=N=K=128, or MxNxK like 64x256x128")
    p.add_argument("--rails", default="INT,1V2",
                   help="INA226 rails summed into power_w (INT = PL+PS logic, 1V2 = DDR4)")
    p.add_argument("--board-rail", default="12V",
                   help="board input rail, reported separately as board_w; '' to skip. "
                        "It already contains the other rails, so it is never added to power_w")
    p.add_argument("--min-latency-s", type=float, default=1.0, help="latency phase length per size")
    p.add_argument("--min-power-s", type=float, default=3.0, help="power phase length per size")
    p.add_argument("--idle-s", type=float, default=2.0, help="idle power measurement per size")
    p.add_argument("--max-repeat", type=int, default=10000)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--sample-ms", type=float, default=5.0, help="power sampling interval")
    p.add_argument("--run-timeout-s", type=float, default=30.0, help="one kernel run longer than this = hang")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args()


def parse_size(text):
    parts = [int(x) for x in text.lower().split("x")]
    if len(parts) == 1:
        return parts[0], parts[0], parts[0]
    if len(parts) == 3:
        return tuple(parts)
    raise ValueError(f"size '{text}': use 128 or MxNxK")


def md5sum(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- the IP
class Gemm:
    """Register access for gemm(A, B, C, M, N, K); offsets come from the hwh, not hard-coded."""

    MAX_N = 1024  # GEMM_MAX_N in gemm.h

    def __init__(self, overlay, name):
        if name not in overlay.ip_dict:
            raise RuntimeError(f"IP '{name}' not in the overlay; found {sorted(overlay.ip_dict)}")
        self.ip = getattr(overlay, name)
        regs = {k.upper(): v["address_offset"] for k, v in overlay.ip_dict[name]["registers"].items()}
        self.ctrl = regs.get("CTRL", 0x00)
        self.off = {}
        for arg in ("A", "B", "C"):           # 64-bit pointer: <name>_1 low word, <name>_2 high word
            self.off[arg] = (regs[f"{arg}_1"], regs[f"{arg}_2"])
        for arg in ("M", "N", "K"):
            self.off[arg] = regs[arg]

    def setup(self, a, b, c, m, n, k):
        if n > self.MAX_N:
            raise ValueError(f"N={n} > GEMM_MAX_N={self.MAX_N}")
        for arg, buf in (("A", a), ("B", b), ("C", c)):
            lo, hi = self.off[arg]
            self.ip.write(lo, buf.physical_address & 0xFFFFFFFF)
            self.ip.write(hi, buf.physical_address >> 32)
        for arg, value in (("M", m), ("N", n), ("K", k)):
            self.ip.write(self.off[arg], value)

    def run(self, timeout_s):
        """Start once and wait for ap_done; returns the latency in ns (PS clock)."""
        ip, ctrl = self.ip, self.ctrl
        ip.read(ctrl)                        # clear a stale ap_done (clear-on-read)
        t0 = time.perf_counter_ns()
        ip.write(ctrl, AP_START)
        while not (ip.read(ctrl) & AP_DONE):
            if time.perf_counter_ns() - t0 > timeout_s * 1e9:
                raise TimeoutError(f"no ap_done after {timeout_s} s (CTRL=0x{ip.read(ctrl):x})")
        return time.perf_counter_ns() - t0


class Buffers:
    """A, B, C as flat float32 buffers in CMA memory, filled with random data in [-1, 1)."""

    def __init__(self, allocate, m, n, k, rng):
        self.m, self.n, self.k = m, n, k
        self.a = allocate((m * k,), dtype=np.float32)
        self.b = allocate((k * n,), dtype=np.float32)
        self.c = allocate((m * n,), dtype=np.float32)
        self.a[:] = rng.uniform(-1, 1, m * k).astype(np.float32)
        self.b[:] = rng.uniform(-1, 1, k * n).astype(np.float32)
        self.c[:] = np.nan                   # a kernel that writes nothing cannot pass
        for buf in (self.a, self.b, self.c):
            buf.flush()

    def check(self):
        """Compare C with numpy (float64 reference); returns (pass, max_abs_err)."""
        self.c.invalidate()
        ref = (np.asarray(self.a, np.float64).reshape(self.m, self.k)
               @ np.asarray(self.b, np.float64).reshape(self.k, self.n))
        out = np.asarray(self.c, np.float64).reshape(self.m, self.n)
        err = np.abs(out - ref)
        tol = 1e-4 * self.k + 1e-5 * np.abs(ref)   # float32 accumulation over K terms
        ok = bool(np.all(np.isfinite(out)) and np.all(err <= tol))
        return ok, float(np.nanmax(err)) if err.size else 0.0

    def free(self):
        for buf in (self.a, self.b, self.c):
            buf.freebuffer()


# ---------------------------------------------------------------- power
class PowerSampler:
    """Background thread summing the chosen INA226 rails every sample_ms while active."""

    def __init__(self, rails, names, board_rail, sample_ms):
        wanted = names + ([board_rail] if board_rail and board_rail not in names else [])
        missing = [r for r in wanted if r not in rails]
        if missing:
            raise RuntimeError(f"rails {missing} not found; available: {sorted(rails)}")
        self.summed = list(names)
        self.board_rail = board_rail or None
        self.sensors = {r: rails[r].power for r in wanted}
        self.interval = sample_ms / 1000.0
        self.samples = []                      # (t_s, {rail: W})
        self._stop = threading.Event()
        self._thread = None

    def read(self):
        return {r: float(s.value) for r, s in self.sensors.items()}

    def _loop(self):
        t0 = time.perf_counter()
        while not self._stop.is_set():
            self.samples.append((time.perf_counter() - t0, self.read()))
            self._stop.wait(self.interval)

    def start(self):
        self.samples = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()
        return summarize(self.samples, list(self.sensors), self.summed, self.board_rail)


def summarize(samples, names, summed, board_rail):
    if not samples:
        return {"total_w": None, "board_w": None, "per_rail": {r: None for r in names}, "n": 0}
    per_rail = {r: statistics.fmean(s[r] for _, s in samples) for r in names}
    return {"total_w": sum(per_rail[r] for r in summed),
            "board_w": per_rail[board_rail] if board_rail else None,
            "per_rail": per_rail, "n": len(samples)}


def measure_idle(sampler, seconds):
    sampler.start()
    time.sleep(seconds)
    return sampler.stop()


# ---------------------------------------------------------------- phases
def functional_test(gemm, allocate, rng, timeout_s):
    shapes = [(1, 1, 1), (16, 16, 16), (37, 19, 23), (64, 128, 32), (128, 64, 256)]
    rows = []
    for m, n, k in shapes:
        bufs = Buffers(allocate, m, n, k, rng)
        try:
            gemm.setup(bufs.a, bufs.b, bufs.c, m, n, k)
            lat_ns = gemm.run(timeout_s)
            ok, err = bufs.check()
        except TimeoutError as exc:
            ok, err, lat_ns = False, float("nan"), None
            print(f"  {exc}")
        finally:
            bufs.free()
        rows.append({"case": f"func_{m}x{n}x{k}", "M": m, "N": n, "K": k,
                     "latency_ms": lat_ns / 1e6 if lat_ns else None, "max_abs_err": err, "pass": ok})
        print(f"  functional {m}x{n}x{k}: max_abs_err={err:.3e} -> {'PASS' if ok else 'FAIL'}")
        if lat_ns is None:
            break                            # the IP never finished: later runs cannot start either
    return rows


def run_repeated(gemm, min_s, max_repeat, timeout_s):
    latencies = []
    t_end = time.perf_counter() + min_s
    while len(latencies) < max_repeat and (time.perf_counter() < t_end or len(latencies) < 3):
        latencies.append(gemm.run(timeout_s) / 1e6)
    return latencies


def benchmark(gemm, allocate, rng, sampler, size, args, raw_dir):
    m, n, k = size
    case = f"gemm_{m}x{n}x{k}"
    bufs = Buffers(allocate, m, n, k, rng)
    try:
        gemm.setup(bufs.a, bufs.b, bufs.c, m, n, k)
        idle = measure_idle(sampler, args.idle_s)
        for _ in range(args.warmup):
            gemm.run(args.run_timeout_s)

        latencies = run_repeated(gemm, args.min_latency_s, args.max_repeat, args.run_timeout_s)

        sampler.start()
        power_runs = run_repeated(gemm, args.min_power_s, args.max_repeat, args.run_timeout_s)
        busy = sampler.stop()
        power_samples = list(sampler.samples)
        ok, err = bufs.check()
    finally:
        bufs.free()

    with open(raw_dir / f"{case}_latency.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["phase", "run", "latency_ms"])
        w.writerows(("latency", i, f"{v:.6f}") for i, v in enumerate(latencies))
        w.writerows(("power", i, f"{v:.6f}") for i, v in enumerate(power_runs))
    with open(raw_dir / f"{case}_power.csv", "w", newline="") as f:
        names = list(sampler.sensors)
        w = csv.writer(f)
        w.writerow(["t_s"] + [f"{r}_w" for r in names])
        w.writerows([f"{t:.4f}"] + [f"{s[r]:.4f}" for r in names] for t, s in power_samples)

    lat = statistics.fmean(latencies)
    power_w, idle_w = busy["total_w"], idle["total_w"]
    flops = 2.0 * m * n * k
    row = {"case": case, "M": m, "N": n, "K": k,
           "latency_ms": round(lat, 6),
           "latency_ms_std": round(statistics.pstdev(latencies), 6),
           "latency_ms_min": round(min(latencies), 6),
           "latency_ms_median": round(statistics.median(latencies), 6),
           "repeat": len(latencies),
           "power_w": round(power_w, 4) if power_w is not None else None,
           "idle_w": round(idle_w, 4) if idle_w is not None else None,
           "power_samples": busy["n"],
           "gflops": round(flops / (lat * 1e6), 4),
           "pass": ok, "max_abs_err": err}
    if power_w is not None:
        row["energy_mj"] = round(lat * power_w, 6)                 # ms * W = mJ
        row["dyn_energy_mj"] = round(lat * (power_w - idle_w), 6)
        row["gflops_per_w"] = round(row["gflops"] / power_w, 4)
    if busy["board_w"] is not None:
        row["board_w"] = round(busy["board_w"], 4)
        row["idle_board_w"] = round(idle["board_w"], 4)
        row["board_energy_mj"] = round(lat * busy["board_w"], 6)
        row["board_dyn_energy_mj"] = round(lat * (busy["board_w"] - idle["board_w"]), 6)
    for r in sampler.sensors:
        row[f"power_{r}_w"] = round(busy["per_rail"][r], 4)
        row[f"idle_{r}_w"] = round(idle["per_rail"][r], 4)
    print(f"  {case}: {lat:.3f} ms (+-{row['latency_ms_std']:.3f}, n={len(latencies)}), "
          f"{power_w:.3f} W (idle {idle_w:.3f} W), {row.get('energy_mj')} mJ/run, "
          f"{row['gflops']} GFLOP/s, {'PASS' if ok else 'FAIL'}")
    return row


# ---------------------------------------------------------------- output
def write_csv(path, rows):
    fields = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_meta(out, meta):
    """meta.json is written last and atomically: the server treats it as 'run finished'."""
    tmp = out / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, out / "meta.json")


def main():
    args = parse_args()
    print(f"{Path(__file__).resolve()} (python {sys.version.split()[0]})")
    out = Path(args.out)
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").unlink(missing_ok=True)
    meta = {"status": "failed", "build_id": args.build_id, "bit": args.bit, "ip": args.ip,
            "rails": args.rails.split(","), "board_rail": args.board_rail, "start": time.strftime("%Y-%m-%dT%H:%M:%S"), "warnings": []}
    try:
        status = run(args, out, raw_dir, meta)
    except Exception as exc:              # still leave a meta.json so the server sees why
        meta["error"] = f"{type(exc).__name__}: {exc}"
        meta["traceback"] = traceback.format_exc()
        print(meta["traceback"], end="")
        print(f"ERROR {meta['error']}")
        status = 2
    meta["end"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_meta(out, meta)
    print(f"results: {out} (status {meta['status']})")
    return status


def run(args, out, raw_dir, meta):
    from pynq import Clocks, Overlay, allocate, get_rails

    bit = Path(args.bit)
    meta["bit_md5"] = md5sum(bit)
    if args.bit_md5 and args.bit_md5 != meta["bit_md5"]:
        raise RuntimeError(f"md5 of {bit} is {meta['bit_md5']}, expected {args.bit_md5}")

    print(f"loading {bit}")
    overlay = Overlay(str(bit))              # also sets FCLK0 from the hwh
    meta["clock_mhz"] = float(Clocks.fclk0_mhz)
    if args.clock_mhz and abs(meta["clock_mhz"] - args.clock_mhz) > 0.01 * args.clock_mhz:
        meta["warnings"].append(f"FCLK0 is {meta['clock_mhz']} MHz, build expects {args.clock_mhz} MHz")
    print(f"  FCLK0 = {meta['clock_mhz']} MHz")
    gemm = Gemm(overlay, args.ip)
    rng = np.random.default_rng(args.seed)

    print("functional test")
    func_rows = functional_test(gemm, allocate, rng, args.run_timeout_s)
    write_csv(raw_dir / "functional.csv", func_rows)
    meta["functional"] = {"pass": all(r["pass"] for r in func_rows),
                          "cases": len(func_rows), "failed": [r["case"] for r in func_rows if not r["pass"]]}
    if not meta["functional"]["pass"]:
        # result.csv still gets rows so the server can show what failed; no benchmark on a wrong IP
        write_csv(out / "result.csv", [{**r, "power_w": None} for r in func_rows])
        return 1

    sampler = PowerSampler(get_rails(), args.rails.split(","), args.board_rail, args.sample_ms)
    rows = []
    for text in args.sizes.split(","):
        size = parse_size(text.strip())
        print(f"benchmark {size[0]}x{size[1]}x{size[2]}")
        rows.append(benchmark(gemm, allocate, rng, sampler, size, args, raw_dir))
    write_csv(out / "result.csv", rows)
    meta["sizes"] = args.sizes
    meta["status"] = "done" if all(r["pass"] for r in rows) else "failed"
    return 0 if meta["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
