"""Copy a build to the ZCU104, run the user's application there over ssh, and bring the results back.

The application is the user's own code on the board. hwflow only tells it where the
overlay is and where to write, then collects this result format:

  <results>/result.csv   one row per measured case
                         required: case, latency_ms, power_w
                         optional: latency_ms_std, repeat, idle_w, energy_mj, pass, any other column
  <results>/meta.json    written last: {"status": "done" | "failed", "build_id": ..., "bit_md5": ...}
  <results>/raw/         optional, kept as is
"""


import csv
import hashlib
import json
import shlex
import subprocess
import threading
from pathlib import Path

REQUIRED_COLUMNS = ("case", "latency_ms", "power_w")


class RemoteError(RuntimeError):
    pass


class Board:
    def __init__(self, cfg):
        b = cfg.board_cfg
        for key in ("host", "work_dir", "command"):
            if not b.get(key):
                raise RemoteError(f"board.{key} is required in {cfg.path.name}")
        # app_files: application files kept on the server, copied to <work_dir>/<build>/app/ on every
        # run and used as the cwd, so the board always runs the version next to build.yaml
        try:
            self.app_files = cfg._files(b.get("app_files"), "board.app_files", required=False)
        except ValueError as exc:
            raise RemoteError(str(exc)) from None
        if not self.app_files and not b.get("app_dir"):
            raise RemoteError(f"{cfg.path.name}: set board.app_files (copied each run) or board.app_dir")
        self.host = b["host"]                       # e.g. xilinx@192.168.0.10 or root@pynq
        # port / identity_file: leave out to use ~/.ssh/config (Host pynq ... Port, IdentityFile)
        self.port = str(b["port"]) if b.get("port") else None
        self.identity_file = str(Path(b["identity_file"]).expanduser()) if b.get("identity_file") else None
        self.work_dir = b["work_dir"].rstrip("/")   # overlay + results of each build go here
        self.app_dir = b.get("app_dir")             # the application's folder on the board (cwd)
        self.command = b["command"]                 # e.g. python3 run_gemm.py --bit {bit} --out {results}
        self.setup = b.get("setup", "")             # e.g. source /etc/profile.d/pynq_venv.sh
        self.sudo = bool(b.get("sudo", not self.host.startswith("root@")))
        self.timeout_s = float(b.get("timeout_s", 6 * 3600))
        common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.identity_file:
            common += ["-i", self.identity_file]
        self.ssh_opts = (["-p", self.port] if self.port else []) + common
        self.scp_opts = (["-P", self.port] if self.port else []) + common

    def ssh(self, command, timeout=None, log_path=None):
        cmd = ["ssh", *self.ssh_opts, self.host, command]
        if log_path is None:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout or 60)
        with open(log_path, "w", encoding="utf-8") as handle:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

            def copy_output():
                for line in proc.stdout:
                    handle.write(line)
                    handle.flush()
                    print("  [board] " + line, end="", flush=True)

            reader = threading.Thread(target=copy_output, daemon=True)
            reader.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                reader.join(5)
                raise RemoteError(f"application did not finish within {timeout:.0f} s; see {log_path}")
            reader.join(5)
            return proc

    def push(self, files, remote_dir):
        cmd = ["scp", "-q", *self.scp_opts, *[str(p) for p in files], f"{self.host}:{remote_dir}/"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise RemoteError(f"scp to board failed: {proc.stderr.strip()}")

    def pull(self, remote_path, local_parent):
        """Copy remote_path (file or folder) into local_parent/."""
        Path(local_parent).mkdir(parents=True, exist_ok=True)
        cmd = ["scp", "-r", "-q", *self.scp_opts, f"{self.host}:{remote_path}", str(local_parent) + "/"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise RemoteError(f"scp from board failed: {proc.stderr.strip()}")


def check_connection(cfg):
    board = Board(cfg)
    target = board.work_dir if board.app_files else board.app_dir
    probe = board.ssh(f"mkdir -p {shlex.quote(board.work_dir)} 2>/dev/null; test -d {shlex.quote(target)} && echo ok")
    if probe.returncode != 0 or "ok" not in probe.stdout:
        raise RemoteError(f"cannot reach {target} on {board.host}: "
                          f"{probe.stderr.strip() or probe.stdout.strip() or 'folder not found'}")
    if board.sudo:
        s = board.ssh("sudo -n true")
        if s.returncode != 0:
            raise RemoteError("sudo needs a password on the board; allow NOPASSWD or connect as root")
    return board


def md5sum(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_on_board(cfg, build_dir, vivado_summary, log=print):
    board = check_connection(cfg)
    build_id = Path(build_dir).name
    remote = f"{board.work_dir}/{build_id}"
    overlay, results = f"{remote}/overlay", f"{remote}/results"
    app_dir = f"{remote}/app" if board.app_files else board.app_dir
    bit = Path(vivado_summary["bit"])
    bit_md5 = md5sum(bit)

    # old results are removed first, so nothing from an earlier run can be collected by mistake
    # with sudo the application runs as root, so last run's results (and folders made from
    # Jupyter, which runs as root) can be root-owned: clear and create them as root, then hand
    # the build folder to the ssh user so scp can write the overlay
    q = shlex.quote
    if board.sudo:
        prep_cmd = (f"sudo -n rm -rf {q(results)} {q(remote + '/app')} && "
                    f"sudo -n mkdir -p {q(overlay)} {q(results)} {q(remote + '/app')} && "
                    f"sudo -n chown -R \"$(id -u):$(id -g)\" {q(remote)}")
    else:
        prep_cmd = (f"rm -rf {q(results)} {q(remote + '/app')} && "
                    f"mkdir -p {q(overlay)} {q(results)} {q(remote + '/app')}")
    prep = board.ssh(prep_cmd)
    if prep.returncode != 0:
        raise RemoteError(f"ssh {board.host} failed: {prep.stderr.strip()}")
    files = [Path(vivado_summary[k]) for k in ("bit", "hwh", "hw_h") if vivado_summary.get(k)]
    log(f"board: copying {', '.join(p.name for p in files)} to {board.host}:{overlay}")
    board.push(files, overlay)
    if board.app_files:
        log(f"board: copying {', '.join(p.name for p in board.app_files)} to {board.host}:{app_dir}")
        board.push(board.app_files, app_dir)

    values = {"bit": f"{overlay}/{bit.name}",
              "hwh": f"{overlay}/{Path(vivado_summary['hwh']).name}" if vivado_summary.get("hwh") else "",
              "hw_h": f"{overlay}/{Path(vivado_summary['hw_h']).name}" if vivado_summary.get("hw_h") else "",
              "overlay": overlay, "results": results, "build_id": build_id, "bit_md5": bit_md5,
              "clock_mhz": f"{cfg.clock_mhz:g}", "name": cfg.name, "top": cfg.top}
    try:
        app_cmd = board.command.format(**values)
    except KeyError as exc:
        raise RemoteError(f"board.command uses unknown placeholder {exc}; "
                          f"available: {', '.join('{' + k + '}' for k in values)}") from None
    env = " ".join(f"HWFLOW_{k.upper()}={shlex.quote(v)}" for k, v in values.items())
    script = f"{board.setup} && {app_cmd}" if board.setup else app_cmd
    run = f"env {env} bash -c {shlex.quote(script)}"
    if board.sudo:
        run = f"sudo -n {run}"
    command = f"cd {shlex.quote(app_dir)} && {run}"

    local = Path(build_dir) / "board"
    local.mkdir(parents=True, exist_ok=True)
    log(f"board: cd {app_dir} && {app_cmd}")
    proc = board.ssh(command, timeout=board.timeout_s, log_path=local / "board_run.log")
    log(f"board: application exited with {proc.returncode}, fetching results")

    local_results = local / "results"
    if local_results.exists():
        import shutil
        shutil.rmtree(local_results)
    try:
        board.pull(results, local)
    except RemoteError as exc:
        raise RemoteError(f"no results came back (exit {proc.returncode}); "
                          f"see {local / 'board_run.log'} ({exc})") from None
    try:
        summary = collect_results(local_results, build_id, bit_md5)
    except RemoteError as exc:
        if proc.returncode != 0:
            raise RemoteError(f"application exited with {proc.returncode}; see {local / 'board_run.log'} "
                              f"({exc})") from None
        raise
    summary.update({"returncode": proc.returncode, "remote_dir": remote, "log": str(local / "board_run.log")})
    if proc.returncode != 0 and summary["status"] == "ok":
        summary["status"] = "failed"
        summary["warnings"].append(f"application exit code {proc.returncode}")
    (local / "board_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for warning in summary["warnings"]:
        log(f"board: WARNING {warning}")
    return summary


def collect_results(results_dir, build_id, bit_md5):
    """Check meta.json and result.csv against the format above; fill energy_mj when missing."""
    results_dir = Path(results_dir)
    meta_path, csv_path = results_dir / "meta.json", results_dir / "result.csv"
    if not meta_path.exists():
        raise RemoteError(f"{meta_path.name} missing in {results_dir}: the application ran but wrote no "
                          f"meta.json (wrong or incomplete application file? check board_run.log)")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RemoteError(f"meta.json is not valid JSON: {exc}") from None
    warnings = []
    if meta.get("bit_md5") and meta["bit_md5"] != bit_md5:
        raise RemoteError(f"meta.json bit_md5 {meta['bit_md5']} does not match the bitstream sent ({bit_md5})")
    if meta.get("build_id") and meta["build_id"] != build_id:
        raise RemoteError(f"meta.json build_id {meta['build_id']} does not match {build_id}")
    if not meta.get("bit_md5") and not meta.get("build_id"):
        warnings.append("meta.json has neither bit_md5 nor build_id; cannot confirm which bitstream was measured")
    status = str(meta.get("status", "")).lower()
    if status not in ("done", "failed"):
        raise RemoteError(f"meta.json status is '{meta.get('status')}', expected 'done' or 'failed'")

    if not csv_path.exists():
        if meta.get("error"):
            raise RemoteError(f"application error: {meta['error']}")
        raise RemoteError(f"result.csv missing in {results_dir}")
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    missing = [c for c in REQUIRED_COLUMNS if rows and c not in rows[0]]
    if not rows:
        raise RemoteError("result.csv has no rows")
    if missing:
        raise RemoteError(f"result.csv lacks required column(s) {missing}")
    for row in rows:
        lat, pwr = _num(row.get("latency_ms")), _num(row.get("power_w"))
        if _num(row.get("energy_mj")) is None and lat is not None and pwr is not None:
            row["energy_mj"] = f"{lat * pwr:.6g}"           # ms * W = mJ
        idle = _num(row.get("idle_w"))
        if _num(row.get("dyn_energy_mj")) is None and None not in (lat, pwr, idle):
            row["dyn_energy_mj"] = f"{lat * (pwr - idle):.6g}"
    return {"status": "ok" if status == "done" else "failed", "meta": meta, "rows": rows,
            "result_csv": str(csv_path), "warnings": warnings}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def merge_report(cfg, build_dir, hls_summary, vivado_summary, board_summary):
    """One CSV row per measured case, with the build's HLS/Vivado numbers alongside."""
    build_cols = {
        "build_id": Path(build_dir).name,
        "clock_mhz_target": cfg.clock_mhz,
        "hls_est_clock_ns": (hls_summary.get("csynth") or {}).get("estimated_clock_ns"),
        "hls_latency_cycles_best": (hls_summary.get("csynth") or {}).get("latency_cycles_best"),
        "hls_latency_cycles_worst": (hls_summary.get("csynth") or {}).get("latency_cycles_worst"),
        "wns_ns": vivado_summary.get("wns_ns"),
        "whs_ns": vivado_summary.get("whs_ns"),
        "vivado_power_total_w": (vivado_summary.get("vivado_power") or {}).get("total_onchip_w"),
    }
    for name, item in (vivado_summary.get("utilization") or {}).items():
        build_cols[f"{name}_used"] = item["used"]
        build_cols[f"{name}_pct"] = item["util_pct"]
    rows = board_summary["rows"]
    fields = list(build_cols)
    for row in rows:
        fields += [k for k in row if k not in fields]
    out = Path(build_dir) / "report.csv"
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**build_cols, **row})
    (Path(build_dir) / "report.json").write_text(json.dumps(
        {"build": build_cols, "hls": hls_summary, "vivado": vivado_summary, "board": board_summary},
        indent=2), encoding="utf-8")
    return out
