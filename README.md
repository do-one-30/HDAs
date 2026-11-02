# hwflow: HLS → Vivado → ZCU104 측정 자동화 (server 쪽)

Design마다 `build.yaml` 하나로 아래를 차례로 실행해요.

1. **HLS**: `build.yaml`로 Tcl을 생성해서 `vitis_hls`로 csim → csynth → (cosim) → export_design을 실행해요.
2. **Vivado**: PS + HLS IP block design을 자동으로 만들거나 `bd_tcl`을 사용해서 bitstream을 만들어요. Timing(WNS/WHS)을 검사하고, utilization·power report를 남겨요.
3. **Board**: ssh/scp로 bit, hwh, hw.h를 board에 보내고, board에 이미 있는 사용자 application을 실행한 뒤 결과(`result.csv`, `meta.json`)를 가져와요.
4. **Report**: HLS·Vivado 수치와 board 측정값을 합쳐 `report.csv`를 만들어요.

## 준비

- Server: Python 3.8+, PyYAML, `vitis_hls`와 `vivado`가 PATH에 있어야 해요 (`source <Xilinx>/settings64.sh`).
- Board: latency와 power를 재는 application이 `board.app_dir`에 있어야 하고, server에서 비밀번호 없이 `ssh <host>`가 되어야 해요 (`ssh-copy-id root@pynq`).
- Design 폴더 예시:
  ```
  gemm/
  ├── build.yaml
  ├── src/gemm.cc, gemm.h
  ├── tb/gemm_tb.cc
  ```

## 명령

설치나 PYTHONPATH 설정 없이 `run_hwflow.py`의 경로로 바로 실행해요. 설계 폴더가 어디에 있든 build.yaml 경로만 주면 돼요.

```bash
python3 ~/tools/hwflow_pkg/run_hwflow.py all ~/designs/gemm/build.yaml
```

`python3 -m hwflow ...` 형식도 그대로 돼요 (PYTHONPATH에 hwflow_pkg가 있을 때). 아래 명령의 `python3 -m hwflow`는 `python3 <경로>/run_hwflow.py`로 바꿔 써도 같아요.

```bash
python3 -m hwflow tcl gemm/build.yaml          # 생성될 HLS/Vivado Tcl만 출력
python3 -m hwflow check-board gemm/build.yaml  # ssh, application 폴더, sudo 확인
python3 -m hwflow hls gemm/build.yaml
python3 -m hwflow vivado gemm/build.yaml
python3 -m hwflow board gemm/build.yaml
python3 -m hwflow all gemm/build.yaml          # 위 세 단계를 한 번에
```

이미 성공한 단계는 다시 실행하지 않아요. 강제로 다시 하려면 `--force`를 붙이세요.

### HLS 단계를 하나씩 실행

`hls`는 build.yaml의 `steps`를 vitis_hls 한 번에 모두 실행해요. 단계별로 나눠서 실행하려면 단계 이름을 명령으로 쓰세요. build.yaml의 `steps`는 이때 무시돼요.

```bash
python3 -m hwflow csim   gemm/build.yaml   # C simulation (프로젝트가 없으면 새로 만듦)
python3 -m hwflow csynth gemm/build.yaml   # 합성 + 리소스 검사 (hls_max_util_pct)
python3 -m hwflow cosim  gemm/build.yaml   # C/RTL co-simulation (csynth 후에만)
python3 -m hwflow export gemm/build.yaml   # IP export (csynth 후에만). 끝나면 vivado 실행 가능
```

- 첫 단계가 HLS 프로젝트를 만들고, 이후 단계는 같은 프로젝트를 다시 열어서 앞 단계 결과를 유지해요.
- 단계마다 `hls/run_<step>.tcl`과 `hls/vitis_hls_<step>.log`가 따로 남아요.
- `hls/hls_summary.json`의 `steps_done`에 끝난 단계가 기록돼요. export까지 끝나면 status가 `ok`가 되어 `vivado`, `all`이 이어서 동작해요.
- csynth를 다시 하면 이전 export 결과는 무효로 처리되니 export도 다시 하세요.
- 단계 명령은 항상 실행돼요 (`--force` 불필요). 처음부터 다시 하려면 build 폴더를 지우세요.

## 결과 폴더

```
gemm/builds/gemm_<build_id>/       build_id = HW 소스 + HLS/Vivado 설정의 hash
├── hls/       run_hls.tcl, vitis_hls.log, csynth.xml, hls_summary.json, hls_prj/
├── vivado/    build_vivado.tcl, vivado.log, reports/, vivado_summary.json, prj/
├── overlay/   gemm.bit, gemm.hwh, gemm.tcl, x<top>_hw.h
├── board/     board_run.log (application 출력), results/ (result.csv, meta.json, raw/), board_summary.json
├── report.csv  case 한 줄씩: build 정보 + HLS 예상치 + WNS + 자원 + 실측 latency/energy
└── report.json
```

Testbench, tb_args, board 설정만 바꾸면 build_id가 그대로라서 HLS/Vivado를 다시 돌리지 않아요. HW 소스나 clock을 바꾸면 새 build 폴더가 생겨요.

## 자동 block design

Zynq UltraScale+ PS(ZCU104 board preset)와 HLS IP 하나를 놓고 이렇게 연결해요.
- `s_axi_control` → `M_AXI_HPM0_FPD`
- m_axi port들 → `S_AXI_HP0..3_FPD` (port가 4개보다 많으면 HP port를 나눠 써요)
- PL clock 0 = `clock_mhz`, reset과 interconnect는 `apply_bd_automation`이 추가해요.

특별한 연결이 필요하면 `vivado.bd_tcl`에 `write_bd_tcl`로 만든 파일을 지정하세요. 이 경우 Vivado version이 그 파일을 만든 version과 같아야 해요.

## Board application과 결과 형식

hwflow는 board에서 `board.command`를 실행해요. 실행 위치는 둘 중 하나예요.

- `board.app_files: [app/run_x.py]` (권장): 서버의 이 파일들을 매번 `<work_dir>/<build_id>/app/`으로 복사하고 거기서 실행해요. 보드에 손으로 복사할 필요가 없고, 항상 build.yaml 옆의 최신 버전이 실행돼요.
- `board.app_dir: /home/xilinx/...`: 보드에 이미 있는 폴더에서 실행해요.
 command 안의 placeholder는 이번 build 값으로 바뀌고, 같은 값이 환경변수(`HWFLOW_BIT`, `HWFLOW_RESULTS`, ...)로도 전달돼요.

| placeholder | 값 |
|---|---|
| `{bit}` `{hwh}` `{hw_h}` | board에 복사된 파일 경로 (`<work_dir>/<build_id>/overlay/`) |
| `{results}` | 결과를 쓸 폴더 (`<work_dir>/<build_id>/results/`, 실행 전에 비워져요) |
| `{build_id}` `{bit_md5}` | meta.json에 그대로 적어 주면 server가 bitstream이 맞는지 확인해요 |
| `{clock_mhz}` `{name}` `{top}` | build.yaml 값 |

Application은 `{results}`에 아래 두 파일을 써야 해요.

**result.csv**: case 하나당 한 줄

| 열 | 필수 | 의미 |
|---|---|---|
| `case` | O | case 이름 |
| `latency_ms` | O | 평균 latency |
| `power_w` | O | 측정 구간 평균 전력 (측정한 rail 합) |
| `latency_ms_std`, `repeat`, `idle_w`, `energy_mj`, `pass` | | 있으면 그대로 사용 |
| 그 밖의 열 (`M`, `N`, `power_INT_w` 등) | | 그대로 report.csv로 옮겨요 |

`energy_mj`가 없으면 server가 `latency_ms × power_w`로 채우고, `idle_w`가 있으면 `dyn_energy_mj = latency_ms × (power_w − idle_w)`도 계산해요.

**meta.json**: 반드시 마지막에 써요.

```json
{"status": "done", "build_id": "gemm_00b8aa20", "bit_md5": "...",
 "clock_mhz": 150, "rails": ["INT", "1V2", "12V"], "start": "...", "end": "..."}
```

- `status`: `done` 또는 `failed`. 없거나 다른 값이면 server가 실패로 처리해요.
- `build_id` / `bit_md5`: 다르면 server가 멈춰요. 둘 다 없으면 경고만 남겨요.
- `raw/` 폴더(선택)는 내용을 보지 않고 그대로 가져와요.

Python application이라면 끝에 이렇게 쓰면 돼요.

```python
import csv, json, os
out = args.out                       # {results}
with open(os.path.join(out, "result.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["case", "M", "N", "K", "latency_ms", "latency_ms_std", "repeat", "power_w", "idle_w"])
    w.writeheader()
    w.writerows(rows)                # rows: list of dict
with open(os.path.join(out, "meta.json"), "w") as f:
    json.dump({"status": "done", "build_id": args.build_id, "bit_md5": args.bit_md5,
               "rails": ["INT", "1V2", "12V"]}, f)
```

`timeout_s`가 지나면 server 쪽 ssh를 끊고 실패로 처리해요. Board에서 프로세스가 남아 있을 수 있으니 그때는 직접 확인해 주세요.
