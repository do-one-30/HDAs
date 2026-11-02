"""Generate the Vivado Tcl (block design -> bitstream), run it, and collect bit/hwh + reports.

Two block-design modes:
  * auto (default): Zynq UltraScale+ PS + the HLS IP. s_axi_control goes to
    M_AXI_HPM0_FPD and the m_axi ports of the IP are spread over `hp_ports`
    S_AXI_HPx_FPD ports (default 1: all through one SmartConnect into HP0, as in
    the hand-made GPT2 design), all through apply_bd_automation. PL clock 0 = clock_mhz.
  * bd_tcl: source a user block-design script (e.g. one written by write_bd_tcl);
    its PL0 clock is overridden with clock_mhz unless override_clock is false.
"""


import json
import re
import shutil
import subprocess
import time
from pathlib import Path


class VivadoError(RuntimeError):
    pass


def _brace(value):
    return "{" + str(value) + "}"


AUTO_BD = r"""
# ---- block design: PS + HLS IP, wired by apply_bd_automation -------------------
create_bd_design $bd_name
set ps_vlnv [lindex [lsort [get_ipdefs -all xilinx.com:ip:zynq_ultra_ps_e:*]] end]
set ps [create_bd_cell -type ip -vlnv $ps_vlnv zynq_ultra_ps_e_0]
if {$board_part ne ""} {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
}
set hls_vlnv [lindex [lsort [get_ipdefs -all "*:hls:${top}:*"]] end]
if {$hls_vlnv eq ""} { error "HLS IP '$top' not found in $ip_repo" }
set ip [create_bd_cell -type ip -vlnv $hls_vlnv ${top}_0]

set masters [get_bd_intf_pins -of_objects $ip -filter {MODE == Master}]
set n_hp [expr {max(1, min([llength $masters], $hp_ports, 4))}]
# HP0..HP3 FPD are S_AXI_GP2..GP5 in the PS configuration
set cfg [list CONFIG.PSU__USE__M_AXI_GP0 {1} CONFIG.PSU__USE__M_AXI_GP1 {0} CONFIG.PSU__USE__M_AXI_GP2 {0} \
             CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $clock_mhz]
for {set i 0} {$i < $n_hp} {incr i} { lappend cfg CONFIG.PSU__USE__S_AXI_GP[expr {$i + 2}] {1} }
set_property -dict $cfg $ps

apply_bd_automation -rule xilinx.com:bd_rule:axi4 -config [list Clk_master {Auto} Clk_slave {Auto} \
    Clk_xbar {Auto} Master {/zynq_ultra_ps_e_0/M_AXI_HPM0_FPD} Slave "/${top}_0/s_axi_control" \
    ddr_seg {Auto} intc_ip {New AXI SmartConnect} master_apm {0}] [get_bd_intf_pins ${top}_0/s_axi_control]
# data path wired by hand: one SmartConnect per HP port, masters spread round-robin.
# (apply_bd_automation adds a new SmartConnect for every master, which leaves extra ones
#  when several masters share one HP port)
set clk [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]
set rst [get_bd_pins -of_objects [get_bd_cells -filter {VLNV =~ "xilinx.com:ip:proc_sys_reset:*"}] \
             -filter {NAME == peripheral_aresetn}]
if {[llength $rst] == 0} { error "no proc_sys_reset after the s_axi_control automation" }
set smc_vlnv [lindex [lsort [get_ipdefs -all xilinx.com:ip:smartconnect:*]] end]
for {set h 0} {$h < $n_hp} {incr h} {
    set group {}
    for {set i $h} {$i < [llength $masters]} {incr i $n_hp} { lappend group [lindex $masters $i] }
    set smc [create_bd_cell -type ip -vlnv $smc_vlnv hp${h}_smc]
    set_property -dict [list CONFIG.NUM_SI [llength $group] CONFIG.NUM_MI {1}] $smc
    connect_bd_net $clk [get_bd_pins hp${h}_smc/aclk] [get_bd_pins zynq_ultra_ps_e_0/saxihp${h}_fpd_aclk]
    connect_bd_net [lindex $rst 0] [get_bd_pins hp${h}_smc/aresetn]
    set j 0
    foreach m $group {
        connect_bd_intf_net $m [get_bd_intf_pins hp${h}_smc/[format S%02d_AXI $j]]
        incr j
    }
    connect_bd_intf_net [get_bd_intf_pins hp${h}_smc/M00_AXI] [get_bd_intf_pins zynq_ultra_ps_e_0/S_AXI_HP${h}_FPD]
}
puts "hwflow: [llength $masters] m_axi port(s) -> $n_hp HP port(s)"
assign_bd_address
validate_bd_design
save_bd_design
"""

USER_BD = r"""
# ---- block design: user script ------------------------------------------------
source $bd_tcl
if {[llength [get_bd_designs -quiet]] == 0} { error "BD script did not create a block design" }
set bd_name [get_property NAME [lindex [get_bd_designs] 0]]
if {$override_clock} {
    # a write_bd_tcl script carries its own PL clock; make it follow clock_mhz
    set ps [lindex [get_bd_cells -hierarchical -filter {VLNV =~ "xilinx.com:ip:zynq_ultra_ps_e:*"}] 0]
    set old [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $ps]
    set_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $clock_mhz $ps
    puts "hwflow: PL0 clock $old MHz -> $clock_mhz MHz"
    validate_bd_design
    save_bd_design
}
"""

BUILD = r"""
# ---- wrapper, synth, impl, bitstream ------------------------------------------
set bd_file [get_files $bd_name.bd]
add_files -norecurse [make_wrapper -files $bd_file -top]
set_property top ${bd_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1
# ooc 1: every BD IP is synthesized in its own out-of-context run, in parallel (-jobs).
# ooc 0: one global synthesis inside synth_1 (slower, no per-IP runs that can fail on their own).
if {!$ooc} { set_property synth_checkpoint_mode None $bd_file }
generate_target all $bd_file
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
foreach run [get_runs -filter {IS_SYNTHESIS}] {
    if {[get_property PROGRESS $run] ne "100%"} { error "$run failed -- see prj.runs/$run/runme.log" }
}
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "impl_1 failed -- see prj.runs/impl_1/runme.log" }

# ---- reports ------------------------------------------------------------------
open_run impl_1
file mkdir reports
report_timing_summary -file reports/timing_summary.rpt
report_utilization -file reports/utilization.rpt
report_power -file reports/power.rpt
set wns [get_property STATS.WNS [get_runs impl_1]]
set whs [get_property STATS.WHS [get_runs impl_1]]
set ps [lindex [get_bd_cells -hierarchical -filter {VLNV =~ "xilinx.com:ip:zynq_ultra_ps_e:*"}] 0]
set pl0 [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ $ps]

# ---- outputs: <name>.bit + <name>.hwh (same basename for PYNQ) ----------------
file mkdir $out_dir
set hwh [glob -nocomplain prj.gen/sources_1/bd/$bd_name/hw_handoff/$bd_name.hwh]
if {$hwh eq ""} { set hwh [lindex [glob -nocomplain prj.gen/sources_1/bd/*/hw_handoff/*.hwh] 0] }
if {$hwh eq ""} { error "no .hwh found" }
file copy -force $hwh $out_dir/$out_name.hwh
set bit [lindex [glob -nocomplain prj.runs/impl_1/*.bit] 0]
if {$bit eq ""} { error "no bitstream found in prj.runs/impl_1" }
file copy -force $bit $out_dir/$out_name.bit
write_bd_tcl -force $out_dir/$out_name.tcl

set f [open metrics.json w]
puts $f "{\"wns_ns\": $wns, \"whs_ns\": $whs, \"pl0_actual_mhz\": $pl0, \"bd_name\": \"$bd_name\"}"
close $f
puts "hwflow: WNS=$wns WHS=$whs"
exit
"""


def generate_tcl(cfg, ip_repo, out_dir):
    viv = cfg.vivado_cfg
    bd_tcl = viv.get("bd_tcl")
    header = [
        "# generated by hwflow from " + str(cfg.path),
        f"set top {cfg.top}",
        f"set ip_repo {_brace(ip_repo)}",
        f"set clock_mhz {cfg.clock_mhz:g}",
        f"set jobs {int(viv.get('jobs', 8))}",
        f"set bd_name {viv.get('bd_name', 'design_1')}",
        f"set out_dir {_brace(out_dir)}",
        f"set out_name {cfg.name}",
        f"set bd_tcl {_brace(cfg.resolve(bd_tcl)) if bd_tcl else _brace('')}",
        f"set hp_ports {int(viv.get('hp_ports', 1))}",
        f"set override_clock {1 if viv.get('override_clock', True) else 0}",
        f"set ooc {1 if viv.get('ooc', True) else 0}",
        f"create_project -force prj . -part {_brace(cfg.part)}",
        "set board_part [lindex [lsort [get_board_parts -quiet *zcu104*]] end]",
        'if {$board_part ne ""} { set_property BOARD_PART $board_part [current_project] }',
        "set_property ip_repo_paths $ip_repo [current_project]",
        "update_ip_catalog -rebuild",
    ]
    return "\n".join(header) + (USER_BD if bd_tcl else AUTO_BD) + BUILD


def _first_number(text):
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def parse_utilization(path):
    """Used / available for the main resources from report_utilization."""
    wanted = {"CLB LUTs": "LUT", "CLB Registers": "FF", "Block RAM Tile": "BRAM_tile",
              "URAM": "URAM", "DSPs": "DSP"}
    out = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        name = cells[0].rstrip("*").strip()
        if name in wanted and wanted[name] not in out:
            nums = [c for c in cells[1:] if re.fullmatch(r"-?\d+(?:\.\d+)?", c)]
            if len(nums) >= 3:
                out[wanted[name]] = {"used": float(nums[0]), "available": float(nums[-2]), "util_pct": float(nums[-1])}
    return out


def parse_power(path):
    out = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        for key, label in (("total_onchip_w", "Total On-Chip Power (W)"), ("dynamic_w", "Dynamic (W)"),
                           ("static_w", "Device Static (W)")):
            if label in line and key not in out:
                out[key] = _first_number(line.split(label, 1)[1])
    return out


def run_vivado(cfg, build_dir, hls_summary, force=False, log=print):
    viv_dir = Path(build_dir) / "vivado"
    out_dir = Path(build_dir) / "overlay"
    summary_path = viv_dir / "vivado_summary.json"
    if summary_path.exists() and not force:
        summary = json.loads(summary_path.read_text())
        if summary.get("status") == "ok":
            log(f"vivado: up to date ({viv_dir})")
            return summary
    ip_repo = hls_summary.get("ip_repo")
    if not ip_repo:
        raise VivadoError("HLS summary has no ip_repo; add 'export' to steps")
    viv_dir.mkdir(parents=True, exist_ok=True)
    tcl = viv_dir / "build_vivado.tcl"
    tcl.write_text(generate_tcl(cfg, ip_repo, out_dir), encoding="utf-8")
    log_path = viv_dir / "vivado.log"
    cmd = [cfg.vivado, "-mode", "batch", "-nojournal", "-log", str(log_path), "-source", str(tcl)]
    log(f"vivado: {' '.join(cmd)}")
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=viv_dir, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except FileNotFoundError as exc:
        raise VivadoError(f"'{cfg.vivado}' not found; source the Vivado settings64.sh or set tools.vivado") from exc

    summary = {"status": "error", "returncode": proc.returncode, "seconds": round(time.time() - start, 1),
               "tcl": str(tcl), "log": str(log_path), "clock_mhz": cfg.clock_mhz}
    try:
        if proc.returncode != 0:
            errors = []
            if log_path.exists():
                errors = [l.strip() for l in log_path.read_text(errors="replace").splitlines() if l.startswith("ERROR:")]
            summary["errors"] = errors[:20]
            raise VivadoError(f"vivado exited with {proc.returncode}; first errors: {errors[:3]}")
        metrics = json.loads((viv_dir / "metrics.json").read_text())
        summary.update(metrics)
        reports = viv_dir / "reports"
        if (reports / "utilization.rpt").exists():
            summary["utilization"] = parse_utilization(reports / "utilization.rpt")
        if (reports / "power.rpt").exists():
            summary["vivado_power"] = parse_power(reports / "power.rpt")
        summary["bit"] = str(out_dir / f"{cfg.name}.bit")
        summary["hwh"] = str(out_dir / f"{cfg.name}.hwh")
        if hls_summary.get("hw_h"):
            dest = out_dir / Path(hls_summary["hw_h"]).name
            shutil.copy2(hls_summary["hw_h"], dest)
            summary["hw_h"] = str(dest)
        timing_ok = metrics["wns_ns"] >= 0 and metrics["whs_ns"] >= 0
        summary["timing_met"] = timing_ok
        if not timing_ok and not cfg.vivado_cfg.get("allow_timing_fail", False):
            summary["status"] = "timing_failed"
            raise VivadoError(f"timing not met at {cfg.clock_mhz:g} MHz (WNS={metrics['wns_ns']} ns, "
                              f"WHS={metrics['whs_ns']} ns); lower clock_mhz or set vivado.allow_timing_fail")
        summary["status"] = "ok"
    finally:
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"vivado: ok in {summary['seconds']} s, WNS {summary['wns_ns']} ns, bit {summary['bit']}")
    return summary
