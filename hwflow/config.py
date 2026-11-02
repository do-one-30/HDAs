"""build.yaml loading: sources, tool settings and a content-based build id."""

from __future__ import annotations

import glob
import hashlib
import json
from pathlib import Path


class ConfigError(ValueError):
    pass


def load_document(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is not installed (pip install pyyaml), or use a .json config") from exc
    return yaml.safe_load(text) or {}


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class BuildConfig:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.root = self.path.parent
        doc = load_document(self.path)
        self.doc = doc
        for key in ("top", "sources"):
            if not doc.get(key):
                raise ConfigError(f"{self.path.name}: '{key}' is required")
        self.top = doc["top"]
        self.name = doc.get("name", self.top)
        self.sources = self._files(doc["sources"], "sources")
        self.headers = self._files(doc.get("headers"), "headers", required=False)
        self.testbench = self._files(doc.get("testbench"), "testbench", required=False)
        self.tb_data = [self.resolve(p) for p in _as_list(doc.get("tb_data"))]
        self.include_dirs = [self.resolve(p) for p in _as_list(doc.get("include_dirs"))]
        if not self.include_dirs:
            self.include_dirs = sorted({p.parent for p in self.sources + self.headers})
        self.cflags = str(doc.get("cflags", "")).strip()
        self.tb_cflags = str(doc.get("tb_cflags", self.cflags)).strip()
        self.part = doc.get("part", "xczu7ev-ffvc1156-2-e")
        self.clock_mhz = float(doc.get("clock_mhz", 100.0))
        self.steps = [s.lower() for s in _as_list(doc.get("steps", ["csim", "csynth", "export"]))]
        unknown = set(self.steps) - {"csim", "csynth", "cosim", "export"}
        if unknown:
            raise ConfigError(f"unknown HLS steps {sorted(unknown)}")
        if "export" in self.steps and "csynth" not in self.steps:
            raise ConfigError("'export' needs 'csynth' in steps")
        self.tb_args = str(doc.get("tb_args", ""))
        self.hls_directives = self._files(doc.get("hls_directives"), "hls_directives", required=False)
        self.extra_tcl = _as_list(doc.get("hls_extra_tcl"))
        # % of the device a csynth estimate may use before Vivado is skipped; number or {LUT: 90, default: 100}
        self.hls_max_util_pct = doc.get("hls_max_util_pct", 100)
        tools = doc.get("tools") or {}
        self.vitis_hls = tools.get("vitis_hls", "vitis_hls")
        self.vivado = tools.get("vivado", "vivado")
        self.builds_dir = self.resolve(doc.get("builds_dir", "builds"))
        self.vivado_cfg = doc.get("vivado") or {}
        self.board_cfg = doc.get("board") or {}

    def resolve(self, rel):
        path = Path(str(rel)).expanduser()
        return path if path.is_absolute() else (self.root / path).resolve()

    def _files(self, patterns, what, required=True):
        files = []
        for pattern in _as_list(patterns):
            matches = sorted(glob.glob(str(self.resolve(pattern))))
            if not matches:
                raise ConfigError(f"{what}: nothing matches {pattern}")
            files.extend(Path(m).resolve() for m in matches)
        if required and not files:
            raise ConfigError(f"{what}: no files")
        return files

    @property
    def clock_period_ns(self):
        return 1000.0 / self.clock_mhz

    def fill(self, text):
        """Replace {root} and {top} in user strings such as tb_args."""
        return str(text).replace("{root}", str(self.root)).replace("{top}", self.top)

    def build_id(self):
        """Hash of everything that changes the bitstream: HW sources and HLS/Vivado settings.

        Testbench, tb_args, steps and board settings are left out, so editing them
        does not trigger a new HLS + Vivado build.
        """
        digest = hashlib.sha1()
        vivado = {k: v for k, v in self.vivado_cfg.items() if k not in ("jobs", "allow_timing_fail")}
        settings = {"top": self.top, "part": self.part, "clock_mhz": self.clock_mhz, "cflags": self.cflags,
                    "include_dirs": [str(p) for p in self.include_dirs], "extra_tcl": self.extra_tcl,
                    "vivado": vivado}
        digest.update(json.dumps(settings, sort_keys=True, default=str).encode())
        files = self.sources + self.headers + self.hls_directives
        if vivado.get("bd_tcl"):
            files.append(self.resolve(vivado["bd_tcl"]))
        for path in files:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()[:8]

    def build_dir(self):
        return self.builds_dir / f"{self.name}_{self.build_id()}"
