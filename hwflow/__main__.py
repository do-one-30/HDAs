"""Command line: python3 -m hwflow <command> build.yaml
 
  tcl          print the generated HLS and Vivado Tcl (nothing is run)
  hls          Vitis HLS: csim / csynth / cosim / export (as listed in steps)
  csim         Vitis HLS C simulation only
  csynth       Vitis HLS synthesis only (keeps the project, so export can follow)
  cosim        C/RTL co-simulation only (after csynth)
  export       IP export only (after csynth); then `vivado` can run
  vivado       block design -> bitstream (+ hwh, reports, timing check)
  check-board  test ssh, the application folder and sudo on the board
  board        copy the overlay to the board, run the application, fetch results
  all          hls -> vivado -> board -> report.csv
"""
 
 
import argparse
import json
import sys
from pathlib import Path
 
from . import hls, remote, vivado
from .config import BuildConfig, ConfigError
 
 
def _load(build_dir, sub, name):
    path = Path(build_dir) / sub / name
    if not path.exists():
        raise SystemExit(f"{path} not found; run the earlier step first")
    return json.loads(path.read_text())
 
 
def main(argv=None):
    parser = argparse.ArgumentParser(prog="hwflow", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("tcl", "hls", *hls.STEPS, "vivado", "check-board", "board", "all"))
    parser.add_argument("config", help="build.yaml of one design")
    parser.add_argument("--force", action="store_true", help="rerun steps that are already up to date")
    args = parser.parse_args(argv)
 
    try:
        cfg = BuildConfig(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    build_dir = cfg.build_dir()
 
    if args.command == "tcl":
        print("#" * 20, "HLS", "#" * 20)
        print(hls.generate_tcl(cfg), end="")
        print("#" * 20, "Vivado", "#" * 20)
        print(vivado.generate_tcl(cfg, build_dir / "hls" / hls.PROJECT / hls.SOLUTION / "impl" / "ip",
                                  build_dir / "overlay"), end="")
        return 0
    if args.command == "check-board":
        try:
            board = remote.check_connection(cfg)
        except remote.RemoteError as exc:
            print(f"FAILED: {exc}")
            return 1
        print(f"board OK: {board.host}, application at {board.app_dir}, sudo={board.sudo}")
        return 0
 
    print(f"build: {build_dir}")
    if args.command in hls.STEPS:
        try:
            hls.run_step(cfg, build_dir, args.command)
        except hls.HlsError as exc:
            print(f"FAILED: {exc}")
            return 1
        return 0
    try:
        if args.command in ("hls", "all"):
            hls_sum = hls.run_hls(cfg, build_dir, force=args.force)
        else:
            hls_sum = _load(build_dir, "hls", "hls_summary.json")
            if args.command == "vivado":
                hls.check_resources(cfg, hls_sum)
        if args.command in ("vivado", "all"):
            viv_sum = vivado.run_vivado(cfg, build_dir, hls_sum, force=args.force)
        elif args.command == "board":
            viv_sum = _load(build_dir, "vivado", "vivado_summary.json")
        if args.command in ("board", "all"):
            board_sum = remote.run_on_board(cfg, build_dir, viv_sum)
            report = remote.merge_report(cfg, build_dir, hls_sum, viv_sum, board_sum)
            print(f"report: {report}")
            if board_sum["status"] != "ok":
                print(f"board run reported a failure (meta.json status or exit code); see {board_sum['log']}")
                return 1
    except (hls.HlsError, vivado.VivadoError, remote.RemoteError) as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 