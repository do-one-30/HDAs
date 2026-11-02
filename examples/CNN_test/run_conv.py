#!/usr/bin/env python3
"""conv2d application for the ZCU104: correctness test, then latency and power per layer, then result files.

Usage (hwflow runs it like this; it also works by hand):
  python3 run_conv.py --bit /path/conv2d.bit --out results/ [--layers conv1,res2,res3,res4]

Steps
  1. load the overlay (conv2d.bit + conv2d.hwh in the same folder), check the PL clock
  2. functional test: random input/weights/bias for several small layer shapes, compared with numpy
  3. per benchmark layer
       a. idle power with the IP stopped
       b. latency: back-to-back runs, ap_start -> ap_done timed on the PS (no power sampling)
       c. power: back-to-back runs again while a thread samples the INA226 rails
       d. output of the last run checked again
  4. write <out>/result.csv, <out>/raw/, and <out>/meta.json last

A layer is C_in,C_out,H,W,K,stride,pad,relu (NCHW, batch 1, zero padding).
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

# ResNet-18 style layers (input 224x224); --layers takes these names or C_in,C_out,H,W,K,stride,pad,relu
PRESETS = {
    "conv1": (3, 64, 224, 224, 7, 2, 3, 1),
    "res2": (64, 64, 56, 56, 3, 1, 1, 1),
    "res3": (128, 128, 28, 28, 3, 1, 1, 1),
    "res4": (256, 256, 14, 14, 3, 1, 1, 1),
    "res5": (512, 512, 7, 7, 3, 1, 1, 1),
}
FIELDS = ("C_in", "C_out", "H", "W", "K", "stride", "pad", "relu")


# ---------------------------------------------------------------- arguments
def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bit", required=True, help="conv2d.bit (conv2d.hwh must be next to it)")
    p.add_argument("--out", required=True, help="results folder (result.csv, meta.json, raw/)")
    p.add_argument("--build-id", default="", help="copied to meta.json so the server can match it")
    p.add_argument("--bit-md5", default="", help="expected md5 of --bit; mismatch stops the run")
    p.add_argument("--clock-mhz", type=float, default=None, help="expected FCLK0; a warning if it differs")
    p.add_argument("--ip", default="conv2d_0", help="IP instance name in the block design")
    p.add_argument("--layers", default="conv1,res2,res3,res4",
                   help=f"benchmark layers separated by ';' or ',': names {sorted(PRESETS)} "
                        "or C_in,C_out,H,W,K,stride,pad,relu groups joined by ';'")
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


def parse_layers(text):
    """'conv1,res2' or '64,64,56,56,3,1,1,1;3,64,224,224,7,2,3,1' -> list of (name, layer tuple)."""
    layers = []
    for group in text.split(";"):
        parts = [x.strip() for x in group.split(",") if x.strip()]
        if parts and all(x in PRESETS for x in parts):
            layers += [(x, PRESETS[x]) for x in parts]
        elif len(parts) == len(FIELDS) and all(x.lstrip("-").isdigit() for x in parts):
            layer = tuple(int(x) for x in parts)
            layers.append(("c{}_{}_{}x{}_k{}s{}p{}".format(*layer[:7]), layer))
        else:
            raise ValueError(f"layer '{group}': use names {sorted(PRESETS)} or 8 numbers {FIELDS}")
    return layers


def out_size(layer):
    c_in, c_out, h, w, k, s, p, _ = layer
    return (h + 2 * p - k) // s + 1, (w + 2 * p - k) // s + 1


def md5sum(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- the IP
class Conv2d:
    """Register access for conv2d(in, wgt, bias, out, C_in, ..., relu); offsets come from the hwh."""

    MAX_W = 224                 # CONV_MAX_W in conv2d.h
    MAX_WGT = 512 * 3 * 3       # CONV_MAX_WGT in conv2d.h
    POINTERS = ("IN", "WGT", "BIAS", "OUT")

    def __init__(self, overlay, name):
        if name not in overlay.ip_dict:
            raise RuntimeError(f"IP '{name}' not in the overlay; found {sorted(overlay.ip_dict)}")
        self.ip = getattr(overlay, name)
        regs = {k.upper(): v["address_offset"] for k, v in overlay.ip_dict[name]["registers"].items()}
        self.ctrl = regs.get("CTRL", 0x00)
        self.off = {}
        for arg in self.POINTERS:              # 64-bit pointer: <name>_1 low word, <name>_2 high word
            base = self._name(regs, arg, "_1")
            self.off[arg] = (regs[base + "_1"], regs[base + "_2"])
        for arg in FIELDS:
            self.off[arg] = regs[self._name(regs, arg.upper(), "")]

    @staticmethod
    def _name(regs, arg, suffix):
        """HLS renames ports that are HDL keywords (in, out, ...) to <name>_r: accept both."""
        for name in (arg, arg + "_R"):
            if name + suffix in regs:
                return name
        raise RuntimeError(f"register for '{arg}' not in the hwh; registers: {sorted(regs)}")

    def setup(self, bufs, layer):
        c_in, c_out, h, w, k, s, p, relu = layer
        h_out, w_out = out_size(layer)
        if w > self.MAX_W or w_out > self.MAX_W or c_in * k * k > self.MAX_WGT:
            raise ValueError(f"layer {layer} exceeds the on-chip buffers (W<={self.MAX_W}, "
                             f"C_in*K*K<={self.MAX_WGT})")
        if h_out < 1 or w_out < 1:
            raise ValueError(f"layer {layer} has no output")
        for arg, buf in zip(self.POINTERS, (bufs.x, bufs.w, bufs.b, bufs.y)):
            lo, hi = self.off[arg]
            self.ip.write(lo, buf.physical_address & 0xFFFFFFFF)
            self.ip.write(hi, buf.physical_address >> 32)
        for arg, value in zip(FIELDS, layer):
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
    """in, wgt, bias, out as flat float32 CMA buffers, random data in [-1, 1)."""

    def __init__(self, allocate, layer, rng):
        self.layer = layer
        c_in, c_out, h, w, k = layer[:5]
        h_out, w_out = out_size(layer)
        self.x = allocate((c_in * h * w,), dtype=np.float32)
        self.w = allocate((c_out * c_in * k * k,), dtype=np.float32)
        self.b = allocate((c_out,), dtype=np.float32)
        self.y = allocate((c_out * h_out * w_out,), dtype=np.float32)
        for buf in (self.x, self.w, self.b):
            buf[:] = rng.uniform(-1, 1, buf.size).astype(np.float32)
        self.y[:] = np.nan                   # a kernel that writes nothing cannot pass
        for buf in (self.x, self.w, self.b, self.y):
            buf.flush()

    def reference(self):
        """Same convolution in float64 numpy (zero padding, optional ReLU)."""
        c_in, c_out, h, w, k, s, p, relu = self.layer
        h_out, w_out = out_size(self.layer)
        x = np.pad(np.asarray(self.x, np.float64).reshape(c_in, h, w), ((0, 0), (p, p), (p, p)))
        wt = np.asarray(self.w, np.float64).reshape(c_out, c_in, k, k)
        y = np.zeros((c_out, h_out, w_out)) + np.asarray(self.b, np.float64)[:, None, None]
        for kh in range(k):                  # one shifted, strided slice per kernel tap
            for kw in range(k):
                patch = x[:, kh:kh + s * (h_out - 1) + 1:s, kw:kw + s * (w_out - 1) + 1:s]
                y += np.tensordot(wt[:, :, kh, kw], patch, axes=(1, 0))
        return np.maximum(y, 0) if relu else y

    def check(self):
        """Compare out with numpy; returns (pass, max_abs_err)."""
        self.y.invalidate()
        c_in, k = self.layer[0], self.layer[4]
        ref = self.reference()
        out = np.asarray(self.y, np.float64).reshape(ref.shape)
        err = np.abs(out - ref)
        tol = 2e-6 * c_in * k * k + 1e-5 * np.abs(ref)   # float32 accumulation over C_in*K*K terms
        ok = bool(np.all(np.isfinite(out)) and np.all(err <= tol))
        return ok, float(np.nanmax(err)) if err.size else 0.0

    def free(self):
        for buf in (self.x, self.w, self.b, self.y):
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
def layer_cols(layer):
    h_out, w_out = out_size(layer)
    return {**dict(zip(FIELDS, layer)), "H_out": h_out, "W_out": w_out}


def functional_test(conv, allocate, rng, timeout_s):
    shapes = [(1, 1, 5, 5, 1, 1, 0, 0), (4, 3, 8, 8, 3, 1, 1, 0), (3, 4, 9, 11, 3, 2, 1, 1),
              (2, 2, 13, 7, 5, 2, 2, 1), (3, 8, 16, 16, 7, 2, 3, 1), (16, 16, 28, 28, 3, 1, 1, 1)]
    rows = []
    for layer in shapes:
        name = "func_c{}_{}_{}x{}_k{}s{}p{}r{}".format(*layer)
        bufs = Buffers(allocate, layer, rng)
        try:
            conv.setup(bufs, layer)
            lat_ns = conv.run(timeout_s)
            ok, err = bufs.check()
        except TimeoutError as exc:
            ok, err, lat_ns = False, float("nan"), None
            print(f"  {exc}")
        finally:
            bufs.free()
        rows.append({"case": name, **layer_cols(layer),
                     "latency_ms": lat_ns / 1e6 if lat_ns else None, "max_abs_err": err, "pass": ok})
        print(f"  {name}: max_abs_err={err:.3e} -> {'PASS' if ok else 'FAIL'}")
        if lat_ns is None:
            break                            # the IP never finished: later runs cannot start either
    return rows


def run_repeated(conv, min_s, max_repeat, timeout_s):
    latencies = []
    t_end = time.perf_counter() + min_s
    while len(latencies) < max_repeat and (time.perf_counter() < t_end or len(latencies) < 3):
        latencies.append(conv.run(timeout_s) / 1e6)
    return latencies


def benchmark(conv, allocate, rng, sampler, name, layer, args, raw_dir):
    case = f"conv_{name}"
    bufs = Buffers(allocate, layer, rng)
    try:
        conv.setup(bufs, layer)
        idle = measure_idle(sampler, args.idle_s)
        for _ in range(args.warmup):
            conv.run(args.run_timeout_s)

        latencies = run_repeated(conv, args.min_latency_s, args.max_repeat, args.run_timeout_s)

        sampler.start()
        power_runs = run_repeated(conv, args.min_power_s, args.max_repeat, args.run_timeout_s)
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

    c_in, c_out, k = layer[0], layer[1], layer[4]
    h_out, w_out = out_size(layer)
    macs = c_out * h_out * w_out * c_in * k * k   # padded taps counted too (standard convention)
    lat = statistics.fmean(latencies)
    power_w, idle_w = busy["total_w"], idle["total_w"]
    row = {"case": case, **layer_cols(layer),
           "latency_ms": round(lat, 6),
           "latency_ms_std": round(statistics.pstdev(latencies), 6),
           "latency_ms_min": round(min(latencies), 6),
           "latency_ms_median": round(statistics.median(latencies), 6),
           "repeat": len(latencies),
           "power_w": round(power_w, 4) if power_w is not None else None,
           "idle_w": round(idle_w, 4) if idle_w is not None else None,
           "power_samples": busy["n"],
           "macs": macs,
           "gflops": round(2.0 * macs / (lat * 1e6), 4),
           "macs_per_cycle": round(macs / (lat * 1e-3 * args.clock_hz), 4),
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
          f"{row['gflops']} GFLOP/s ({row['macs_per_cycle']} MAC/cycle), {'PASS' if ok else 'FAIL'}")
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

    layers = parse_layers(args.layers)       # bad --layers fails before the overlay is touched
    bit = Path(args.bit)
    meta["bit_md5"] = md5sum(bit)
    if args.bit_md5 and args.bit_md5 != meta["bit_md5"]:
        raise RuntimeError(f"md5 of {bit} is {meta['bit_md5']}, expected {args.bit_md5}")

    print(f"loading {bit}")
    overlay = Overlay(str(bit))              # also sets FCLK0 from the hwh
    meta["clock_mhz"] = float(Clocks.fclk0_mhz)
    args.clock_hz = meta["clock_mhz"] * 1e6
    if args.clock_mhz and abs(meta["clock_mhz"] - args.clock_mhz) > 0.01 * args.clock_mhz:
        meta["warnings"].append(f"FCLK0 is {meta['clock_mhz']} MHz, build expects {args.clock_mhz} MHz")
    print(f"  FCLK0 = {meta['clock_mhz']} MHz")
    conv = Conv2d(overlay, args.ip)
    rng = np.random.default_rng(args.seed)

    print("functional test")
    func_rows = functional_test(conv, allocate, rng, args.run_timeout_s)
    write_csv(raw_dir / "functional.csv", func_rows)
    meta["functional"] = {"pass": all(r["pass"] for r in func_rows),
                          "cases": len(func_rows), "failed": [r["case"] for r in func_rows if not r["pass"]]}
    if not meta["functional"]["pass"]:
        # result.csv still gets rows so the server can show what failed; no benchmark on a wrong IP
        write_csv(out / "result.csv", [{**r, "power_w": None} for r in func_rows])
        return 1

    sampler = PowerSampler(get_rails(), args.rails.split(","), args.board_rail, args.sample_ms)
    rows = []
    for name, layer in layers:
        h_out, w_out = out_size(layer)
        print(f"benchmark {name}: {layer[0]}->{layer[1]} @{layer[2]}x{layer[3]} K{layer[4]} "
              f"s{layer[5]} p{layer[6]} -> {h_out}x{w_out}")
        rows.append(benchmark(conv, allocate, rng, sampler, name, layer, args, raw_dir))
    write_csv(out / "result.csv", rows)
    meta["layers"] = {name: dict(zip(FIELDS, layer)) for name, layer in layers}
    meta["status"] = "done" if all(r["pass"] for r in rows) else "failed"
    return 0 if meta["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
