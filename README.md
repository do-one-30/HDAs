# hwflow: HLS → Vivado → ZCU104 measurement automation (server side)

One `build.yaml` per design drives these stages in order:

1. **HLS**: generate a Tcl script from `build.yaml` and run csim → csynth → (cosim) → export_design with `vitis_hls`.
2. **Vivado**: build the bitstream from an automatic PS + HLS IP block design, or from your own `bd_tcl`. Timing (WNS/WHS) is checked, and utilization and power reports are kept.
3. **Board**: copy bit, hwh and hw.h to the board over ssh/scp, run your board application, and fetch its results (`result.csv`, `meta.json`).
4. **Report**: merge the HLS and Vivado numbers with the board measurements into `report.csv`.

## Requirements

- Server: Python 3.6+, PyYAML, and `vitis_hls` and `vivado` on PATH (`source <Xilinx>/settings64.sh`).
- Board: an application that measures latency and power (see below), and passwordless ssh from the server (`ssh-copy-id`, or `board.identity_file` in build.yaml).
- Example design folder:
  ```
  gemm/
  ├── build.yaml
  ├── src/gemm.cc, gemm.h
  ├── tb/gemm_tb.cc
  └── app/run_gemm.py
  ```

## Commands

Run `run_hwflow.py` by its path; no install or PYTHONPATH is needed. The design folder can be anywhere, just pass the path of its build.yaml.

```bash
python3 ~/tools/hwflow_pkg/run_hwflow.py all ~/designs/gemm/build.yaml
```

`python3 -m hwflow ...` also works when hwflow_pkg is on PYTHONPATH. In the commands below, `python3 -m hwflow` and `python3 <path>/run_hwflow.py` are interchangeable.

```bash
python3 -m hwflow tcl gemm/build.yaml          # print the generated HLS/Vivado Tcl only
python3 -m hwflow check-board gemm/build.yaml  # check ssh, the application folder and sudo
python3 -m hwflow hls gemm/build.yaml
python3 -m hwflow vivado gemm/build.yaml
python3 -m hwflow board gemm/build.yaml
python3 -m hwflow all gemm/build.yaml          # hls + vivado + board in one go
```

Stages that already succeeded are skipped. Add `--force` (anywhere after `run_hwflow.py`) to run them again.

### Running HLS steps one at a time

`hls` runs all `steps` from build.yaml in a single vitis_hls call. To run the steps separately, use the step name as the command. The `steps` list in build.yaml is ignored in this case.

```bash
python3 -m hwflow csim   gemm/build.yaml   # C simulation (creates the project if it does not exist)
python3 -m hwflow csynth gemm/build.yaml   # synthesis + resource check (hls_max_util_pct)
python3 -m hwflow cosim  gemm/build.yaml   # C/RTL co-simulation (only after csynth)
python3 -m hwflow export gemm/build.yaml   # IP export (only after csynth); vivado can run afterwards
```

- The first step creates the HLS project; later steps reopen the same project, so earlier results are kept.
- Each step leaves its own `hls/run_<step>.tcl` and `hls/vitis_hls_<step>.log`.
- Finished steps are recorded in `steps_done` in `hls/hls_summary.json`. After export the status becomes `ok`, so `vivado` and `all` continue from there.
- Running csynth again invalidates an earlier export, so run export again too.
- Step commands always run (no `--force` needed). To start from scratch, delete the build folder.

## Build folder

```
gemm/builds/gemm_<build_id>/       build_id = hash of the HW sources + HLS/Vivado settings
├── hls/       run_hls.tcl, vitis_hls.log, csynth.xml, hls_summary.json, hls_prj/
├── vivado/    build_vivado.tcl, vivado.log, reports/, vivado_summary.json, prj/
├── overlay/   gemm.bit, gemm.hwh, gemm.tcl, x<top>_hw.h
├── board/     board_run.log (application output), results/ (result.csv, meta.json, raw/), board_summary.json
├── report.csv  one row per case: build info + HLS estimates + WNS + resources + measured latency/energy
└── report.json
```

Changing only the testbench, tb_args or board settings keeps the build_id, so HLS and Vivado are not rerun. Changing the HW sources or the clock creates a new build folder.

## Automatic block design

A Zynq UltraScale+ PS (ZCU104 board preset) and one HLS IP, connected as follows:
- `s_axi_control` ← `M_AXI_HPM0_FPD` through a SmartConnect (`apply_bd_automation`).
- The m_axi ports are spread round-robin over `vivado.hp_ports` HP ports (default 1). Each HP port gets one SmartConnect with N slave inputs (N masters → 1 HP port).
- PL clock 0 = `clock_mhz`; a proc_sys_reset drives all resets.
- `vivado.ooc` (default true) keeps out-of-context synthesis of the block design IPs; set it to false to synthesize everything in one run.

If you need custom connections, point `vivado.bd_tcl` to a file made with `write_bd_tcl`. That file must come from the same Vivado version.

## Board application and result format

hwflow runs `board.command` on the board, from one of two places:

- `board.app_files: [app/run_x.py]` (recommended): the server copies these files to `<work_dir>/<build_id>/app/` on every run and runs from there. No manual copying, and the latest version next to build.yaml always runs.
- `board.app_dir: /home/xilinx/...`: run from a folder that already exists on the board.

Placeholders in the command are replaced with this build's values, and the same values are exported as environment variables (`HWFLOW_BIT`, `HWFLOW_RESULTS`, ...).

| placeholder | value |
|---|---|
| `{bit}` `{hwh}` `{hw_h}` | paths of the files copied to the board (`<work_dir>/<build_id>/overlay/`) |
| `{overlay}` | the overlay folder itself |
| `{results}` | folder for the results (`<work_dir>/<build_id>/results/`, emptied before each run) |
| `{build_id}` `{bit_md5}` | write them into meta.json so the server can check that the right bitstream ran |
| `{clock_mhz}` `{name}` `{top}` | values from build.yaml |

The application must write these two files into `{results}`.

**result.csv**: one row per case

| column | required | meaning |
|---|---|---|
| `case` | yes | case name |
| `latency_ms` | yes | mean latency |
| `power_w` | yes | mean power during the measurement (sum of the measured rails) |
| `latency_ms_std`, `repeat`, `idle_w`, `energy_mj`, `pass` | | used as-is when present |
| any other column (`M`, `N`, `power_INT_w`, ...) | | copied to report.csv |

If `energy_mj` is missing, the server fills it with `latency_ms × power_w`. If `idle_w` is present, it also computes `dyn_energy_mj = latency_ms × (power_w − idle_w)`.

**meta.json**: always write it last.

```json
{"status": "done", "build_id": "gemm_00b8aa20", "bit_md5": "...",
 "clock_mhz": 100, "rails": ["INT", "1V2"], "start": "...", "end": "..."}
```

- `status`: `done` or `failed`. A missing or different value is treated as a failure.
- `build_id` / `bit_md5`: the server stops on a mismatch. If both are missing it only warns.
- An optional `raw/` folder is fetched as-is without being read.

A Python application can end like this:

```python
import csv, json, os
out = args.out                       # {results}
with open(os.path.join(out, "result.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["case", "M", "N", "K", "latency_ms", "latency_ms_std", "repeat", "power_w", "idle_w"])
    w.writeheader()
    w.writerows(rows)                # rows: list of dict
with open(os.path.join(out, "meta.json"), "w") as f:
    json.dump({"status": "done", "build_id": args.build_id, "bit_md5": args.bit_md5,
               "rails": ["INT", "1V2"]}, f)
```

After `timeout_s` the server drops the ssh connection and marks the run as failed. The process may still be running on the board, so check it there.
