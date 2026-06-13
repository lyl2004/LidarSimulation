#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""1D Lidar Simulation — Demo UI

Left panel  : editable simulation parameters (instrument / global /
              per-scenario optical + particle-distribution specs)
              with a recompute control section at the bottom.
Right panel : five tab groups, each with an interactive Plotly chart
              (log/linear toggle, legend-click curve visibility),
              a collapsible numerical-summary table, a collapsible
              reference-image panel, and CSV download links.

Run:
    pixi run -e gui python app/demo_ui.py
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi.responses import FileResponse, StreamingResponse
from nicegui import app, ui, background_tasks

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE       = Path(__file__).resolve().parent
_SRC       = HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TOOLS_DIR = HERE.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from path_resolver import get_root as _get_root  # noqa: E402
from diagnostics import get_or_create_session, new_component_logger  # noqa: E402
import cache_keys  # noqa: E402
import cache_runtime  # noqa: E402
ROOT       = _get_root()
_OUTPUTS   = ROOT / "temp" / "lidar_1d" / "outputs_high_precision_latest" / "local"
DOCS       = ROOT / "temp" / "set"
OVERRIDES  = ROOT / "temp" / "lidar_1d" / "param_overrides.json"
MFF_SCRIPT   = ROOT / "temp" / "lidar_1d" / "make_final_figures.py"
HIAL_SCRIPT  = ROOT / "temp" / "lidar_1d" / "high_altitude_aerosol.py"
HIAL_OUT_DIR = ROOT / "temp" / "lidar_1d" / "outputs_high_altitude" / "local"
HISTORY_DIR = cache_runtime.LAYOUT.history_root
MANIFEST    = cache_runtime.LAYOUT.manifest_path
_DIAG_SESSION = get_or_create_session("gui")
_DIAG_LOGGER = new_component_logger(_DIAG_SESSION, "gui")
_STARTUP_TIMELINE = "startup_timeline.jsonl"
_RECOMPUTE_TIMELINE = "recompute_timeline.jsonl"
_RECOMPUTE_STDOUT_LOG = "recompute_stdout.log"


def _diag_event(stage: str, *, status: str = "ok", elapsed_ms: float | None = None,
                payload: dict | None = None, timeline: str | None = None) -> None:
    _DIAG_LOGGER.event(stage, status=status, elapsed_ms=elapsed_ms, payload=payload)
    if timeline:
        _DIAG_SESSION.write_component_event(timeline, stage, status=status, elapsed_ms=elapsed_ms, payload=payload)


def _diag_append_stdout(text: str) -> None:
    if not text:
        return
    _DIAG_SESSION.append_component_log(_RECOMPUTE_STDOUT_LOG, text)


def _diag_update_gui_summary(payload: dict, *, status: str | None = None) -> None:
    _DIAG_SESSION.update_summary("gui", payload, status=status)

_DEFAULT_RESULT_DIR = cache_runtime.LAYOUT.default_result_root
_DEFAULT_RESULT_ID = cache_runtime.DEFAULT_RESULT_ID

# ---------------------------------------------------------------------------
# Active-run pointers — mutable, switched when user loads a history entry
# ---------------------------------------------------------------------------
_active_data_dir:    Path = _OUTPUTS / "data"
_active_figures_dir: Path = _OUTPUTS / "figures"
_active_summary:     Path = _OUTPUTS / "summary.json"
_csv_cache: dict[str, dict[str, list[float]]] = {}


def _refresh_active_paths(run_id: str | None = None) -> str | None:
    global _active_data_dir, _active_figures_dir, _active_summary
    paths = cache_runtime.get_run_paths(run_id)
    new_data = paths["data"]
    if new_data != _active_data_dir:
        _csv_cache.clear()
    _active_data_dir = new_data
    _active_figures_dir = paths["figures"]
    _active_summary = paths["summary"]
    return paths.get("run_id")

# Convenient aliases used throughout the file (read via functions, not directly)
def _DATA_DIR()   -> Path: return _active_data_dir
def _FIGURES()    -> Path: return _active_figures_dir
def _SUMMARY()    -> Path: return _active_summary

# Keep legacy names for static mount (always the default output dir)
FIGURES  = _OUTPUTS / "figures"
DATA_DIR = _OUTPUTS / "data"
SUMMARY  = _OUTPUTS / "summary.json"

if DOCS.exists():
    app.add_static_files("/docs", str(DOCS))

# ---------------------------------------------------------------------------
# Clean-export column mapping
# export_name → (src_csv, x_col, y_col)
# One export file per curve; exactly two columns (x, y) in each output.
# ---------------------------------------------------------------------------
_EXPORT_CURVES: dict[str, tuple[str, str, str]] = {
    "fig1_RadiationFog.csv":             ("radiation_fog_power.csv",                   "range_m", "power_signal_raw"),
    "fig2_AdvectionFog.csv":             ("advection_fog_power.csv",                   "range_m", "power_signal_raw"),
    "fig3_UrbanIndustrialHaze.csv":      ("urban_industrial_haze_power.csv",           "range_m", "power_signal_raw"),
    "fig4_RuralContinentalHaze.csv":     ("rural_continental_haze_power.csv",          "range_m", "power_signal_raw"),
    "fig5_DustDesertHaze.csv":           ("dust_desert_haze_power.csv",                "range_m", "power_signal_raw"),
    "fig6_MaritimeHaze.csv":             ("maritime_haze_power.csv",                   "range_m", "power_signal_raw"),
    "fig7_UrbanIndustrialHazeDepol.csv": ("urban_industrial_haze_depolarization.csv",  "range_m", "echo_depolarization_ratio"),
    "fig8_RuralContinentalHazeDepol.csv":("rural_continental_haze_depolarization.csv", "range_m", "echo_depolarization_ratio"),
    "fig9_DustDesertHazeDepol.csv":      ("dust_desert_haze_depolarization.csv",       "range_m", "echo_depolarization_ratio"),
    "fig10_MaritimeHazeDepol.csv":       ("maritime_haze_depolarization.csv",          "range_m", "echo_depolarization_ratio"),
    "fig11a_LightRain.csv":              ("rain_power.csv",                            "range_m", "light_rain_power_signal_raw"),
    "fig11b_ModerateRain.csv":           ("rain_power.csv",                            "range_m", "moderate_rain_power_signal_raw"),
    "fig11c_HeavyRain.csv":              ("rain_power.csv",                            "range_m", "heavy_rain_power_signal_raw"),
    "fig1_RadiationFog_SNR.csv":             ("radiation_fog_power.csv",                   "range_m", "snr_db"),
    "fig2_AdvectionFog_SNR.csv":             ("advection_fog_power.csv",                   "range_m", "snr_db"),
    "fig3_UrbanIndustrialHaze_SNR.csv":      ("urban_industrial_haze_power.csv",           "range_m", "snr_db"),
    "fig4_RuralContinentalHaze_SNR.csv":     ("rural_continental_haze_power.csv",          "range_m", "snr_db"),
    "fig5_DustDesertHaze_SNR.csv":           ("dust_desert_haze_power.csv",                "range_m", "snr_db"),
    "fig6_MaritimeHaze_SNR.csv":             ("maritime_haze_power.csv",                   "range_m", "snr_db"),
    "fig11a_LightRain_SNR.csv":              ("rain_power.csv",                            "range_m", "light_rain_snr_db"),
    "fig11b_ModerateRain_SNR.csv":           ("rain_power.csv",                            "range_m", "moderate_rain_snr_db"),
    "fig11c_HeavyRain_SNR.csv":              ("rain_power.csv",                            "range_m", "heavy_rain_snr_db"),
    "fig12_LayeredAtmosphere.csv":           ("layered_atmosphere_power.csv",              "range_m", "power_signal_raw"),
    "fig13_LayeredBeta.csv":                 ("layered_atmosphere_profile.csv",            "range_m", "beta_total_m_inv_sr"),
    "fig14_LayeredAlpha.csv":                ("layered_atmosphere_profile.csv",            "range_m", "alpha_total_m_inv"),
}


def _build_csv_bytes(export_name: str, data_dir: Path) -> bytes | None:
    """Build a 2-column (x, y) CSV for the given export filename. Returns None if unavailable."""
    curve = _EXPORT_CURVES.get(export_name)
    if not curve:
        return None
    src_csv, x_col, y_col = curve
    src = data_dir / src_csv
    if not src.exists():
        return None
    rows: list[dict] = []
    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    actual_y_col = y_col
    if rows and actual_y_col not in rows[0] and actual_y_col.endswith("power_signal_raw"):
        fallback_y_col = actual_y_col.replace("power_signal_raw", "power_observed_raw")
        if fallback_y_col in rows[0]:
            actual_y_col = fallback_y_col
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        try:
            writer.writerow([row[x_col], row[actual_y_col]])
        except KeyError:
            continue
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Native file-save helper — bypasses pywebview, uses OS save dialog directly
# ---------------------------------------------------------------------------

async def _native_save(data: bytes, default_name: str,
                       filetypes: list[tuple[str, str]] | None = None) -> None:
    """Show the OS native save dialog and write data directly to disk.

    Runs tkinter in a thread so the NiceGUI event loop is not blocked.
    Works in pywebview native mode where ui.download() has no effect.
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox

    def _ask() -> str | None:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ext = Path(default_name).suffix.lstrip(".")
        ft = filetypes or [(f"{ext.upper()} file", f"*.{ext}"), ("All files", "*.*")]
        path = filedialog.asksaveasfilename(
            parent=root,
            initialfile=default_name,
            defaultextension=f".{ext}",
            filetypes=ft,
        )
        root.destroy()
        return path or None

    save_path = await asyncio.get_event_loop().run_in_executor(None, _ask)
    if not save_path:
        return
    try:
        Path(save_path).write_bytes(data)
        ui.notify(f"已保存：{Path(save_path).name}", type="positive")
    except Exception as e:
        ui.notify(f"保存失败：{e}", type="negative")


@app.get("/export/csv/{fname}")
async def export_csv(fname: str):
    data = _build_csv_bytes(fname, _DATA_DIR())
    if data is None:
        from fastapi import Response as R
        return R(status_code=404)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/export/img/{slug}/{ext}")
async def export_img(slug: str, ext: str):
    if ext not in ("png", "svg"):
        from fastapi import Response as R
        return R(status_code=404)
    path = _FIGURES() / f"{slug}.{ext}"
    if not path.exists():
        from fastapi import Response as R
        return R(status_code=404)
    media = "image/png" if ext == "png" else "image/svg+xml"
    return FileResponse(
        str(path), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{slug}.{ext}"'},
    )


@app.get("/export/all.zip")
async def export_all_zip():
    import zipfile
    data_dir  = _DATA_DIR()
    figs_dir  = _FIGURES()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for export_name in _EXPORT_CURVES:
            csv_bytes = _build_csv_bytes(export_name, data_dir)
            if csv_bytes is not None:
                zf.writestr(f"data/{export_name}", csv_bytes)
        if figs_dir.exists():
            for p in sorted(figs_dir.glob("*.png")):
                zf.write(p, f"figures/{p.name}")
            for p in sorted(figs_dir.glob("*.svg")):
                zf.write(p, f"figures/{p.name}")
    buf.seek(0)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="lidar_results_{ts}.zip"'},
    )

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
PAL: dict[str, str] = {
    "radiation_fog":          "#000000",   # 黑
    "advection_fog":          "#0000cc",   # 蓝
    "urban_industrial_haze":  "#cc0000",   # 红
    "rural_continental_haze": "#006600",   # 深绿
    "dust_desert_haze":       "#cc6600",   # 深橙
    "maritime_haze":          "#6600cc",   # 深紫
    "light_rain":             "#0099cc",   # 青蓝
    "moderate_rain":          "#004499",   # 深蓝
    "heavy_rain":             "#000033",   # 近黑蓝
    "layered_atmosphere":     "#cc00cc",   # 品红
}

# ---------------------------------------------------------------------------
# Data loading — always reads from the currently active run
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CSV cache — keyed by filename, invalidated when active data dir changes
# ---------------------------------------------------------------------------

def load_csv(fname: str) -> dict[str, list[float]]:
    if fname in _csv_cache:
        return _csv_cache[fname]
    p = _DATA_DIR() / fname
    if not p.exists():
        return {}
    try:
        import numpy as _np
        with open(p, newline="", encoding="utf-8") as f:
            header = f.readline().rstrip("\r\n").split(",")
            data = _np.loadtxt(f, delimiter=",", dtype=float)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        out: dict[str, list[float]] = {col: data[:, i].tolist() for i, col in enumerate(header)}
    except ImportError:
        # numpy not available in this env — fast stdlib fallback
        out = {}
        with open(p, encoding="utf-8") as f:
            header = f.readline().rstrip("\r\n").split(",")
            for col in header:
                out[col] = []
            for line in f:
                parts = line.rstrip("\r\n").split(",")
                for col, v in zip(header, parts):
                    try:
                        out[col].append(float(v))
                    except (ValueError, TypeError):
                        out[col].append(0.0)
    except Exception:
        out = {}
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                for k, v in row.items():
                    try:
                        out.setdefault(k, []).append(float(v))
                    except (ValueError, TypeError):
                        out.setdefault(k, []).append(0.0)
    _csv_cache[fname] = out
    return out


_DISPLAY_MAX_POINTS = 2000  # cap for UI rendering; full data still in cache


def load_csv_display(fname: str) -> dict[str, list[float]]:
    """Like load_csv but downsamples to _DISPLAY_MAX_POINTS for Plotly rendering."""
    full = load_csv(fname)
    if not full:
        return full
    n = len(next(iter(full.values())))
    if n <= _DISPLAY_MAX_POINTS:
        return full
    step = max(1, n // _DISPLAY_MAX_POINTS)
    return {k: v[::step] for k, v in full.items()}


def load_summary() -> dict:
    p = _SUMMARY()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------
_MAX_HISTORY = 10


def _load_manifest() -> dict:
    return cache_runtime.load_manifest()


def _save_manifest(m: dict) -> None:
    cache_runtime.save_manifest(m)


def _ensure_cache_identity_in_manifest() -> None:
    try:
        cache_runtime.ensure_manifest_cache_identity()
    except Exception:
        return


_ensure_cache_identity_in_manifest()


def _run_dir(run_id: str) -> Path:
    return cache_runtime.LAYOUT.run_dir(run_id)


def save_run_to_history(params: dict, label: str = "", precision: str | None = None) -> str:
    result = cache_runtime.archive_local_result(params, label=label, precision=precision, origin="manual")
    _refresh_active_paths(result["run_id"])
    return str(result["run_id"])


def load_run(run_id: str) -> dict:
    cache_runtime.set_active_view(run_id)
    _refresh_active_paths(run_id)
    return load_summary()


def _history_options() -> dict[str, str]:
    """Return {run_id: display_label} ordered newest-first, including default result set."""
    m = _load_manifest()
    opts: dict[str, str] = {}

    if (_DEFAULT_RESULT_DIR / "summary.json").exists():
        opts[_DEFAULT_RESULT_ID] = "默认结果"

    for entry in m["runs"]:
        opts[entry["id"]] = _history_label(entry["id"], entry)
    return opts


def _startup_load_latest() -> None:
    t0 = time.perf_counter()
    _diag_event("startup_load_latest_begin", status="begin", timeline=_STARTUP_TIMELINE)
    active = cache_runtime.load_active_view()
    active_id = active.get("run_id")
    _refresh_active_paths(active_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    _diag_event(
        "startup_load_latest_end",
        elapsed_ms=elapsed_ms,
        timeline=_STARTUP_TIMELINE,
        payload={
            "active_id": active_id,
            "summary_path": str(_active_summary),
        },
    )


# Run at import time so data is correct before the UI builds
_startup_load_latest()


# ---------------------------------------------------------------------------
# Plotly helpers  (pure-dict approach — no Python plotly package needed)
# ---------------------------------------------------------------------------

def _trace(x: list, y: list, name: str, color: str,
           dash: str = "solid") -> dict:
    return {
        "type": "scatter",
        "x": x, "y": y, "name": name,
        "mode": "lines",
        "line": {"color": color, "width": 2, "dash": dash},
        "hovertemplate": "%{y:.4e}<extra>" + name + "</extra>",
    }


def _layout(title: str, y_label: str, log: bool) -> dict:
    return {
        "title": {"text": title, "font": {"size": 15, "color": "#2c3e50"}},
        "xaxis": {
            "title": {"text": "距离  R  (m)", "font": {"size": 11}},
            "gridcolor": "#e4e4e4", "showgrid": True, "zeroline": False,
        },
        "yaxis": {
            "title": {"text": y_label, "font": {"size": 11}},
            "type": "log" if log else "linear",
            "gridcolor": "#e4e4e4", "showgrid": True, "zeroline": False,
            "exponentformat": "power",
        },
        "legend": {
            "orientation": "h", "yanchor": "bottom",
            "y": 1.02, "xanchor": "left", "x": 0,
            "font": {"size": 11},
            "itemclick": "toggle", "itemdoubleclick": "toggleothers",
        },
        "margin": {"l": 65, "r": 20, "t": 80, "b": 55},
        "autosize": True,
        "plot_bgcolor": "#fafafa",
        "paper_bgcolor": "#ffffff",
        "hovermode": "x unified",
    }


# ---------------------------------------------------------------------------
# Figure builders
# ---------------------------------------------------------------------------

def _constant_trace(x: list, y: float, name: str, color: str, dash: str = "dash") -> dict:
    return {
        "type": "scatter",
        "x": x,
        "y": [y for _ in x],
        "name": name,
        "mode": "lines",
        "line": {"color": color, "width": 1.3, "dash": dash},
        "hovertemplate": "%{y:.4e}<extra>" + name + "</extra>",
    }


def _power_curve(d: dict[str, list[float]], signal_col: str, observed_col: str) -> list[float]:
    return d.get(signal_col) or d.get(observed_col, [])


def fig_fog_power(log: bool) -> dict:
    traces = []
    noise_floor_added = False
    for fname, label, key in [
        ("radiation_fog_power.csv", "辐射雾", "radiation_fog"),
        ("advection_fog_power.csv", "平流雾", "advection_fog"),
    ]:
        d = load_csv_display(fname)
        if d:
            traces.append(_trace(
                d["range_m"],
                _power_curve(d, "power_signal_raw", "power_observed_raw"),
                label,
                PAL[key],
            ))
            if not noise_floor_added and d.get("noise_floor_rms_W"):
                traces.append(_constant_trace(d["range_m"], d["noise_floor_rms_W"][0], "噪声底 RMS", "#666666"))
                noise_floor_added = True
    return {"data": traces,
            "layout": _layout("雾 — 回波功率 P(R)", "P(R)  (W)", log)}


def fig_haze_power(log: bool) -> dict:
    traces = []
    noise_floor_added = False
    for fname, label, key in [
        ("urban_industrial_haze_power.csv",  "城市/工业型霾",     "urban_industrial_haze"),
        ("rural_continental_haze_power.csv", "乡村/大陆背景型霾", "rural_continental_haze"),
        ("dust_desert_haze_power.csv",       "沙尘型霾",           "dust_desert_haze"),
        ("maritime_haze_power.csv",          "海洋性霾",            "maritime_haze"),
    ]:
        d = load_csv_display(fname)
        if d:
            traces.append(_trace(
                d["range_m"],
                _power_curve(d, "power_signal_raw", "power_observed_raw"),
                label,
                PAL[key],
            ))
            if not noise_floor_added and d.get("noise_floor_rms_W"):
                traces.append(_constant_trace(d["range_m"], d["noise_floor_rms_W"][0], "噪声底 RMS", "#666666"))
                noise_floor_added = True
    return {"data": traces,
            "layout": _layout("霾 — 回波功率 P(R)", "P(R)  (W)", log)}


def fig_haze_depol(log: bool = False) -> dict:  # noqa: ARG001  log ignored
    traces = []
    for fname, label, key in [
        ("urban_industrial_haze_depolarization.csv",  "城市/工业型霾",     "urban_industrial_haze"),
        ("rural_continental_haze_depolarization.csv", "乡村/大陆背景型霾", "rural_continental_haze"),
        ("dust_desert_haze_depolarization.csv",       "沙尘型霾",           "dust_desert_haze"),
        ("maritime_haze_depolarization.csv",          "海洋性霾",            "maritime_haze"),
    ]:
        d = load_csv_display(fname)
        if d:
            traces.append(_trace(d["range_m"], d["echo_depolarization_ratio"],
                                 label, PAL[key]))
    return {"data": traces,
            "layout": _layout("霾 — 回波退偏比 δ(R)", "δ(R)", False)}


def fig_rain_power(log: bool) -> dict:
    d = load_csv_display("rain_power.csv")
    traces = []
    if d:
        traces = [
            _trace(
                d["range_m"],
                _power_curve(d, "light_rain_power_signal_raw", "light_rain_power_observed_raw"),
                "小雨",
                PAL["light_rain"],
            ),
            _trace(
                d["range_m"],
                _power_curve(d, "moderate_rain_power_signal_raw", "moderate_rain_power_observed_raw"),
                "中雨",
                PAL["moderate_rain"],
            ),
            _trace(
                d["range_m"],
                _power_curve(d, "heavy_rain_power_signal_raw", "heavy_rain_power_observed_raw"),
                "大雨",
                PAL["heavy_rain"],
            ),
        ]
        if "light_rain_noise_floor_rms_W" in d:
            traces.append(_constant_trace(d["range_m"], d["light_rain_noise_floor_rms_W"][0], "噪声底 RMS", "#666666"))
    return {"data": traces,
            "layout": _layout("雨 — 回波功率 P(R)", "P(R)  (W)", log)}


_hial_csv_cache: dict[str, dict] = {}


def _load_hial_csv(fname: str) -> dict[str, list[float]]:
    if fname in _hial_csv_cache:
        return _hial_csv_cache[fname]
    p = HIAL_OUT_DIR / "data" / fname
    if not p.exists():
        return {}
    try:
        with p.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            result: dict[str, list[float]] = {}
            for row in reader:
                for k, v in row.items():
                    try:
                        result.setdefault(k, []).append(float(v))
                    except (ValueError, TypeError):
                        pass
        _hial_csv_cache[fname] = result
        return result
    except Exception:
        return {}


def _load_hial_summary() -> dict:
    p = HIAL_OUT_DIR / "summary.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _invalidate_hial_cache() -> None:
    _hial_csv_cache.clear()


def _downsample(data: dict[str, list[float]], max_pts: int = 2000) -> dict[str, list[float]]:
    if not data:
        return data
    n = len(next(iter(data.values())))
    if n <= max_pts:
        return data
    step = max(1, n // max_pts)
    return {k: v[::step] for k, v in data.items()}


def fig_hial_power(log: bool) -> dict:
    d = _downsample(_load_hial_csv("high_altitude_aerosol_power.csv"))
    traces = []
    if d:
        y = _power_curve(d, "power_signal_raw", "power_observed_raw")
        traces.append(_trace(d["range_m"], y, "高空气溶胶", "#7b2d8b"))
        if d.get("noise_floor_rms_W"):
            traces.append(_constant_trace(d["range_m"], d["noise_floor_rms_W"][0], "噪声底 RMS", "#666666"))
    return {"data": traces,
            "layout": _layout("高空低气溶胶浓度 — 回波功率 P(R)", "P(R)  (W)", log)}


def fig_hial_snr() -> dict:
    d = _downsample(_load_hial_csv("high_altitude_aerosol_power.csv"))
    traces = []
    if d and d.get("snr_db"):
        traces.append(_trace(d["range_m"], d["snr_db"], "高空气溶胶 SNR", "#7b2d8b"))
        traces.append(_constant_trace(d["range_m"], 10.0, "SNR = 10 dB", "#ff9900", dash="dash"))
        traces.append(_constant_trace(d["range_m"], 4.77, "SNR = 3 线性", "#cc3333", dash="dot"))
    return {"data": traces,
            "layout": _layout("高空低气溶胶浓度 — 信噪比 SNR(R)", "SNR  (dB)", False)}


def fig_layered_power(log: bool) -> dict:
    traces = []
    if d:
        traces.append(_trace(
            d["range_m"],
            _power_curve(d, "power_signal_raw", "power_observed_raw"),
            "分层大气",
            "#8b1e3f",
        ))
        if d.get("noise_floor_rms_W"):
            traces.append(_constant_trace(d["range_m"], d["noise_floor_rms_W"][0], "噪声底 RMS", "#666666"))
    return {"data": traces,
            "layout": _layout("分层大气 — 回波功率 P(R)", "P(R)  (W)", log)}


def fig_layered_beta(log: bool) -> dict:
    d = load_csv_display("layered_atmosphere_profile.csv")
    traces = []
    if d:
        for col, label, color, dash in [
            ("beta_molecular_m_inv_sr", "分子后向散射", "#3366cc", "dot"),
            ("beta_aerosol_m_inv_sr", "气溶胶后向散射", "#cc3333", "dash"),
            ("beta_total_m_inv_sr", "总后向散射", "#228833", "solid"),
        ]:
            if col in d:
                traces.append(_trace(d["range_m"], d[col], label, color, dash))
    return {"data": traces,
            "layout": _layout("分层大气 — 后向散射系数 β(R)", "β(R)  (m⁻¹sr⁻¹)", log)}


def fig_layered_alpha(log: bool) -> dict:
    d = load_csv_display("layered_atmosphere_profile.csv")
    traces = []
    if d:
        for col, label, color, dash in [
            ("alpha_molecular_m_inv", "分子消光", "#3366cc", "dot"),
            ("alpha_aerosol_m_inv", "气溶胶消光", "#cc3333", "dash"),
            ("alpha_total_m_inv", "总消光", "#228833", "solid"),
        ]:
            if col in d:
                traces.append(_trace(d["range_m"], d[col], label, color, dash))
    return {"data": traces,
            "layout": _layout("分层大气 — 消光系数 α(R)", "α(R)  (m⁻¹)", log)}


def _threshold_trace(x: list[float], y: float, name: str, color: str) -> dict:
    if not x:
        return {}
    return {
        "type": "scatter",
        "x": x,
        "y": [y for _ in x],
        "name": name,
        "mode": "lines",
        "line": {"color": color, "width": 1.2, "dash": "dash"},
        "hovertemplate": "%{y:.2f} dB<extra>" + name + "</extra>",
    }


def fig_all_snr(log: bool = False) -> dict:  # noqa: ARG001
    traces = []
    x_ref: list[float] = []
    for fname, label, key, dash in [
        ("radiation_fog_power.csv",          "辐射雾",          "radiation_fog",          "solid"),
        ("advection_fog_power.csv",          "平流雾",          "advection_fog",          "dot"),
        ("urban_industrial_haze_power.csv",  "城市/工业型霾",   "urban_industrial_haze",  "solid"),
        ("rural_continental_haze_power.csv", "乡村/大陆背景型霾", "rural_continental_haze", "dash"),
        ("dust_desert_haze_power.csv",       "沙尘型霾",        "dust_desert_haze",       "dashdot"),
        ("maritime_haze_power.csv",          "海洋性霾",        "maritime_haze",          "dot"),
    ]:
        d = load_csv_display(fname)
        if d and "snr_db" in d:
            x_ref = d["range_m"]
            traces.append(_trace(d["range_m"], d["snr_db"], label, PAL[key], dash))
    d = load_csv_display("rain_power.csv")
    if d:
        x_ref = d.get("range_m", x_ref)
        for key, label, dash in [
            ("light_rain", "小雨", "solid"),
            ("moderate_rain", "中雨", "dash"),
            ("heavy_rain", "大雨", "dashdot"),
        ]:
            col = f"{key}_snr_db"
            if col in d:
                traces.append(_trace(d["range_m"], d[col], label, PAL[key], dash))
    d = load_csv_display("layered_atmosphere_power.csv")
    if d and "snr_db" in d:
        x_ref = d.get("range_m", x_ref)
        traces.append(_trace(d["range_m"], d["snr_db"], "分层大气", PAL["layered_atmosphere"], "solid"))
    for threshold, name in ((20.0 * math.log10(3.0), "SNR=3"), (20.0 * math.log10(10.0), "SNR=10")):
        t = _threshold_trace(x_ref, threshold, name, "#555555")
        if t:
            traces.append(t)
    base = _layout("全场景 — 信噪比 SNR(R)", "SNR (dB)", False)
    base["margin"] = {"l": 65, "r": 20, "t": 80, "b": 55}
    return {"data": traces, "layout": base}


def fig_all_power(log: bool) -> dict:
    traces = []
    for fname, label, key, dash in [
        ("radiation_fog_power.csv",          "辐射雾",         "radiation_fog",          "solid"),
        ("advection_fog_power.csv",          "平流雾",          "advection_fog",          "dot"),
        ("urban_industrial_haze_power.csv",  "城市/工业型霾",   "urban_industrial_haze",  "solid"),
        ("rural_continental_haze_power.csv", "乡村/大陆背景型霾","rural_continental_haze", "dash"),
        ("dust_desert_haze_power.csv",       "沙尘型霾",        "dust_desert_haze",       "dashdot"),
        ("maritime_haze_power.csv",          "海洋性霾",         "maritime_haze",          "dot"),
    ]:
        d = load_csv_display(fname)
        if d:
            traces.append(_trace(
                d["range_m"],
                _power_curve(d, "power_signal_raw", "power_observed_raw"),
                label,
                PAL[key],
                dash,
            ))
    d = load_csv_display("rain_power.csv")
    if d:
        traces += [
            _trace(
                d["range_m"],
                _power_curve(d, "light_rain_power_signal_raw", "light_rain_power_observed_raw"),
                "小雨",
                PAL["light_rain"],
                "solid",
            ),
            _trace(
                d["range_m"],
                _power_curve(d, "moderate_rain_power_signal_raw", "moderate_rain_power_observed_raw"),
                "中雨",
                PAL["moderate_rain"],
                "dash",
            ),
            _trace(
                d["range_m"],
                _power_curve(d, "heavy_rain_power_signal_raw", "heavy_rain_power_observed_raw"),
                "大雨",
                PAL["heavy_rain"],
                "dashdot",
            ),
        ]
    d = load_csv_display("layered_atmosphere_power.csv")
    if d and "power_signal_raw" in d:
        traces.append(_trace(
            d["range_m"],
            _power_curve(d, "power_signal_raw", "power_observed_raw"),
            "分层大气",
            PAL["layered_atmosphere"],
            "solid",
        ))
    base = _layout("全场景 — 回波功率对比", "P(R)  (W)", log)
    base["margin"] = {"l": 65, "r": 20, "t": 80, "b": 55}
    return {"data": traces, "layout": base}


def fig_all_depol(log: bool = False) -> dict:  # noqa: ARG001
    traces = []
    for fname, label, key in [
        ("urban_industrial_haze_depolarization.csv",  "城市/工业型霾",     "urban_industrial_haze"),
        ("rural_continental_haze_depolarization.csv", "乡村/大陆背景型霾", "rural_continental_haze"),
        ("dust_desert_haze_depolarization.csv",       "沙尘型霾",           "dust_desert_haze"),
        ("maritime_haze_depolarization.csv",          "海洋性霾",            "maritime_haze"),
    ]:
        d = load_csv_display(fname)
        if d:
            traces.append(_trace(d["range_m"], d["echo_depolarization_ratio"],
                                 label, PAL[key]))
    base = _layout("全场景 — 退偏比对比", "δ(R)", False)
    base["margin"] = {"l": 65, "r": 20, "t": 80, "b": 55}
    return {"data": traces, "layout": base}


# ---------------------------------------------------------------------------
# Numerical summary table
# ---------------------------------------------------------------------------

def _fsc(v) -> str:
    return f"{v:.4e}" if isinstance(v, float) else "—"


def _ff(v, digits: int = 4) -> str:
    return f"{v:.{digits}f}" if isinstance(v, float) else "—"


def _summary_table_payload(summary: dict, cat: str,
                           keys: list[tuple[str, str]],
                           show_depol: bool,
                           show_snr: bool = False) -> tuple[list[dict], list[dict]]:
    if not keys:
        return [], []
    cat_data = summary.get(cat, {})
    col_names = [
        "场景",
        "α_p  (m⁻¹)",
        "β_p  (m⁻¹sr⁻¹)",
        "S = α/β  (sr)",
    ]
    if show_snr:
        col_names += ["SNR@1km (dB)", "R(SNR≥3)", "R(SNR≥10)"]
    if show_depol:
        col_names.append("退偏比  δ")

    rows = []
    for key, label in keys:
        sc  = cat_data.get(key, {})
        α   = sc.get("alpha_particle")
        β   = sc.get("beta_particle")
        S   = (α / β) if (α is not None and β is not None and β != 0.0) else None
        row = {
            "场景":            label,
            "α_p  (m⁻¹)":     _fsc(α),
            "β_p  (m⁻¹sr⁻¹)": _fsc(β),
            "S = α/β  (sr)":   _ff(S),
        }
        if show_snr:
            snr = sc.get("snr_summary", {}) if isinstance(sc.get("snr_summary"), dict) else {}
            row["SNR@1km (dB)"] = _ff(snr.get("snr_db_at_1000m"), digits=2)
            row["R(SNR≥3)"] = f"{snr.get('max_range_snr_ge_3'):.0f} m" if isinstance(snr.get("max_range_snr_ge_3"), float) else "—"
            row["R(SNR≥10)"] = f"{snr.get('max_range_snr_ge_10'):.0f} m" if isinstance(snr.get("max_range_snr_ge_10"), float) else "—"
        if show_depol:
            row["退偏比  δ"] = _ff(sc.get("depol_ratio"))
        rows.append(row)

    columns = [{"name": c, "label": c, "field": c, "align": "left"}
               for c in col_names]
    return columns, rows


def render_summary_table(summary: dict, cat: str,
                         keys: list[tuple[str, str]],
                         show_depol: bool,
                         show_snr: bool = False):
    columns, rows = _summary_table_payload(summary, cat, keys, show_depol, show_snr)
    return ui.table(
        columns=columns,
        rows=rows,
    ).classes("w-full text-xs").props("dense flat bordered separator=cell")


def refresh_summary_table(table, summary: dict, cat: str,
                          keys: list[tuple[str, str]],
                          show_depol: bool,
                          show_snr: bool = False) -> None:
    columns, rows = _summary_table_payload(summary, cat, keys, show_depol, show_snr)
    table.columns = columns
    table.rows = rows
    table.update()


# ---------------------------------------------------------------------------
# Reusable chart tab builder
# ---------------------------------------------------------------------------

def chart_tab(
    *,
    fig_builder,
    allow_log: bool,
    default_log: bool,
    summary: dict,
    cat: str,
    summary_keys: list[tuple[str, str]],
    show_depol: bool,
    csv_links: list[tuple[str, str]],
    ref_images: list[tuple[str, str]],
    callbacks: list | None = None,
) -> None:
    """Render one tab's complete content area."""

    log_state = [default_log and allow_log]

    with ui.column().classes("w-full gap-4"):

        # ── toolbar ───────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.label("坐标轴：").classes("text-xs text-gray-500")
            if allow_log:
                scale_toggle = ui.toggle(
                    {"log": "对数", "lin": "线性"},
                    value="log" if default_log else "lin",
                ).props("dense").classes("text-xs")
            ui.space()
            ui.label("图例单击切换曲线显示 / 双击单独显示").classes(
                "text-xs text-gray-400 italic"
            )

        # ── plotly chart ──────────────────────────────────────────────────
        with ui.element("div").classes("w-full").style("min-height:420px; overflow:hidden"):
            plotly_elem = ui.plotly(fig_builder(log_state[0])).classes("w-full h-full")

        def redraw() -> None:
            plotly_elem.update_figure(fig_builder(log_state[0]))

        if allow_log:
            def on_scale(e) -> None:
                log_state[0] = (e.value == "log")
                redraw()
            scale_toggle.on_value_change(on_scale)

        if callbacks is not None:
            callbacks.append(redraw)

        # ── numerical summary ─────────────────────────────────────────────
        with ui.expansion("数值摘要", icon="table_chart",
                          value=True).classes("w-full"):
            summary_table = render_summary_table(summary, cat, summary_keys, show_depol)

        def redraw_summary() -> None:
            refresh_summary_table(
                summary_table,
                load_summary(),
                cat,
                summary_keys,
                show_depol,
            )

        if callbacks is not None:
            callbacks.append(redraw_summary)

        # ── reference images ──────────────────────────────────────────────
        if ref_images:
            with ui.expansion("原始图像参考", icon="image",
                              value=False).classes("w-full"):
                with ui.column().classes("w-full gap-4"):
                    for label, slug in ref_images:
                        with ui.column().classes("w-full gap-1"):
                            ui.label(label).classes("text-xs text-gray-500 font-medium")
                            img_url = f"/export/img/{slug}/png"
                            png_in_active = _FIGURES() / f"{slug}.png"
                            if png_in_active.exists():
                                ui.image(img_url).classes("w-full rounded border")
                            else:
                                ui.label("（图像尚未生成）").classes("text-xs text-yellow-600")
                            with ui.row().classes("gap-2 items-center"):
                                ui.label("下载：").classes("text-sm text-gray-400")
                                if png_in_active.exists():
                                    async def _dl_png(_slug=slug) -> None:
                                        p = _FIGURES() / f"{_slug}.png"
                                        if p.exists():
                                            await _native_save(p.read_bytes(), f"{_slug}.png",
                                                               [("PNG image", "*.png")])
                                    ui.button("PNG", on_click=_dl_png).props(
                                        "dense flat"
                                    ).classes("text-sm text-blue-600")
                                svg_path = _FIGURES() / f"{slug}.svg"
                                if svg_path.exists():
                                    async def _dl_svg(_slug=slug) -> None:
                                        p = _FIGURES() / f"{_slug}.svg"
                                        if p.exists():
                                            await _native_save(p.read_bytes(), f"{_slug}.svg",
                                                               [("SVG image", "*.svg")])
                                    ui.button("SVG", on_click=_dl_svg).props(
                                        "dense flat"
                                    ).classes("text-sm text-blue-600")

        # ── CSV downloads ─────────────────────────────────────────────────
        if csv_links:
            with ui.row().classes("items-center gap-3 flex-wrap pt-1"):
                ui.label("数据下载：").classes("text-sm text-gray-500 font-medium")
                for label, export_name in csv_links:
                    curve = _EXPORT_CURVES.get(export_name)
                    if curve:
                        src_csv = curve[0]
                        if (_DATA_DIR() / src_csv).exists():
                            async def _dl_csv(_name=export_name) -> None:
                                data = _build_csv_bytes(_name, _DATA_DIR())
                                if data is not None:
                                    await _native_save(data, _name, [("CSV file", "*.csv")])
                            ui.button(f"↓ {label}", on_click=_dl_csv).props(
                                    "dense flat"
                            ).classes("text-sm font-mono text-green-700")


async def _do_hial_compute(
    status_label,
    compute_btn,
    hial_callbacks: list,
) -> None:
    global _hial_running, _hial_current_proc
    if _hial_running:
        return
    _hial_running = True
    _safe_call(compute_btn.disable)
    _safe_call(status_label.set_text, "计算中…")
    _safe_call(status_label.classes, remove="text-green-600 text-red-600", add="text-blue-600")

    overrides = _collect_overrides()
    hial_cfg  = overrides.get("high_altitude_aerosol", {})
    H_m       = float(hial_cfg.get("height_m", 20000.0))
    n0_cm3    = float(hial_cfg.get("n0_cm3",   7.245788e-1))
    profile   = overrides.get("profile", {})
    noise_cfg = overrides.get("instrument", {}).get("receiver_noise", {})
    sys_c     = overrides.get("cli", {}).get("system-constant", None)

    from path_resolver import resolve_mie_python_executable
    mie_python = resolve_mie_python_executable()

    cmd = [
        mie_python, str(HIAL_SCRIPT),
        "--output", str(HIAL_OUT_DIR),
        "--height-m", str(H_m),
        "--n0-cm3", str(n0_cm3),
    ]
    if sys_c is not None:
        cmd += ["--system-constant", str(sys_c)]
    if profile:
        cmd += ["--profile-json", json.dumps(profile)]
    if noise_cfg:
        cmd += ["--noise-overrides", json.dumps(noise_cfg)]

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs: dict = {"cwd": str(ROOT), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        _hial_current_proc = proc
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                _safe_call(status_label.set_text, line[-80:])
        await proc.wait()
        rc = proc.returncode
    except Exception as exc:
        _safe_call(status_label.set_text, f"子进程异常: {exc}")
        _safe_call(status_label.classes, remove="text-blue-600", add="text-red-600")
        _hial_running = False
        _safe_call(compute_btn.enable)
        return
    finally:
        _hial_current_proc = None

    if rc == 0:
        _invalidate_hial_cache()
        for cb in hial_callbacks:
            try:
                cb()
            except Exception:
                pass
        _safe_call(status_label.set_text, f"计算完成  H={H_m:.0f}m  n₀={n0_cm3:.3e} cm⁻³")
        _safe_call(status_label.classes, remove="text-blue-600 text-red-600", add="text-green-600")
    else:
        _safe_call(status_label.set_text, f"计算失败（返回码 {rc}）")
        _safe_call(status_label.classes, remove="text-blue-600", add="text-red-600")

    _hial_running = False
    _safe_call(compute_btn.enable)


def highalt_tab() -> None:
    global _hial_callbacks
    hial_cbs: list = []

    with ui.column().classes("w-full gap-4"):
        # ── 控制栏 ────────────────────────────────────────────────────────
        with ui.card().classes("w-full p-3 shadow-none border bg-gray-50"):
            with ui.row().classes("items-center gap-3 flex-wrap"):
                compute_btn = ui.button("▶ 计算", icon="play_arrow").props("dense").classes(
                    "text-sm font-semibold bg-purple-700 text-white"
                )
                status_lbl = ui.label("就绪 — 在左侧输入 H 和 n₀ 后点击计算").classes(
                    "text-xs text-gray-500"
                )
            with ui.row().classes("items-start gap-1 mt-1"):
                ui.icon("info", size="xs").classes("text-gray-400 mt-0.5")
                ui.label(
                    "均匀层模型：P(R) 与 SNR 的物理意义与分层大气不同，不可直接类比"
                ).classes("text-xs text-gray-400")

        # ── 功率曲线 ─────────────────────────────────────────────────────
        log_state = [True]
        with ui.card().classes("w-full p-4 shadow-none border"):
            with ui.row().classes("items-center gap-3 mb-2 flex-wrap"):
                ui.label("回波功率 P(R)").classes("font-semibold text-sm text-gray-700")
                ui.space()
                sc_tog = ui.toggle({"log": "对数", "lin": "线性"}, value="log").props("dense").classes("text-xs")
            power_plot = ui.plotly(fig_hial_power(True)).classes("w-full").style("min-height:380px; overflow:hidden")

            def _on_scale(e) -> None:
                log_state[0] = (e.value == "log")
                power_plot.update_figure(fig_hial_power(log_state[0]))
            sc_tog.on_value_change(_on_scale)

        # ── SNR 曲线 ──────────────────────────────────────────────────────
        with ui.card().classes("w-full p-4 shadow-none border"):
            ui.label("信噪比 SNR(R)").classes("font-semibold text-sm text-gray-700 mb-2")
            snr_plot = ui.plotly(fig_hial_snr()).classes("w-full").style("min-height:320px; overflow:hidden")

        # ── 数值摘要 ──────────────────────────────────────────────────────
        with ui.expansion("数值摘要", icon="table_chart", value=True).classes("w-full"):
            summary_container = ui.column().classes("w-full")

            def _render_hial_summary() -> None:
                summary_container.clear()
                sm = _load_hial_summary().get("high_altitude_aerosol", {})
                if not sm:
                    with summary_container:
                        ui.label("暂无数据，请先点击「计算」").classes("text-xs text-gray-400 p-2")
                    return
                inp   = sm.get("input", {})
                opt   = sm.get("optical", {})
                ref   = sm.get("layered_reference", {})
                snrs  = sm.get("snr_summary", {})

                def _ff(v, d=4):
                    return f"{v:.{d}e}" if isinstance(v, float) else "—"
                def _f2(v):
                    return f"{v:.2f}" if isinstance(v, float) else "—"

                cols = [
                    {"name": "项目", "label": "项目", "field": "项目", "align": "left"},
                    {"name": "Mie计算值", "label": "Mie 计算值", "field": "Mie计算值", "align": "right"},
                    {"name": "分层大气参考", "label": "分层大气 H 处参考", "field": "分层大气参考", "align": "right"},
                ]
                rows = [
                    {"项目": f"H (m)",             "Mie计算值": f"{inp.get('height_m', 0):.0f}",         "分层大气参考": "—"},
                    {"项目": "n₀ (cm⁻³)",          "Mie计算值": _ff(inp.get("n0_cm3")),                  "分层大气参考": "—"},
                    {"项目": "α_p (m⁻¹)",          "Mie计算值": _ff(opt.get("alpha_particle")),          "分层大气参考": _ff(ref.get("alpha_aerosol_at_H"))},
                    {"项目": "β_p (m⁻¹sr⁻¹)",     "Mie计算值": _ff(opt.get("beta_particle")),           "分层大气参考": _ff(ref.get("beta_aerosol_at_H"))},
                    {"项目": "S = α/β (sr)",       "Mie计算值": _f2(opt.get("S_mie_sr")),               "分层大气参考": f"{ref.get('S_assumed_sr', 50):.0f}"},
                    {"项目": "β 偏差 (%)",         "Mie计算值": f"{ref.get('beta_deviation_pct', 0):+.2f}%", "分层大气参考": "0%"},
                    {"项目": "SNR@1km (dB)",        "Mie计算值": _f2(snrs.get("snr_db_at_1000m")),        "分层大气参考": "—"},
                    {"项目": "SNR@2km (dB)",        "Mie计算值": _f2(snrs.get("snr_db_at_2000m")),        "分层大气参考": "—"},
                    {"项目": "R(SNR≥10) (m)",       "Mie计算值": f"{snrs.get('max_range_snr_ge_10') or '—'}",  "分层大气参考": "—"},
                ]
                with summary_container:
                    ui.table(columns=cols, rows=rows).classes("w-full text-xs").props("dense flat bordered separator=cell")

            _render_hial_summary()

        # ── CSV 下载 ──────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-3 flex-wrap pt-1"):
            ui.label("数据下载：").classes("text-sm text-gray-500 font-medium")
            async def _dl_hial_csv() -> None:
                p = HIAL_OUT_DIR / "data" / "high_altitude_aerosol_power.csv"
                if p.exists():
                    await _native_save(p.read_bytes(), "high_altitude_aerosol_power.csv", [("CSV file", "*.csv")])
                else:
                    ui.notify("数据文件不存在，请先计算", type="warning")
            ui.button("↓ 功率/SNR", on_click=_dl_hial_csv).props("dense flat").classes("text-sm font-mono text-green-700")

        # ── 刷新回调注册 ──────────────────────────────────────────────────
        def _redraw_hial() -> None:
            power_plot.update_figure(fig_hial_power(log_state[0]))
            snr_plot.update_figure(fig_hial_snr())
            _render_hial_summary()

        hial_cbs.append(_redraw_hial)
        _hial_callbacks[:] = hial_cbs

        # ── 按钮绑定 ──────────────────────────────────────────────────────
        async def _on_compute() -> None:
            await _do_hial_compute(status_lbl, compute_btn, hial_cbs)

        compute_btn.on_click(_on_compute)


def layered_tab(callbacks: list | None = None) -> None:
    with ui.column().classes("w-full gap-4"):
        summary = load_summary().get("layered_atmosphere", {})
        model = summary.get("profile_model", {})
        with ui.card().classes("w-full p-4 shadow-none border"):
            ui.label("分层大气回波").classes("font-semibold text-sm text-gray-700 mb-2")
            if model:
                desc = (
                    f"近地衰减高度 = {model.get('aerosol_boundary_scale_height_m', 0):.0f} m；"
                    f"高空层中心 = {model.get('aerosol_layer_center_m', 0):.0f} m；"
                    f"高空层厚度 = {model.get('aerosol_layer_width_m', 0):.0f} m"
                )
                ui.label(desc).classes("text-xs text-gray-500 mb-2")
            layered_power_plot = ui.plotly(fig_layered_power(True)).classes("w-full").style("min-height:420px; overflow:hidden")

        with ui.card().classes("w-full p-4 shadow-none border"):
            ui.label("后向散射系数剖面").classes("font-semibold text-sm text-gray-700 mb-2")
            layered_beta_plot = ui.plotly(fig_layered_beta(True)).classes("w-full").style("min-height:420px; overflow:hidden")

        with ui.card().classes("w-full p-4 shadow-none border"):
            ui.label("消光系数剖面").classes("font-semibold text-sm text-gray-700 mb-2")
            layered_alpha_plot = ui.plotly(fig_layered_alpha(True)).classes("w-full").style("min-height:420px; overflow:hidden")

        def redraw_layered() -> None:
            layered_power_plot.update_figure(fig_layered_power(True))
            layered_beta_plot.update_figure(fig_layered_beta(True))
            layered_alpha_plot.update_figure(fig_layered_alpha(True))

        if callbacks is not None:
            callbacks.append(redraw_layered)

        with ui.row().classes("items-center gap-3 flex-wrap pt-1"):
            ui.label("数据下载：").classes("text-sm text-gray-500 font-medium")
            for label, export_name in [
                ("分层回波", "fig12_LayeredAtmosphere.csv"),
                ("总后向散射", "fig13_LayeredBeta.csv"),
                ("总消光", "fig14_LayeredAlpha.csv"),
            ]:
                async def _dl_csv(_name=export_name) -> None:
                    data = _build_csv_bytes(_name, _DATA_DIR())
                    if data is not None:
                        await _native_save(data, _name, [("CSV file", "*.csv")])
                    else:
                        ui.notify("数据文件不存在，请先重算", type="warning")
                ui.button(f"↓ {label}", on_click=_dl_csv).props(
                    "dense flat"
                ).classes("text-sm font-mono text-green-700")

        with ui.row().classes("items-center gap-3 flex-wrap pt-1"):
            ui.label("图像下载：").classes("text-sm text-gray-500 font-medium")
            for label, slug in [
                ("回波", "fig12_layered_atmosphere_power"),
                ("后向散射", "fig13_layered_atmosphere_beta_profile"),
                ("消光", "fig14_layered_atmosphere_alpha_profile"),
            ]:
                async def _dl_png(_slug=slug) -> None:
                    p = _FIGURES() / f"{_slug}.png"
                    if p.exists():
                        await _native_save(p.read_bytes(), f"{_slug}.png", [("PNG image", "*.png")])
                    else:
                        ui.notify("图像文件不存在，请先重算", type="warning")
                async def _dl_svg(_slug=slug) -> None:
                    p = _FIGURES() / f"{_slug}.svg"
                    if p.exists():
                        await _native_save(p.read_bytes(), f"{_slug}.svg", [("SVG image", "*.svg")])
                    else:
                        ui.notify("图像文件不存在，请先重算", type="warning")
                with ui.row().classes("gap-1 items-center"):
                    ui.label(label).classes("text-sm text-gray-500")
                    ui.button("PNG", on_click=_dl_png).props("dense flat").classes("text-sm text-blue-600")
                    ui.button("SVG", on_click=_dl_svg).props("dense flat").classes("text-sm text-blue-600")


# ---------------------------------------------------------------------------
# Left panel — editable parameter state
# ---------------------------------------------------------------------------

# Global mutable state: all editable parameters collected from ui.number/ui.input
# widgets. Keyed as (section, key, field) or (section, key, mode_name, field).
_inputs: dict[tuple, "ui.number"] = {}
_input_impact_labels: dict[tuple, object] = {}

_SCENE_LABELS = {
    "radiation_fog": "辐射雾",
    "advection_fog": "平流雾",
    "urban_industrial_haze": "城市/工业型霾",
    "rural_continental_haze": "乡村/大陆背景型霾",
    "dust_desert_haze": "沙尘型霾",
    "maritime_haze": "海洋性霾",
    "light_rain": "小雨",
    "moderate_rain": "中雨",
    "heavy_rain": "大雨",
}

_FIELD_LABELS = {
    "wavelength_nm": "波长",
    "range_max_m": "最大探测距离",
    "range_step_m": "距离步长",
    "laser_peak_power_W": "峰值功率",
    "pulse_width_s": "脉宽",
    "receiver_radius_m": "接收半径",
    "optical_efficiency": "光学效率",
    "alpha_mol": "分子消光",
    "beta_mol": "分子后向散射",
    "molecular-depol-ratio": "分子退偏比",
    "n0_cm3": "数量浓度",
    "rg_um": "几何均值半径",
    "sigma_g": "几何标准差",
    "m_real": "折射率实部",
    "m_imag": "折射率虚部",
    "rain_rate_mm_h": "降雨率",
    "enabled": "启用噪声",
    "quantum_efficiency": "量子效率",
    "background_power_W": "背景光功率",
    "average_pulses": "平均脉冲数",
    "generate_noisy_curve": "带噪曲线",
    "random_seed": "随机种子",
    "mode": "大气模式",
    "aerosol_boundary_beta0_m_inv_sr": "近地散射强度",
    "aerosol_boundary_scale_height_m": "近地衰减高度",
    "aerosol_layer_beta0_m_inv_sr": "高空层强度",
    "aerosol_layer_center_m": "高空层中心",
    "aerosol_layer_width_m": "高空层厚度",
    "aerosol_lidar_ratio_sr": "气溶胶激光雷达比",
}


def _trigger_plan_refresh() -> None:
    if _identity_refresh_current:
        try:
            _identity_refresh_current[0]()
        except Exception:
            pass


def _impact_scope_text(key: tuple) -> str:
    if key[0] == "cli":
        field = key[1]
        if field in {"laser_peak_power_W", "pulse_width_s", "receiver_radius_m", "optical_efficiency"}:
            return "影响：仅输出重建"
        if field in {"range_max_m", "range_step_m"}:
            return "影响：仅输出重建与距离坐标"
        if field == "wavelength_nm":
            return "影响：雾/霾/雨光学与高耗时散射"
        if field == "alpha_mol":
            return "影响：雾/霾/雨光学"
        if field == "beta_mol":
            return "影响：雾/霾/雨光学与通道分解"
        if field == "molecular-depol-ratio":
            return "影响：分子通道分解与退偏结果"
    elif key[0] == "fog":
        return "影响：雾光学"
    elif key[0] == "rain":
        return "影响：雨光学"
    elif key[0] == "haze":
        return "影响：霾光学、Mueller 与高耗时散射"
    elif key[0] == "noise":
        return "影响：噪声底与 SNR"
    elif key[0] == "profile":
        return "影响：分层大气剖面与新回波曲线"
    elif key[0] == "high_altitude_aerosol":
        return "影响：高空低气溶胶浓度场景"
    return ""


def _beta_mol_input_from_global(g: dict) -> float | None:
    value = g.get("beta_mol_input_m_inv_sr")
    if value is None:
        value = g.get("beta_mol_m_inv_sr_inv_reference")
    return value


def _compact_num(value, *, sig: int = 6) -> str:
    """Format front-end numbers compactly, keeping scientific notation readable."""
    try:
        v = float(value)
    except Exception:
        return str(value)
    if not math.isfinite(v):
        return str(value)
    text = f"{v:.{sig}g}"
    if "e" not in text.lower():
        return text
    mantissa, exponent = text.lower().split("e", 1)
    sign = "-" if exponent.startswith("-") else ""
    digits = exponent.lstrip("+-").lstrip("0") or "0"
    return f"{mantissa}e{sign}{digits}"


def _state_key_label(key: tuple) -> str:
    section = key[0]
    if section == "cli":
        return _FIELD_LABELS.get(key[1], str(key[1]))
    if section in ("fog", "rain"):
        _, scenario, field = key
        return f"{_SCENE_LABELS.get(scenario, scenario)} / {_FIELD_LABELS.get(field, field)}"
    if section == "haze":
        _, scenario, mode_name, field = key
        return f"{_SCENE_LABELS.get(scenario, scenario)} / {mode_name} / {_FIELD_LABELS.get(field, field)}"
    if section == "noise":
        return f"噪声 / {_FIELD_LABELS.get(key[1], str(key[1]))}"
    if section == "profile":
        return f"分层大气 / {_FIELD_LABELS.get(key[1], str(key[1]))}"
    return " / ".join(str(part) for part in key)


def _num(label: str, value: float, state_key: tuple, *,
         step: float | None = None, fmt: str = "%.6g") -> None:
    """Render a labeled number input and register it in _inputs."""
    display_value = value
    if isinstance(value, (int, float)):
        display_value = float(value)
    with ui.column().classes("w-full gap-0"):
        with ui.row().classes("items-baseline gap-1 w-full").style("overflow:hidden"):
            ui.html(
                f"<span style='display:inline-block;width:112px;min-width:112px;"
                f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
                f"font-size:0.875rem;color:#6b7280;flex-shrink:0'>{label}</span>"
            )
            inp = ui.number(value=display_value, format=fmt, step=step or 0).classes(
                "text-sm font-mono flex-1"
            ).props("dense outlined hide-bottom-space")
        impact_label = ui.label("").classes("text-[11px] text-gray-400 pl-28 hidden")
    _inputs[state_key] = inp
    _input_impact_labels[state_key] = impact_label
    inp.on_value_change(lambda _e: _trigger_plan_refresh())


def _switch(label: str, value: bool, state_key: tuple) -> None:
    with ui.row().classes("items-center justify-between w-full").style("overflow:hidden"):
        ui.html(
            f"<span style='display:inline-block;flex:1;min-width:0;"
            f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
            f"font-size:0.875rem;color:#6b7280'>{label}</span>"
        )
        inp = ui.switch(value=bool(value)).props("dense")
    impact_label = ui.label("").classes("text-[11px] text-gray-400 pl-28 hidden")
    _inputs[state_key] = inp
    _input_impact_labels[state_key] = impact_label
    inp.on_value_change(lambda _e: _trigger_plan_refresh())


def _choice(label: str, value: str, state_key: tuple, options: dict[str, str]) -> None:
    with ui.column().classes("w-full gap-0"):
        with ui.row().classes("items-baseline gap-1 w-full").style("overflow:hidden"):
            ui.html(
                f"<span style='display:inline-block;width:112px;min-width:112px;"
                f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
                f"font-size:0.875rem;color:#6b7280;flex-shrink:0'>{label}</span>"
            )
            inp = ui.select(options=options, value=value).classes(
                "text-sm flex-1"
            ).props("dense outlined options-dense")
        impact_label = ui.label("").classes("text-[11px] text-gray-400 pl-28 hidden")
    _inputs[state_key] = inp
    _input_impact_labels[state_key] = impact_label
    def _on_change(_e) -> None:
        if state_key == ("profile", "mode") and str(inp.value) == "ideal_layered":
            range_max_inp = _inputs.get(("cli", "range_max_m"))
            if range_max_inp is not None and range_max_inp.value is not None and float(range_max_inp.value) <= 2000.0:
                range_max_inp.value = 40000.0
        _trigger_plan_refresh()
    inp.on_value_change(_on_change)


def _build_instrument_editor(g: dict) -> None:
    inst = g.get("instrument_parameters", {})
    _num("λ  (nm)",     g.get("wavelength_nm", 1550.0),         ("cli", "wavelength_nm"))
    _num("P₀  (W)",     inst.get("laser_peak_power_W", 50.0),   ("cli", "laser_peak_power_W"))
    _num("τ  (s)",      inst.get("pulse_width_s", 2e-7),        ("cli", "pulse_width_s"), fmt="%.3e")
    _num("r  (m)",      inst.get("receiver_radius_m", 0.05),    ("cli", "receiver_radius_m"), fmt="%.4f")
    _num("η",           inst.get("optical_efficiency", 0.8),    ("cli", "optical_efficiency"), fmt="%.4f")
    ui.separator().classes("my-2")
    ui.label("接收端噪声 / SNR").classes("text-xs font-semibold text-gray-500")
    _build_noise_editor(g)


def _build_global_editor(g: dict) -> None:
    _num("R<sub>max</sub> (m)", g.get("range_max_m", 2000.0), ("cli", "range_max_m"), fmt="%.0f")
    _num("ΔR  (m)", g.get("range_step_m", 1.0), ("cli", "range_step_m"), fmt="%.3f")
    _num("α<sub>mol</sub> (m⁻¹)", g.get("alpha_mol_m_inv", 1.6e-7),                ("cli", "alpha_mol"), fmt="%.3e")
    _num("β<sub>mol</sub> 输入 (m⁻¹sr⁻¹)", _beta_mol_input_from_global(g) or 1.9e-8,
         ("cli", "beta_mol"), fmt="%.3e")
    _num("δ<sub>mol</sub>",       g.get("molecular_depolarization_ratio", 0.00365), ("cli", "molecular-depol-ratio"),
         fmt="%.5f")


def _build_noise_editor(g: dict) -> None:
    noise = g.get("noise_model", {})
    _switch("启用噪声", noise.get("enabled", True), ("noise", "enabled"))
    _num("η<sub>q</sub>",              noise.get("quantum_efficiency", 0.6),    ("noise", "quantum_efficiency"), fmt="%.4f")
    _num("P<sub>bg</sub>  (W)",        noise.get("background_power_W", 1.0e-12),("noise", "background_power_W"), fmt="%.3e")
    _num("N<sub>avg</sub>",            noise.get("average_pulses", 1000),        ("noise", "average_pulses"),     fmt="%.0f")
    _switch("生成带噪曲线",             noise.get("generate_noisy_curve", False), ("noise", "generate_noisy_curve"))
    _num("seed",                       noise.get("random_seed", 202606) or 0,   ("noise", "random_seed"),        fmt="%.0f")


def _build_profile_editor(g: dict) -> None:
    profile = g.get("atmosphere_profile_model", {}) if isinstance(g, dict) else {}
    mode = str(profile.get("mode", "uniform") or "uniform")
    _choice(
        "大气模式",
        mode,
        ("profile", "mode"),
        {
            "uniform": "均匀大气",
            "ideal_layered": "分层大气",
        },
    )
    _num("β<sub>a0,bnd</sub>  (m⁻¹sr⁻¹)", profile.get("aerosol_boundary_beta0_m_inv_sr", 2.47e-6), ("profile", "aerosol_boundary_beta0_m_inv_sr"), fmt="%.3e")
    _num("H<sub>bnd</sub>  (m)",            profile.get("aerosol_boundary_scale_height_m", 2000.0),  ("profile", "aerosol_boundary_scale_height_m"), fmt="%.1f")
    _num("β<sub>a0,lyr</sub>  (m⁻¹sr⁻¹)", profile.get("aerosol_layer_beta0_m_inv_sr", 5.13e-9),    ("profile", "aerosol_layer_beta0_m_inv_sr"),    fmt="%.3e")
    _num("z<sub>lyr</sub>  (m)",            profile.get("aerosol_layer_center_m", 20000.0),          ("profile", "aerosol_layer_center_m"),          fmt="%.1f")
    _num("Δz<sub>lyr</sub>  (m)",           profile.get("aerosol_layer_width_m", 6000.0),            ("profile", "aerosol_layer_width_m"),           fmt="%.1f")
    _num("S<sub>a</sub>  (sr)",             profile.get("aerosol_lidar_ratio_sr", 50.0),             ("profile", "aerosol_lidar_ratio_sr"),          fmt="%.3f")


def _build_highalt_editor(g: dict) -> None:
    hial = g.get("high_altitude_aerosol", {}) if isinstance(g, dict) else {}
    _num("H  (m)",      hial.get("height_m", 20000.0),    ("high_altitude_aerosol", "height_m"),  fmt="%.1f")
    _num("n₀  (cm⁻³)", hial.get("n0_cm3",   7.245788e-1), ("high_altitude_aerosol", "n0_cm3"),   fmt="%.6e")
    with ui.row().classes("items-start gap-1 mt-1"):
        ui.icon("info", size="xs").classes("text-gray-400 mt-0.5")
        ui.label(
            "谱参数固化：Jager & Deshler 2002 平流层硫酸盐  "
            "r_g=0.10 μm  σ_g=1.86  m=1.43+1e-8i"
        ).classes("text-xs text-gray-400")
    with ui.row().classes("items-start gap-1 mt-1"):
        ui.icon("info", size="xs").classes("text-gray-400 mt-0.5")
        ui.label(
            "文献基准：H=20000m  n₀=7.2458e-01 cm⁻³  → β 与分层大气严格对齐"
        ).classes("text-xs text-gray-400")


def _build_fog_editor(key: str, spec: dict) -> None:
    _num("N₀  (cm⁻³)", spec.get("n0_cm3", 0.0),  ("fog", key, "n0_cm3"))
    _num("r<sub>g</sub>  (μm)",   spec.get("rg_um", 0.0),   ("fog", key, "rg_um"), fmt="%.4f")
    _num("σ<sub>g</sub>",         spec.get("sigma_g", 0.0), ("fog", key, "sigma_g"), fmt="%.4f")
    _num("n (实部)",    spec.get("m_real", 0.0),  ("fog", key, "m_real"), fmt="%.4f")
    _num("k (虚部)",    spec.get("m_imag", 0.0),  ("fog", key, "m_imag"), fmt="%.3g")


def _build_haze_mode_editor(haze_key: str, mode_name: str, mode: dict) -> None:
    _num("N₀  (cm⁻³)", mode.get("n0_cm3", 0.0),  ("haze", haze_key, mode_name, "n0_cm3"))
    _num("r<sub>g</sub>  (μm)",   mode.get("rg_um", 0.0),   ("haze", haze_key, mode_name, "rg_um"), fmt="%.4f")
    _num("σ<sub>g</sub>",         mode.get("sigma_g", 0.0), ("haze", haze_key, mode_name, "sigma_g"), fmt="%.4f")
    _num("n (实部)",    mode.get("m_real", 0.0),  ("haze", haze_key, mode_name, "m_real"), fmt="%.4f")
    _num("k (虚部)",    mode.get("m_imag", 0.0),  ("haze", haze_key, mode_name, "m_imag"), fmt="%.3g")


def _build_rain_editor(key: str, spec: dict) -> None:
    _num("降雨率 (mm/h)", spec.get("rain_rate_mm_h", 0.0), ("rain", key, "rain_rate_mm_h"), fmt="%.2f")


# ---------------------------------------------------------------------------
# Build param_overrides.json from current UI state
# ---------------------------------------------------------------------------


def _collect_overrides() -> dict:
    """Read all _inputs and build the param_overrides dict."""
    fog: dict[str, dict] = {}
    haze: dict[str, dict] = {}
    rain: dict[str, dict] = {}
    cli: dict[str, float] = {}
    noise: dict[str, object] = {}
    profile: dict[str, object] = {}
    high_altitude_aerosol: dict[str, float] = {}

    for key, inp in _inputs.items():
        val = inp.value
        if val is None:
            continue
        if key[0] == "fog":
            _, scenario, field = key
            fog.setdefault(scenario, {})[field] = float(val)
        elif key[0] == "haze":
            _, scenario, mode_name, field = key
            haze.setdefault(scenario, {}).setdefault("modes", {}).setdefault(mode_name, {})[field] = float(val)
        elif key[0] == "rain":
            _, scenario, field = key
            rain.setdefault(scenario, {})[field] = float(val)
        elif key[0] == "cli":
            _, field = key
            if field == "molecular-depol-ratio":
                cli["molecular-depol-ratio"] = float(val)
            elif field == "wavelength_nm":
                cli["wavelength-nm"] = float(val)
            elif field == "range_max_m":
                cli["range-max-m"] = float(val)
            elif field == "range_step_m":
                cli["range-step-m"] = float(val)
            elif field == "alpha_mol":
                cli["alpha-mol"] = float(val)
            elif field == "beta_mol":
                cli["beta-mol"] = float(val)
        elif key[0] == "noise":
            _, field = key
            if field in {"enabled", "generate_noisy_curve"}:
                noise[field] = bool(val)
            elif field in {"average_pulses", "random_seed"}:
                noise[field] = int(float(val))
            else:
                noise[field] = float(val)
        elif key[0] == "profile":
            _, field = key
            if field == "mode":
                profile["mode"] = str(val)
            else:
                profile[field] = float(val)
        elif key[0] == "high_altitude_aerosol":
            _, field = key
            high_altitude_aerosol[field] = float(val)
    # system_constant from instrument params
    p0   = _inputs.get(("cli", "laser_peak_power_W"))
    tau  = _inputs.get(("cli", "pulse_width_s"))
    r    = _inputs.get(("cli", "receiver_radius_m"))
    eta  = _inputs.get(("cli", "optical_efficiency"))
    import math
    if p0 and tau and r and eta and all(
        w.value is not None for w in (p0, tau, r, eta)
    ):
        c = 3e8
        area = math.pi * float(r.value) ** 2
        C = float(p0.value) * c * float(tau.value) * 0.5 * area * float(eta.value)
        cli["system-constant"] = C

    instrument: dict[str, float] = {}
    for field in ("laser_peak_power_W", "pulse_width_s", "receiver_radius_m", "optical_efficiency"):
        inp = _inputs.get(("cli", field))
        if inp is not None and inp.value is not None:
            instrument[field] = float(inp.value)
    if noise:
        instrument["receiver_noise"] = noise

    return {"fog": fog, "haze": haze, "rain": rain, "cli": cli, "instrument": instrument, "profile": profile, "high_altitude_aerosol": high_altitude_aerosol}


# ---------------------------------------------------------------------------
# Identity 计算与历史索引(Stage 4)
# ---------------------------------------------------------------------------
_USE_IDENTITY_CACHE = True  # Stage 5 决策开关;False 时退回旧 _detect_changes 路径


# Stage 4 / 5 共享状态:左侧面板每次重建会更新这里,
# 重算路径直接读 _identity_state_current 拿最新命中分类。
# 页面重建会覆盖前一份(只保留最近一次),避免内存泄漏。
_identity_refresh_current: list = []  # 最多 1 项:最新一次刷新函数
_identity_state_current: dict = {"status": "miss", "record": None}


def _compute_current_identity(precision: str) -> dict | None:
    """根据当前 UI 状态推导 (5 子 key + identity);任何字段缺失返回 None。

    必须严格走 cache_keys.apply_*_overrides(default_*_specs(), overrides) 路径,
    保证前端 hash 与后端 _fog_cache_key 等的输入(包括 UI 不渲染的字段)完全一致。
    """
    try:
        overrides = _collect_overrides()
        args = cache_keys.args_from_precision(precision, overrides)
        fog_specs = cache_keys.apply_fog_overrides(cache_keys.default_fog_specs(), overrides)
        haze_specs = cache_keys.apply_haze_overrides(cache_keys.default_haze_specs(), overrides)
        rain_specs = cache_keys.apply_rain_overrides(cache_keys.default_rain_specs(), overrides)
        fog_key = cache_keys.fog_cache_key(fog_specs, args)
        haze_key = cache_keys.haze_cache_key(haze_specs, args)
        mueller_key = cache_keys.haze_mueller_key(haze_specs, args)
        rain_key = cache_keys.rain_cache_key(rain_specs, args)
        instrument_for_hash = dict(overrides.get("instrument", {}))
        cli_overrides = overrides.get("cli", {})
        if "range-max-m" in cli_overrides:
            instrument_for_hash["range_max_m"] = cli_overrides["range-max-m"]
        if "range-step-m" in cli_overrides:
            instrument_for_hash["range_step_m"] = cli_overrides["range-step-m"]
        instr_hash = cache_keys.instrument_hash(instrument_for_hash)
        noise_hash = cache_keys.noise_hash_from_overrides(overrides)
        profile_hash = cache_keys.profile_hash_from_overrides(overrides)
        identity = cache_keys.compose_run_identity(fog_key, haze_key, mueller_key, rain_key, instr_hash, noise_hash, profile_hash)
        return {
            "fog_key": fog_key,
            "haze_key": haze_key,
            "haze_mueller_key": mueller_key,
            "rain_key": rain_key,
            "instrument_hash": instr_hash,
            "noise_hash": noise_hash,
            "profile_hash": profile_hash,
            "identity": identity,
            "precision_profile": precision,
        }
    except Exception:
        return None


def _build_run_index() -> dict:
    return cache_runtime.build_run_index()


def _classify_identity_match(current: dict, index: dict) -> tuple[str, dict | None]:
    return cache_runtime.classify_identity_match(current, index)


def _plan_headline_class(plan_type: str) -> str:
    return {
        "full_hit": "text-xs text-green-600 font-medium",
        "output_only": "text-xs text-blue-600 font-medium",
        "partial_recompute": "text-xs text-amber-600 font-medium",
        "full_recompute": "text-xs text-red-600 font-medium",
    }.get(plan_type, "text-xs text-gray-500")


def _evaluate_execution_plan(precision: str) -> tuple[dict[str, object], dict | None]:
    try:
        current = _compute_current_identity(precision)
        physics_changed, instrument_changed = _detect_changes()
        plan = cache_runtime.build_execution_plan(
            current,
            precision=precision,
            physics_changed=physics_changed,
            instrument_changed=instrument_changed,
        )
        record = plan.get("matched_record")
        match_status = "full" if plan.get("plan_type") == "full_hit" else (
            "optical_only" if plan.get("plan_type") == "output_only" else "miss"
        )
        return plan, {"status": match_status, "record": record}
    except Exception:
        return {
            "plan_type": "full_recompute",
            "headline": "状态未识别",
            "summary_lines": ["识别失败", "将完整重算"],
            "matched_record": None,
            "layer_status": {},
        }, {"status": "miss", "record": None}


def _refresh_field_impact_hints(summary: dict | None = None) -> None:
    current_summary = summary if isinstance(summary, dict) else load_summary()
    for key, inp in _inputs.items():
        label = _input_impact_labels.get(key)
        if label is None:
            continue
        ref = _reference_value_for_key(current_summary, key)
        changed = ref is not None and inp.value is not None and not _value_equal(key, inp.value, ref)
        if changed:
            label.text = _impact_scope_text(key)
            label.classes(remove="hidden")
        else:
            label.text = ""
            label.classes(add="hidden")


def _format_override_value(key: tuple, value) -> str:
    if value is None:
        return "空"
    if isinstance(value, (int, float)):
        if key[0] == "cli" and key[1] == "wavelength_nm":
            return f"{_compact_num(value)} nm"
        if key[0] == "cli" and key[1] in {"range_max_m", "range_step_m"}:
            return f"{_compact_num(value)} m"
        if key[0] == "cli" and key[1] == "pulse_width_s":
            return f"{_compact_num(value)} s"
        if key[0] == "cli" and key[1] == "receiver_radius_m":
            return f"{_compact_num(value)} m"
        if key[0] == "cli" and key[1] == "optical_efficiency":
            return _compact_num(value)
        if key[0] == "cli" and key[1] in {"alpha_mol", "beta_mol"}:
            return _compact_num(value)
        if key[0] in {"fog", "haze"} and key[-1] == "m_imag":
            return _compact_num(value, sig=4)
        if key[0] == "rain" and key[-1] == "rain_rate_mm_h":
            return f"{_compact_num(value)} mm/h"
        if key[0] == "noise":
            if key[1] in {"enabled", "generate_noisy_curve"}:
                return "开" if bool(value) else "关"
            if key[1] == "average_pulses":
                return f"{int(float(value))}"
            return _compact_num(value)
        if key[0] == "profile":
            if key[1] == "mode":
                return "分层大气" if str(value) == "ideal_layered" else "均匀大气"
            if key[1] in {"aerosol_boundary_scale_height_m", "aerosol_layer_center_m", "aerosol_layer_width_m"}:
                return f"{_compact_num(value)} m"
            return _compact_num(value)
        return _compact_num(value)
    return str(value)


def _reference_value_for_key(summary: dict, key: tuple):
    g = summary.get("global", {})
    inst = g.get("instrument_parameters", {})
    if key[0] == "cli":
        _, field = key
        if field in _INSTRUMENT_KEYS:
            return inst.get(field)
        if field == "wavelength_nm":
            return g.get("wavelength_nm")
        if field == "range_max_m":
            return g.get("range_max_m")
        if field == "range_step_m":
            return g.get("range_step_m")
        if field == "alpha_mol":
            return g.get("alpha_mol_m_inv")
        if field == "beta_mol":
            return _beta_mol_input_from_global(g)
        if field == "molecular-depol-ratio":
            return g.get("molecular_depolarization_ratio")
    if key[0] == "noise":
        _, field = key
        noise = g.get("noise_model", {})
        if field == "random_seed":
            return noise.get(field, 202606)
        return noise.get(field)
    if key[0] == "profile":
        _, field = key
        return g.get("atmosphere_profile_model", {}).get(field)
    if key[0] == "high_altitude_aerosol":
        _, field = key
        return g.get("high_altitude_aerosol", {}).get(field)
    if key[0] == "fog":
        _, scenario, field = key
        return summary.get("fog", {}).get(scenario, {}).get("spec", {}).get(field)
    if key[0] == "haze":
        _, scenario, mode_name, field = key
        modes = summary.get("haze", {}).get(scenario, {}).get("spec", {}).get("modes", [])
        for mode in modes:
            if mode.get("name") == mode_name:
                return mode.get(field)
    if key[0] == "rain":
        _, scenario, field = key
        return summary.get("rain", {}).get(scenario, {}).get("spec", {}).get(field)
    return None


def _summarize_changes(summary: dict, *, limit: int = 8) -> tuple[list[str], int]:
    lines: list[str] = []
    total = 0
    for key, inp in _inputs.items():
        if inp.value is None:
            continue
        ref = _reference_value_for_key(summary, key)
        if ref is None:
            continue
        if _value_equal(key, inp.value, ref):
            continue
        total += 1
        if len(lines) < limit:
            before = _format_override_value(key, ref)
            after = _format_override_value(key, inp.value)
            lines.append(f"{_state_key_label(key)}：{before} -> {after}")
    return lines, total


def _history_label(run_id: str, entry: dict | None = None) -> str:
    if run_id == _DEFAULT_RESULT_ID:
        return "默认结果"
    if not entry:
        return run_id
    ts = entry.get("timestamp", run_id)
    try:
        dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
        ts_str = dt.strftime("%m-%d %H:%M:%S")
    except Exception:
        ts_str = str(ts)

    precision_label = _history_precision_label(entry)
    custom_name = cache_runtime.history_entry_custom_name(entry)
    if custom_name:
        return f"{custom_name} · {ts_str} | {precision_label}"
    return f"{ts_str} | {precision_label}"


def _history_entry(run_id: str | None) -> dict | None:
    if not run_id or run_id == _DEFAULT_RESULT_ID:
        return None
    for entry in _load_manifest().get("runs", []):
        if entry.get("id") == run_id:
            return entry
    return None


def _history_precision_label(entry: dict) -> str:
    precision = entry.get("precision")
    if not precision:
        precision = _infer_history_precision(entry.get("id", ""))
    return {
        "fast": "快速",
        "medium": "中速",
        "high": "高精度",
    }.get(str(precision), "精度未知")


def _infer_history_precision(run_id: str) -> str | None:
    """Best-effort compatibility for histories created before precision was stored."""
    if not run_id:
        return None
    try:
        summary_path = _run_dir(run_id) / "summary.json"
        if not summary_path.exists():
            return None
        text = summary_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    match = re.search(r'"precision"\s*:\s*"(?P<value>fast|medium|high)"', text)
    return match.group("value") if match else None


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

_INSTRUMENT_KEYS = frozenset([
    "laser_peak_power_W", "pulse_width_s", "receiver_radius_m", "optical_efficiency",
    "range_max_m", "range_step_m",
])


def _close(a, b) -> bool:
    a, b = float(a), float(b)
    return abs(a - b) <= 1e-9 * max(abs(b), 1e-30) + 1e-30


def _value_equal(key: tuple, a, b) -> bool:
    if key[0] == "profile" and len(key) > 1 and key[1] == "mode":
        return str(a) == str(b)
    return _close(a, b)


def _detect_changes() -> tuple[bool, bool]:
    """Compare current UI inputs against the active summary.json.

    Returns (physics_changed, instrument_changed).
    """
    sp = _SUMMARY()
    if not sp.exists():
        return True, False
    try:
        summary = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return True, False

    physics_changed = False
    instrument_changed = False
    g = summary.get("global", {})
    inst = g.get("instrument_parameters", {})

    for key, inp in _inputs.items():
        if inp.value is None:
            continue
        val = inp.value

        if key[0] == "cli":
            _, field = key
            val_num = float(val)
            if field in _INSTRUMENT_KEYS:
                ref = inst.get(field)
                if ref is None or not _close(val_num, ref):
                    instrument_changed = True
            elif field == "wavelength_nm":
                ref = g.get("wavelength_nm")
                if ref is None or not _close(val_num, ref):
                    physics_changed = True
            elif field == "alpha_mol":
                ref = g.get("alpha_mol_m_inv")
                if ref is None or not _close(val_num, ref):
                    physics_changed = True
            elif field == "beta_mol":
                ref = _beta_mol_input_from_global(g)
                if ref is None or not _close(val_num, ref):
                    physics_changed = True
            elif field == "molecular-depol-ratio":
                ref = g.get("molecular_depolarization_ratio")
                if ref is None or not _close(val_num, ref):
                    physics_changed = True

        elif key[0] == "fog":
            _, scenario, field = key
            spec = summary.get("fog", {}).get(scenario, {}).get("spec", {})
            ref = spec.get(field)
            if ref is None or not _close(float(val), ref):
                physics_changed = True

        elif key[0] == "noise":
            _, field = key
            noise = g.get("noise_model", {})
            ref = noise.get(field)
            if field in {"enabled", "generate_noisy_curve"}:
                if ref is None or bool(inp.value) != bool(ref):
                    instrument_changed = True
            elif ref is None or not _close(float(val), ref):
                instrument_changed = True

        elif key[0] == "profile":
            _, field = key
            profile = g.get("atmosphere_profile_model", {})
            ref = profile.get(field)
            if field == "mode":
                if str(val) != str(ref or "uniform"):
                    physics_changed = True
            elif ref is None or not _close(float(val), ref):
                physics_changed = True

        elif key[0] == "haze":
            _, scenario, mode_name, field = key
            spec_modes = summary.get("haze", {}).get(scenario, {}).get("spec", {}).get("modes", [])
            ref = None
            for m in spec_modes:
                if m.get("name") == mode_name:
                    ref = m.get(field)
                    break
            if ref is None or not _close(float(val), ref):
                physics_changed = True

        elif key[0] == "rain":
            _, scenario, field = key
            spec = summary.get("rain", {}).get(scenario, {}).get("spec", {})
            ref = spec.get(field)
            if ref is None or not _close(float(val), ref):
                physics_changed = True

    _diag_event(
        "change_detection_finished",
        timeline=_RECOMPUTE_TIMELINE,
        payload={
            "physics_changed": physics_changed,
            "instrument_changed": instrument_changed,
            "summary_path": str(sp),
        },
    )
    return physics_changed, instrument_changed


# ---------------------------------------------------------------------------
# Recompute logic
# ---------------------------------------------------------------------------

_recompute_running = False
_current_proc: asyncio.subprocess.Process | None = None  # running simulation process
# Persistent log buffer — survives page rebuilds (not cleared on recompute).
_log_buf: list[str] = []

# 高空低气溶胶计算状态
_hial_running = False
_hial_current_proc: asyncio.subprocess.Process | None = None
_hial_callbacks: list = []   # 图表刷新回调（在 page 构建后注册）
_LOG_BUF_MAX_LINES = 5000

# 模块级状态，让 build_left_panel 在页面重建时能恢复显示
_recompute_status_text: str = "空闲"
_recompute_status_class: str = "text-xs text-gray-400"


def _set_recompute_status(text: str, css_class: str) -> None:
    """记录最新状态，供页面重建时恢复（不依赖 client 存活）。"""
    global _recompute_status_text, _recompute_status_class
    _recompute_status_text = text
    _recompute_status_class = css_class


def _safe_call(fn, *args, **kwargs) -> None:
    """对 UI 调用做容错：client 断开后任何 UI 更新都会抛 'client this element belongs to has been deleted'。
    background_tasks 上下文里 ui.notify/ui.navigate 还会抛 'current slot cannot be determined'。
    这两类都属于"调用时机正常、但当前没有可绑定 UI 的渲染上下文"，静默吞掉；
    其他异常仍打印一行，便于调试，但不让其中断后续业务逻辑（如 manifest 写入）。"""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        msg = str(e).lower()
        if "client" in msg and ("deleted" in msg or "disconnect" in msg):
            return  # 客户端断开
        if "slot" in msg and ("empty" in msg or "determined" in msg):
            return  # background task 没有 slot 栈
        print(f"[demo_ui] safe_call swallowed: {e!r}")


def _safe_ui(client, fn, *args, **kwargs) -> None:
    """与 _safe_call 一致，但在调用前进入指定 client 的 slot 上下文。
    background_tasks.create() 启动的协程脱离了请求生命周期，ui.notify/ui.navigate.reload
    这类需要 slot 的调用必须显式 `with client:` 才能找到连接。
    client 已断开或为 None 时退化为普通 _safe_call。"""
    if client is None:
        _safe_call(fn, *args, **kwargs)
        return
    try:
        with client:
            fn(*args, **kwargs)
    except Exception as e:
        msg = str(e).lower()
        if "client" in msg and ("deleted" in msg or "disconnect" in msg):
            return
        if "slot" in msg and ("empty" in msg or "determined" in msg):
            return
        print(f"[demo_ui] safe_ui swallowed: {e!r}")


def _current_client_or_none():
    """返回当前请求/页面对应的 nicegui Client，捕获不到时返回 None。
    用于把 client 引用传给 background task，让任务结束后还能在该 client 的 slot 上下文里更新 UI。"""
    try:
        from nicegui import context
        return context.client
    except Exception:
        return None


def _log_append(buf: list[str], line: str) -> None:
    buf.append(line)
    if len(buf) > _LOG_BUF_MAX_LINES:
        del buf[:len(buf) - _LOG_BUF_MAX_LINES]

# History panel refresh hook — set once the left panel is built so callbacks can update it
_history_refresh_ref: list = []  # holds [callable(run_id=None)] when ready

# UI 状态持久化（用于窗口切换后恢复）
_ui_state: dict = {}  # 模块级状态，页面重建时保留


def _save_ui_state(active_tab: str | None = None, log_dialog_open: bool | None = None) -> None:
    """保存 UI 状态到模块级变量"""
    global _ui_state
    if active_tab is not None:
        _ui_state["active_tab"] = active_tab
    if log_dialog_open is not None:
        _ui_state["log_dialog_open"] = log_dialog_open


def _load_ui_state() -> dict:
    """从模块级变量加载 UI 状态"""
    return _ui_state.copy()


def _kill_current_proc() -> None:
    """Terminate the running simulation process tree (including Julia grandchildren)."""
    global _current_proc
    p = _current_proc
    if p is None:
        return
    try:
        if os.name == "nt":
            # taskkill /T kills the entire process tree, reaching Julia grandchildren.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                capture_output=True,
            )
        else:
            import signal, os as _os
            try:
                _os.killpg(_os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
    except Exception:
        pass


app.on_shutdown(_kill_current_proc)


async def _do_recompute(status_label: ui.label, log_buf: list,
                        recompute_btn: ui.button,
                        chart_refresh_callbacks: list,
                        precision: str = "fast",
                        log_area: ui.textarea | None = None,
                        log_dialog=None,
                        client=None) -> None:
    """长任务实现。被 background_tasks.create() 调度，脱离 client 生命周期。
    所有 UI 更新都通过 _safe_call 包裹——即使 client 断开（用户切到其他窗口、关浏览器、超时）
    后续的 manifest 写入/归档/状态恢复仍然会完成。
    需要 slot 上下文的调用（ui.notify / ui.navigate.reload）通过 _safe_ui 进入 client。"""
    import time as _time
    global _recompute_running, _active_data_dir, _active_figures_dir, _active_summary, _current_proc
    if _recompute_running:
        _safe_ui(client, ui.notify, "重算正在进行中，请等待", type="warning")
        return

    _diag_event(
        "recompute_requested",
        status="begin",
        timeline=_RECOMPUTE_TIMELINE,
        payload={"precision": precision},
    )
    _recompute_running = True
    _set_recompute_status("检查参数变化…", "text-xs text-blue-600")
    _safe_call(recompute_btn.disable)
    _safe_call(status_label.set_text, "检查参数变化…")
    _safe_call(status_label.classes, remove="text-green-600 text-red-600", add="text-blue-600")

    # Stage 5:identity 查表优先,命中则直接切换活跃指针,跳过子进程
    if _USE_IDENTITY_CACHE:
        try:
            _id_current = _compute_current_identity(precision)
        except Exception:
            _id_current = None
        if _id_current is not None:
            try:
                _id_index = _build_run_index()
                _id_status, _id_record = _classify_identity_match(_id_current, _id_index)
            except Exception:
                _id_status, _id_record = ("miss", None)

            if _id_status == "full" and _id_record:
                _id_run_id = _id_record.get("run_id")
                try:
                    load_run(_id_run_id)
                    for cb in chart_refresh_callbacks:
                        try:
                            cb()
                        except Exception:
                            pass
                    _label = _id_record.get("label") or _id_run_id
                    _set_recompute_status(f"已切换到匹配历史结果 · {_label}",
                                          "text-xs text-green-600")
                    _safe_call(status_label.set_text, f"已切换到匹配历史结果 · {_label}")
                    _safe_call(status_label.classes,
                               remove="text-blue-600 text-red-600",
                               add="text-green-600")
                    _safe_ui(client, ui.notify,
                             f"已切换到匹配的历史结果:{_label}", type="positive")
                    _diag_event(
                        "recompute_skipped",
                        timeline=_RECOMPUTE_TIMELINE,
                        payload={"reason": "identity_full_hit", "run_id": _id_run_id},
                    )
                    _recompute_running = False
                    _safe_call(recompute_btn.enable)
                    return
                except Exception as exc:
                    # 命中但切换失败(目录被清理等),记录后 fall through 到完整重算
                    _diag_event(
                        "identity_switch_failed",
                        payload={"run_id": _id_run_id, "reason": str(exc)},
                    )
            elif _id_status == "optical_only":
                # 当前实现对仅仪器参数变化的场景仍执行完整重算。
                _diag_event(
                    "identity_optical_only",
                    payload={"run_id": (_id_record or {}).get("run_id")},
                )

    physics_changed, instrument_changed = _detect_changes()

    # No changes at all and outputs already exist -> skip recompute.
    sp = _SUMMARY()
    if (not physics_changed and not instrument_changed
            and sp.exists() and _FIGURES().exists() and _DATA_DIR().exists()):
        _set_recompute_status("参数未变化，已跳过", "text-xs text-green-600")
        _safe_call(status_label.set_text, "参数未变化，已跳过")
        _safe_call(status_label.classes, remove="text-blue-600 text-red-600", add="text-green-600")
        _safe_ui(client, ui.notify, "参数未变化，未触发重算", type="info")
        _diag_event(
            "recompute_skipped",
            timeline=_RECOMPUTE_TIMELINE,
            payload={"reason": "no_changes", "summary_path": str(sp)},
        )
        for cb in chart_refresh_callbacks:
            try:
                cb()
            except Exception:
                pass
        _recompute_running = False
        _safe_call(recompute_btn.enable)
        return

    _set_recompute_status("重算中…", "text-xs text-blue-600")
    _safe_call(status_label.set_text, "重算中…")

    # Write overrides only when recompute is actually needed.
    overrides = _collect_overrides()
    try:
        OVERRIDES.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        _diag_event(
            "param_overrides_written",
            timeline=_RECOMPUTE_TIMELINE,
            payload={"path": str(OVERRIDES), "precision": precision},
        )
    except Exception as e:
        _set_recompute_status(f"写入参数失败: {e}", "text-xs text-red-600")
        _safe_call(status_label.set_text, f"写入参数失败: {e}")
        _safe_call(status_label.classes, remove="text-blue-600 text-green-600", add="text-red-600")
        _diag_event(
            "param_overrides_written",
            status="error",
            timeline=_RECOMPUTE_TIMELINE,
            payload={"path": str(OVERRIDES), "error": str(e)},
        )
        _recompute_running = False
        _safe_call(recompute_btn.enable)
        return

    try:
        cache_runtime.begin_pending_run({
            "params": overrides,
            "precision": precision,
            "label": "自动恢复",
        })
        _diag_event(
            "pending_run_written",
            timeline=_RECOMPUTE_TIMELINE,
            payload={"path": str(cache_runtime.LAYOUT.pending_run_path), "precision": precision},
        )
    except Exception as e:
        _set_recompute_status(f"写入待恢复状态失败: {e}", "text-xs text-red-600")
        _safe_call(status_label.set_text, f"写入待恢复状态失败: {e}")
        _safe_call(status_label.classes, remove="text-blue-600 text-green-600", add="text-red-600")
        _diag_event(
            "pending_run_written",
            status="error",
            timeline=_RECOMPUTE_TIMELINE,
            payload={"path": str(cache_runtime.LAYOUT.pending_run_path), "error": str(e)},
        )
        _recompute_running = False
        _safe_call(recompute_btn.enable)
        return

    # 使用统一路径解析
    from path_resolver import resolve_mie_python_executable
    mie_python = resolve_mie_python_executable()

    # Full clean only when physics-related parameters changed.
    cmd = [mie_python, str(MFF_SCRIPT), "--precision", precision]
    if physics_changed:
        cmd.append("--clean")

    mode_hint = "full-clean" if physics_changed else "instrument-fast"
    header_line = (
        f"\n{'─'*60}\n"
        f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [precision:{precision}] [{mode_hint}]\n"
        f"$ {' '.join(cmd)}\n"
    )
    _log_append(log_buf, header_line)
    _diag_append_stdout(header_line)
    if log_area is not None:
        _safe_call(setattr, log_area, "value", "".join(log_buf))

    t0 = _time.perf_counter()
    proc_returncode: int | None = None
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs: dict = {"cwd": str(ROOT), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        _diag_event(
            "recompute_subprocess_started",
            timeline=_RECOMPUTE_TIMELINE,
            payload={
                "cmd": cmd,
                "cwd": str(ROOT),
                "physics_changed": physics_changed,
                "instrument_changed": instrument_changed,
            },
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        _current_proc = proc

        if proc.stdout is None:
            raise RuntimeError("subprocess stdout pipe unavailable")
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            _log_append(log_buf, line)
            _diag_append_stdout(line)
            if log_area is not None:
                _safe_call(setattr, log_area, "value", "".join(log_buf))

        await proc.wait()
        proc_returncode = proc.returncode
        elapsed = _time.perf_counter() - t0
        summary_line = f"\n耗时: {int(elapsed // 60)} 分 {elapsed % 60:.1f} 秒\n"
        _log_append(log_buf, summary_line)
        _diag_append_stdout(summary_line)
        _diag_event(
            "recompute_subprocess_finished",
            status="ok" if proc_returncode == 0 else "error",
            elapsed_ms=elapsed * 1000.0,
            timeline=_RECOMPUTE_TIMELINE,
            payload={"returncode": proc_returncode},
        )
        if log_area is not None:
            _safe_call(setattr, log_area, "value", "".join(log_buf))
    except asyncio.CancelledError:
        # 协程被取消（极少见，因为我们脱离了 client）。让 subprocess 继续跑——
        # 它已经在系统层面独立运行；finally 块的 manifest 检查会在下次启动时归档。
        elapsed = _time.perf_counter() - t0
        line = f"[warn] _do_recompute coroutine cancelled at {elapsed:.1f}s; subprocess continues independently\n"
        _log_append(log_buf, line)
        _diag_append_stdout(line)
        _diag_event(
            "recompute_subprocess_finished",
            status="error",
            elapsed_ms=elapsed * 1000.0,
            timeline=_RECOMPUTE_TIMELINE,
            payload={"error": "cancelled"},
        )
        raise
    except Exception as e:
        elapsed = _time.perf_counter() - t0
        err_line = f"{e}\n耗时: {int(elapsed // 60)} 分 {elapsed % 60:.1f} 秒\n"
        _log_append(log_buf, err_line)
        _diag_append_stdout(err_line)
        if log_area is not None:
            _safe_call(setattr, log_area, "value", "".join(log_buf))
        _set_recompute_status("异常中止", "text-xs text-red-600")
        _safe_call(status_label.set_text, "异常中止")
        _safe_call(status_label.classes, remove="text-blue-600 text-green-600", add="text-red-600")
        _safe_ui(client, ui.notify, str(e), type="negative")
        _diag_event(
            "recompute_subprocess_finished",
            status="error",
            elapsed_ms=elapsed * 1000.0,
            timeline=_RECOMPUTE_TIMELINE,
            payload={"error": str(e)},
        )
        _diag_update_gui_summary({"error": str(e)}, status="error")
        cache_runtime.clear_pending_run()
        _recompute_running = False
        _current_proc = None
        _safe_call(recompute_btn.enable)
        return

    # ── 后处理：必须始终执行（哪怕 client 早就断开） ───────────────────────
    _current_proc = None
    try:
        if proc_returncode == 0:
            # 关键步骤：归档到 history + 写 manifest（无论 client 是否存活都要执行）
            try:
                run_id = save_run_to_history(overrides, precision=precision)
                _diag_event("history_saved", timeline=_RECOMPUTE_TIMELINE, payload={"run_id": run_id})
                load_run(run_id)
                cache_runtime.clear_pending_run()
                _diag_event("active_run_switched", timeline=_RECOMPUTE_TIMELINE, payload={"run_id": run_id})
            except Exception as e:
                print(f"[demo_ui] save_run_to_history failed: {e!r}")
                _set_recompute_status(f"归档失败: {e}", "text-xs text-red-600")
                _diag_event(
                    "history_saved",
                    status="error",
                    timeline=_RECOMPUTE_TIMELINE,
                    payload={"error": str(e)},
                )
                _diag_update_gui_summary({"error": str(e)}, status="error")
                cache_runtime.clear_pending_run()
                _recompute_running = False
                _safe_call(recompute_btn.enable)
                return

            _set_recompute_status("完成 ✓", "text-xs text-green-600")
            _diag_update_gui_summary(
                {
                    "last_run_id": run_id,
                    "precision": precision,
                    "physics_changed": physics_changed,
                    "instrument_changed": instrument_changed,
                    "stdout_log": str(_DIAG_SESSION.component_dir / _RECOMPUTE_STDOUT_LOG),
                },
                status="success",
            )

            # 以下都是 UI 更新，client 断开会失败但不影响业务
            if _history_refresh_ref:
                _safe_ui(client, _history_refresh_ref[0], run_id)

            _safe_call(status_label.set_text, "完成 ✓")
            _safe_call(status_label.classes, remove="text-blue-600 text-red-600", add="text-green-600")
            _safe_ui(client, ui.notify, "重算完成，图表已刷新", type="positive")
            for cb in chart_refresh_callbacks:
                try:
                    cb()
                except Exception as _cb_err:
                    print(f"[warn] chart_refresh_callback error: {_cb_err}")
            _trigger_plan_refresh()
        else:
            cache_runtime.clear_pending_run()
            _set_recompute_status(f"失败（退出码 {proc_returncode}）", "text-xs text-red-600")
            _safe_call(status_label.set_text, f"失败（退出码 {proc_returncode}）")
            _safe_call(status_label.classes, remove="text-blue-600 text-green-600", add="text-red-600")
            _safe_ui(client, ui.notify, "重算失败，请查看日志", type="negative")
            _diag_update_gui_summary({"returncode": proc_returncode}, status="error")
    finally:
        _recompute_running = False
        _save_ui_state(log_dialog_open=False)
        _safe_call(recompute_btn.enable)


# Hardcoded physics defaults — mirror lidar_1d_simulation.py constants exactly.
# _reset_to_defaults reads ONLY from here, never from the mutable summary dict.
_DEFAULTS: dict = {
    # Instrument / CLI
    "laser_peak_power_W":   50.0,
    "pulse_width_s":        2.0e-7,
    "receiver_radius_m":    0.05,
    "optical_efficiency":   0.8,
    "wavelength_nm":        1550.0,
    "range_max_m":          2000.0,
    "range_step_m":         1.0,
    "alpha_mol":            1.6e-7,
    "beta_mol":             1.9e-8,
    "molecular-depol-ratio": 0.00365,
    # Fog scenarios: {scenario: {field: value}}
    "fog": {
        "radiation_fog": {"n0_cm3": 50.0, "rg_um": 2.0,  "sigma_g": 1.4, "m_real": 1.314, "m_imag": 1.0e-4},
        "advection_fog": {"n0_cm3": 5.0,  "rg_um": 7.0,  "sigma_g": 1.8, "m_real": 1.314, "m_imag": 1.0e-4},
    },
    # Haze scenarios: {scenario: {mode_name: {field: value}}}
    "haze": {
        "urban_industrial_haze": {
            "fine":   {"n0_cm3": 15000.0, "rg_um": 0.08,  "sigma_g": 2.0,  "m_real": 1.50, "m_imag": 0.005},
            "coarse": {"n0_cm3": 5.0,     "rg_um": 0.60,  "sigma_g": 2.3,  "m_real": 1.55, "m_imag": 0.005},
        },
        "rural_continental_haze": {
            "fine":   {"n0_cm3": 5000.0,  "rg_um": 0.08,  "sigma_g": 2.0,  "m_real": 1.45, "m_imag": 0.001},
            "coarse": {"n0_cm3": 3.0,     "rg_um": 0.60,  "sigma_g": 2.3,  "m_real": 1.53, "m_imag": 0.003},
        },
        "dust_desert_haze": {
            "fine":   {"n0_cm3": 1500.0,  "rg_um": 0.075, "sigma_g": 2.0,  "m_real": 1.55, "m_imag": 0.005},
            "coarse": {"n0_cm3": 100.0,   "rg_um": 1.10,  "sigma_g": 2.15, "m_real": 1.55, "m_imag": 0.010},
        },
        "maritime_haze": {
            "fine":   {"n0_cm3": 500.0,   "rg_um": 0.075, "sigma_g": 2.0,  "m_real": 1.35, "m_imag": 0.0},
            "coarse": {"n0_cm3": 25.0,    "rg_um": 0.55,  "sigma_g": 2.0,  "m_real": 1.34, "m_imag": 0.0},
        },
    },
    # Rain scenarios: {scenario: {field: value}}
    "rain": {
        "light_rain":    {"rain_rate_mm_h": 1.0},
        "moderate_rain": {"rain_rate_mm_h": 5.0},
        "heavy_rain":    {"rain_rate_mm_h": 12.0},
    },
    "noise": {
        "enabled": True,
        "quantum_efficiency": 0.6,
        "background_power_W": 1.0e-12,
        "average_pulses": 1000,
        "generate_noisy_curve": False,
        "random_seed": 202606,
    },
    "profile": {
        "mode": "uniform",
        "aerosol_boundary_beta0_m_inv_sr": 2.47e-6,
        "aerosol_boundary_scale_height_m": 2000.0,
        "aerosol_layer_beta0_m_inv_sr": 5.13e-9,
        "aerosol_layer_center_m": 20000.0,
        "aerosol_layer_width_m": 6000.0,
        "aerosol_lidar_ratio_sr": 50.0,
    },
}


def _reset_to_defaults(refresh_callbacks: list | None = None) -> None:
    """Load default result set (read-only, no new history entry)."""

    # 1. Check if default result set exists
    if not _DEFAULT_RESULT_DIR.exists() or not (_DEFAULT_RESULT_DIR / "summary.json").exists():
        # Fallback: restore parameters to hardcoded values (old behavior)
        if OVERRIDES.exists():
            OVERRIDES.unlink()
        for key, inp in _inputs.items():
            if key[0] == "fog":
                _, scenario, field = key
                val = _DEFAULTS["fog"].get(scenario, {}).get(field)
                if val is not None:
                    inp.value = val
            elif key[0] == "haze":
                _, scenario, mode_name, field = key
                val = _DEFAULTS["haze"].get(scenario, {}).get(mode_name, {}).get(field)
                if val is not None:
                    inp.value = val
            elif key[0] == "rain":
                _, scenario, field = key
                val = _DEFAULTS["rain"].get(scenario, {}).get(field)
                if val is not None:
                    inp.value = val
            elif key[0] == "cli":
                _, field = key
                val = _DEFAULTS.get(field)
                if val is not None:
                    inp.value = val
            elif key[0] == "noise":
                _, field = key
                val = _DEFAULTS["noise"].get(field)
                if val is not None:
                    inp.value = val
            elif key[0] == "profile":
                _, field = key
                val = _DEFAULTS["profile"].get(field)
                if val is not None:
                    inp.value = val
        _trigger_plan_refresh()
        ui.notify("已恢复默认参数（无预生成结果）", type="info")
        return

    # 2. Switch active view to default result set
    cache_runtime.set_active_view(_DEFAULT_RESULT_ID)
    _refresh_active_paths(_DEFAULT_RESULT_ID)

    # 3. Restore parameter panel from default result's params.json
    summary = load_summary()
    params_file = _DEFAULT_RESULT_DIR / "params.json"
    if params_file.exists():
        try:
            params = json.loads(params_file.read_text(encoding="utf-8"))
            _apply_params_json(params, summary)
        except Exception:
            _populate_inputs_from_summary(summary)
    else:
        _populate_inputs_from_summary(summary)

    # 4. Delete current param_overrides.json (ensure next recompute uses default params)
    if OVERRIDES.exists():
        OVERRIDES.unlink()

    # 5. Update history selector to show "默认"
    if _history_refresh_ref:
        _history_refresh_ref[0](_DEFAULT_RESULT_ID)

    _trigger_plan_refresh()
    for cb in refresh_callbacks or []:
        try:
            cb()
        except Exception as _cb_err:
            print(f"[warn] reset refresh callback error: {_cb_err}")

    ui.notify("已加载默认结果集", type="positive")


def _populate_inputs_from_summary(summary: dict) -> None:
    """Overwrite all input widgets with values from the given summary dict."""
    g    = summary.get("global", {})
    inst = g.get("instrument_parameters", {})
    for key, inp in _inputs.items():
        if key[0] == "fog":
            _, scenario, field = key
            spec = summary.get("fog", {}).get(scenario, {}).get("spec", {})
            if field in spec:
                inp.value = spec[field]
        elif key[0] == "haze":
            _, scenario, mode_name, field = key
            spec_modes = summary.get("haze", {}).get(scenario, {}).get("spec", {}).get("modes", [])
            for m in spec_modes:
                if m.get("name") == mode_name and field in m:
                    inp.value = m[field]
        elif key[0] == "rain":
            _, scenario, field = key
            spec = summary.get("rain", {}).get(scenario, {}).get("spec", {})
            if field in spec:
                inp.value = spec[field]
        elif key[0] == "cli":
            _, field = key
            if field == "laser_peak_power_W":
                v = inst.get("laser_peak_power_W")
                if v is not None: inp.value = v
            elif field == "pulse_width_s":
                v = inst.get("pulse_width_s")
                if v is not None: inp.value = v
            elif field == "receiver_radius_m":
                v = inst.get("receiver_radius_m")
                if v is not None: inp.value = v
            elif field == "optical_efficiency":
                v = inst.get("optical_efficiency")
                if v is not None: inp.value = v
            elif field == "wavelength_nm":
                v = g.get("wavelength_nm")
                if v is not None: inp.value = v
            elif field == "range_max_m":
                v = g.get("range_max_m")
                if v is not None: inp.value = v
            elif field == "range_step_m":
                v = g.get("range_step_m")
                if v is not None: inp.value = v
            elif field == "alpha_mol":
                v = g.get("alpha_mol_m_inv")
                if v is not None: inp.value = v
            elif field == "beta_mol":
                v = _beta_mol_input_from_global(g)
                if v is not None: inp.value = v
            elif field == "molecular-depol-ratio":
                v = g.get("molecular_depolarization_ratio")
                if v is not None: inp.value = v
        elif key[0] == "noise":
            _, field = key
            noise = g.get("noise_model", {})
            if field in noise:
                inp.value = noise[field]
            elif field in _DEFAULTS["noise"]:
                inp.value = _DEFAULTS["noise"][field]
        elif key[0] == "profile":
            _, field = key
            profile = g.get("atmosphere_profile_model", {})
            if field in profile:
                inp.value = profile[field]
            elif field in _DEFAULTS["profile"]:
                inp.value = _DEFAULTS["profile"][field]
        elif key[0] == "high_altitude_aerosol":
            _, field = key
            hial = g.get("high_altitude_aerosol", {})
            if field in hial:
                inp.value = hial[field]


# ---------------------------------------------------------------------------
# Left panel
# ---------------------------------------------------------------------------

def build_left_panel(summary: dict, chart_refresh_callbacks: list) -> None:
    g    = summary.get("global", {})

    with ui.column().classes("w-full gap-3"):

        # 页面重建/重连时，从模块级变量恢复状态文案，
        # 这样即使 client 在计算过程中断开过，重新连接后也能看到正确状态。
        if _recompute_running:
            status_text = "重算中…"
            status_class = "text-xs text-blue-600"
        else:
            status_text = _recompute_status_text
            status_class = _recompute_status_class

        # ── 历史记录选择器 ─────────────────────────────────────────────────
        hist_opts = _history_options()
        active_id = cache_runtime.get_active_view_id()

        with ui.expansion("历史记录", icon="history", value=True).classes("w-full"):
            hist_select = ui.select(
                hist_opts or {},
                value=active_id if active_id in hist_opts else (next(iter(hist_opts), None) if hist_opts else None),
                label="选择仿真存档",
            ).props("dense outlined").classes("w-full text-xs")
            if not hist_opts:
                hist_select.disable()
            current_history_label = ui.label(
                f"当前：{hist_opts[active_id]}" if active_id in hist_opts else "（尚无历史记录，重算后自动填入）"
            ).classes("text-xs text-gray-500 mt-1")
            history_name_input = ui.input(
                "存档名称",
                placeholder="可选命名，不影响计算结果",
            ).props("dense outlined clearable maxlength=40").classes("w-full text-xs")
            with ui.row().classes("gap-2 flex-wrap"):
                save_history_name_btn = ui.button("保存名称", icon="save").props("dense flat").classes("text-xs")
                clear_history_name_btn = ui.button("清除名称", icon="backspace").props("dense flat").classes("text-xs")

            def _refresh_history_name_controls(run_id: str | None) -> None:
                options = _history_options()
                if run_id and run_id in options:
                    current_history_label.text = f"当前：{options[run_id]}"
                elif options:
                    current_history_label.text = "请选择仿真存档"
                else:
                    current_history_label.text = "（尚无历史记录，重算后自动填入）"
                current_history_label.update()

                entry = _history_entry(run_id)
                if entry is None:
                    history_name_input.value = ""
                    history_name_input.disable()
                    save_history_name_btn.disable()
                    clear_history_name_btn.disable()
                    history_name_input.update()
                    return

                history_name_input.enable()
                save_history_name_btn.enable()
                clear_history_name_btn.enable()
                history_name_input.value = cache_runtime.history_entry_custom_name(entry)
                history_name_input.update()

            def _refresh_history_select(run_id: str | None = None) -> None:
                options = _history_options()
                hist_select.options = options
                if options:
                    selected = run_id if run_id in options else hist_select.value
                    hist_select.value = selected if selected in options else next(iter(options), None)
                    hist_select.enable()
                else:
                    hist_select.value = None
                    hist_select.disable()
                hist_select.update()
                _refresh_history_name_controls(hist_select.value)

            _history_refresh_ref.clear()
            _history_refresh_ref.append(_refresh_history_select)

            def _save_history_name() -> None:
                run_id = hist_select.value
                if not run_id or run_id == _DEFAULT_RESULT_ID:
                    ui.notify("默认结果不可重命名", type="warning")
                    return
                try:
                    cache_runtime.rename_history_run(run_id, history_name_input.value or "")
                except Exception as _err:
                    ui.notify(f"保存名称失败: {_err}", type="negative")
                    return
                _refresh_history_select(run_id)
                _trigger_plan_refresh()
                ui.notify("已保存存档名称", type="positive")

            def _clear_history_name() -> None:
                run_id = hist_select.value
                if not run_id or run_id == _DEFAULT_RESULT_ID:
                    ui.notify("默认结果不可重命名", type="warning")
                    return
                try:
                    cache_runtime.rename_history_run(run_id, "")
                except Exception as _err:
                    ui.notify(f"清除名称失败: {_err}", type="negative")
                    return
                _refresh_history_select(run_id)
                _trigger_plan_refresh()
                ui.notify("已清除存档名称", type="info")

            save_history_name_btn.on_click(_save_history_name)
            clear_history_name_btn.on_click(_clear_history_name)
            _refresh_history_name_controls(hist_select.value)

            def _on_history_change(e) -> None:
                run_id = e.value
                if not run_id:
                    return
                try:
                    new_summary = load_run(run_id)
                except FileNotFoundError as _err:
                    ui.notify(str(_err), type="negative")
                    return
                except Exception as _err:
                    ui.notify(f"加载存档失败: {_err}", type="negative")
                    return
                # Repopulate input widgets to match the loaded run's params.json.
                # Default result uses its own params file; normal runs use run_history/<id>/params.json.
                if run_id == _DEFAULT_RESULT_ID:
                    params_file = _DEFAULT_RESULT_DIR / "params.json"
                else:
                    params_file = _run_dir(run_id) / "params.json"
                if params_file.exists():
                    try:
                        _apply_params_json(
                            json.loads(params_file.read_text(encoding="utf-8")),
                            new_summary,
                        )
                    except Exception:
                        _populate_inputs_from_summary(new_summary)
                else:
                    _populate_inputs_from_summary(new_summary)
                for cb in chart_refresh_callbacks:
                    try:
                        cb()
                    except Exception as _cb_err:
                        print(f"[warn] history chart refresh error: {_cb_err}")
                _trigger_plan_refresh()
                display = _history_options().get(run_id, run_id)
                _refresh_history_name_controls(run_id)
                ui.notify(f"已切换到存档 {display}", type="info")

            hist_select.on_value_change(_on_history_change)

        # ── 仪器参数（可编辑） ─────────────────────────────────────────────
        with ui.expansion("仪器参数", icon="settings", value=True).classes("w-full"):
            ui.label("修改后点击「重算」生效").classes("text-xs text-amber-600 italic mb-1")
            _build_instrument_editor(g)

        # ── 全局计算参数（可编辑） ─────────────────────────────────────────
        with ui.expansion("大气分子参数", icon="tune").classes("w-full"):
            ui.label("修改后点击「重算」生效").classes("text-xs text-amber-600 italic mb-1")
            _build_global_editor(g)

        with ui.expansion("分层大气", icon="layers").classes("w-full"):
            ui.label("分层模式会额外生成剖面与分层回波图").classes("text-xs text-amber-600 italic mb-1")
            _build_profile_editor(g)

        with ui.expansion("高空低气溶胶浓度", icon="air").classes("w-full"):
            ui.label("平流层硫酸盐谱（Jager & Deshler 2002），输入高度与数密度").classes("text-xs text-amber-600 italic mb-1")
            _build_highalt_editor(g)

        # ── 场景参数（逐条展开，可编辑） ───────────────────────────────────
        FOG_SCENARIOS  = [("radiation_fog","辐射雾"), ("advection_fog","平流雾")]
        HAZE_SCENARIOS = [("urban_industrial_haze","城市/工业型霾"),
                          ("rural_continental_haze","乡村/大陆背景型霾"),
                          ("dust_desert_haze","沙尘型霾"),
                          ("maritime_haze","海洋性霾")]
        RAIN_SCENARIOS = [("light_rain","小雨"),("moderate_rain","中雨"),("heavy_rain","大雨")]

        for cat, cat_icon, cat_label, scenes in [
            ("fog",  "water_drop", "雾 — 场景参数",  FOG_SCENARIOS),
            ("haze", "blur_on",    "霾 — 场景参数",  HAZE_SCENARIOS),
            ("rain", "grain",      "雨 — 场景参数",  RAIN_SCENARIOS),
        ]:
            cat_data = summary.get(cat, {})
            with ui.expansion(cat_label, icon=cat_icon).classes("w-full"):
                for key, label in scenes:
                    sc   = cat_data.get(key, {})
                    spec = sc.get("spec", {})
                    with ui.card().classes(
                        "w-full mb-2 p-2 bg-gray-50 shadow-none border border-gray-200"
                    ):
                        with ui.row().classes("items-center gap-2 mb-1"):
                            dot_color = PAL.get(key, "#888")
                            ui.element("div").style(
                                f"width:10px;height:10px;border-radius:50%;"
                                f"background:{dot_color};flex-shrink:0"
                            )
                            ui.label(label).classes("font-semibold text-sm")

                        if cat == "fog":
                            fog_spec = spec if spec else _DEFAULTS["fog"].get(key, {})
                            _build_fog_editor(key, fog_spec)
                        elif cat == "haze":
                            spec_modes = spec.get("modes", [])
                            if not spec_modes:
                                haze_defaults = _DEFAULTS["haze"].get(key, {})
                                spec_modes = [{"name": mode_name, **vals} for mode_name, vals in haze_defaults.items()]
                            for mode in spec_modes:
                                mode_name = mode.get("name", "?")
                                tmatrix_tag = "  [T-matrix]" if mode.get("use_tmatrix") else ""
                                ui.label(f"▸ {mode_name}{tmatrix_tag}").classes(
                                    "text-xs font-medium text-gray-600 mt-1"
                                )
                                _build_haze_mode_editor(key, mode_name, mode)
                        elif cat == "rain":
                            rain_spec = spec if spec else _DEFAULTS["rain"].get(key, {})
                            _build_rain_editor(key, rain_spec)

        # ── 重算控制区 ────────────────────────────────────────────────────
        ui.separator()
        with ui.column().classes("w-full gap-2"):
            ui.label("重算控制").classes("text-sm font-semibold text-gray-700")
            status_label = ui.label(status_text).classes(status_class)

            precision_select = ui.select(
                {
                    "fast": "快速",
                    "medium": "中速",
                    "high": "高精度",
                },
                value="fast",
                label="计算精度",
            ).props("dense outlined").classes("w-full text-xs")

            with ui.column().classes("w-full gap-1 px-2 py-2 rounded bg-gray-50 border border-gray-100").style("overflow:hidden; min-width:0"):
                plan_headline_label = ui.label("…").classes("text-xs text-gray-500").style("overflow:hidden; white-space:nowrap; text-overflow:ellipsis; min-width:0")
                plan_summary_column = ui.column().classes("w-full gap-0").style("overflow:hidden; min-width:0")

            def _refresh_identity_status() -> None:
                """根据当前 UI 输入 + precision 重新评估缓存执行计划并刷新展示。"""
                global _identity_state_current
                if not _USE_IDENTITY_CACHE:
                    _identity_state_current = {"status": "miss", "record": None}
                    try:
                        plan_headline_label.text = "(缓存执行计划已禁用)"
                        plan_headline_label.classes(replace="text-xs text-gray-400")
                        plan_summary_column.clear()
                    except Exception:
                        pass
                    _refresh_field_impact_hints()
                    return
                plan, identity_state = _evaluate_execution_plan(precision_select.value)
                _identity_state_current = identity_state
                try:
                    plan_headline_label.text = str(plan.get("headline") or "状态未识别")
                    plan_headline_label.classes(replace=_plan_headline_class(str(plan.get("plan_type"))))
                    plan_summary_column.clear()
                    with plan_summary_column:
                        for line in list(plan.get("summary_lines") or [])[:3]:
                            ui.label(str(line)).classes("text-[11px] text-gray-600").style(
                                "overflow:hidden; white-space:nowrap; text-overflow:ellipsis; min-width:0"
                            )
                except Exception:
                    pass
                _refresh_field_impact_hints()

            # 精度档变化时刷新(修复原 bug:fast→high 不再误报"参数匹配")
            precision_select.on_value_change(lambda _e: _refresh_identity_status())
            # 替换为最近一次的刷新函数(供 _on_recompute_click 及其它入口调用)
            _identity_refresh_current[:] = [_refresh_identity_status]

            with ui.row().classes("gap-2 flex-wrap"):
                reset_btn = ui.button("恢复默认", icon="restart_alt").props(
                    "dense flat"
                ).classes("text-xs")
                recompute_btn = ui.button("写入并重算", icon="play_arrow",
                                          color="primary").props("dense").classes("text-xs")
                if _recompute_running:
                    recompute_btn.disable()
                log_btn = ui.button("查看日志", icon="terminal").props(
                    "dense flat"
                ).classes("text-xs")

            log_buf = _log_buf  # module-level, persists across page rebuilds

            with ui.dialog().props("maximized") as log_dialog:
                with ui.card().classes("w-full flex flex-col p-0").style("height:100vh"):
                    with ui.row().classes("items-center gap-2 px-4 py-2 bg-gray-100 shrink-0"):
                        ui.label("计算日志").classes("font-semibold text-sm flex-1")
                        copy_log_btn = ui.button("复制", icon="content_copy").props("dense flat size=sm")
                        ui.button(icon="close", on_click=lambda: (_save_ui_state(log_dialog_open=False), log_dialog.close())).props("dense flat")
                    log_ta = ui.textarea().props("readonly").classes(
                        "w-full font-mono text-xs"
                    ).style("font-size:11px; resize:none; flex:1; min-height:0;")
                    copy_log_btn.on_click(
                        lambda: ui.run_javascript(
                            f"navigator.clipboard.writeText({json.dumps(log_ta.value)})"
                        )
                    )

            # 页面重建时，如正在重算且之前日志窗口开启，则自动恢复
            if _recompute_running and _load_ui_state().get("log_dialog_open"):
                log_ta.value = "".join(log_buf)
                log_dialog.open()

            def _open_log() -> None:
                if not _recompute_running:
                    log_ta.value = "".join(log_buf)
                _save_ui_state(log_dialog_open=True)
                log_dialog.open()

            log_btn.on_click(_open_log)

            reset_btn.on_click(lambda: _reset_to_defaults(chart_refresh_callbacks))

            # Pre-recompute confirmation dialog
            with ui.dialog() as confirm_dialog, ui.card().classes("p-6 gap-4"):
                ui.label("重算前确认").classes("text-base font-bold")
                confirm_summary = ui.column().classes(
                    "w-full gap-1 rounded border border-gray-200 bg-gray-50 p-3"
                )
                ui.label("重算会刷新当前图表和 CSV，并在成功后自动写入历史记录。").classes(
                    "text-sm text-gray-600"
                )
                with ui.row().classes("gap-3 mt-2 justify-end w-full"):
                    async def _save_then_recompute(_e=None,
                                                   _sl=status_label, _lb=log_buf,
                                                   _btn=recompute_btn, _cbs=chart_refresh_callbacks,
                                                   _psel=precision_select):
                        confirm_dialog.close()
                        import zipfile as _zf
                        _buf = io.BytesIO()
                        with _zf.ZipFile(_buf, "w", _zf.ZIP_DEFLATED) as _z:
                            for _name in _EXPORT_CURVES:
                                _csv = _build_csv_bytes(_name, _DATA_DIR())
                                if _csv is not None:
                                    _z.writestr(f"data/{_name}", _csv)
                            _figs = _FIGURES()
                            if _figs.exists():
                                for _p in sorted(_figs.glob("*.png")):
                                    _z.write(_p, f"figures/{_p.name}")
                                for _p in sorted(_figs.glob("*.svg")):
                                    _z.write(_p, f"figures/{_p.name}")
                        _buf.seek(0)
                        _ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        await _native_save(_buf.getvalue(), f"lidar_results_{_ts}.zip",
                                           [("ZIP archive", "*.zip")])
                        # 用 background_tasks 让 _do_recompute 脱离当前 client 生命周期；
                        # 即使用户切换窗口、关闭浏览器、WebSocket 心跳超时，任务也不会被取消。
                        # 把当前 client 传入，让最终的 ui.notify/ui.navigate.reload 能进入 slot 上下文。
                        _cli = _current_client_or_none()
                        background_tasks.create(
                            _do_recompute(_sl, _lb, _btn, _cbs, _psel.value, log_ta, log_dialog, client=_cli),
                            name="lidar_recompute",
                        )

                    async def _just_recompute(_e=None,
                                              _sl=status_label, _lb=log_buf,
                                              _btn=recompute_btn, _cbs=chart_refresh_callbacks,
                                              _psel=precision_select):
                        confirm_dialog.close()
                        _cli = _current_client_or_none()
                        background_tasks.create(
                            _do_recompute(_sl, _lb, _btn, _cbs, _psel.value, log_ta, log_dialog, client=_cli),
                            name="lidar_recompute",
                        )

                    ui.button("保存并重算", icon="download", color="primary").on_click(_save_then_recompute)
                    ui.button("直接重算", icon="play_arrow", color="warning").on_click(_just_recompute)
                    ui.button("取消", icon="close").on_click(lambda: confirm_dialog.close())

            async def _on_recompute_click(_e=None):
                # 点击重算前再算一次执行计划,保证状态条与即将执行的判定一致
                _refresh_identity_status()
                plan, _identity_state = _evaluate_execution_plan(precision_select.value)
                change_lines, change_total = _summarize_changes(load_summary())
                confirm_summary.clear()
                with confirm_summary:
                    ui.label(f"计算精度：{precision_select.options.get(precision_select.value, precision_select.value)}").classes(
                        "text-xs text-gray-600"
                    )
                    ui.label(f"执行计划：{plan.get('headline') or '状态未识别'}").classes("text-xs text-gray-600")
                    ui.label(f"检测到变化：{change_total} 项").classes("text-xs text-gray-600")
                    for line in list(plan.get("summary_lines") or [])[:3]:
                        ui.label(str(line)).classes("text-xs text-gray-600")
                    if change_lines:
                        for line in change_lines:
                            ui.label(line).classes("text-xs font-mono text-gray-700")
                        if change_total > len(change_lines):
                            ui.label(f"另有 {change_total - len(change_lines)} 项变化未展开").classes(
                                "text-xs text-gray-500"
                            )
                    else:
                        ui.label("当前参数与活动结果一致，通常会跳过重算。").classes(
                            "text-xs text-gray-500"
                        )
                confirm_dialog.open()

            recompute_btn.on_click(_on_recompute_click)

            # 面板构建结束后立即算一次 identity 命中,作为状态条的初值
            try:
                _refresh_identity_status()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers for restoring inputs from a history entry's params.json
# ---------------------------------------------------------------------------

def _apply_params_json(params: dict, summary: dict) -> None:
    """Apply a saved params.json (overrides format) + summary to all _inputs."""
    fog  = params.get("fog", {})
    haze = params.get("haze", {})
    rain = params.get("rain", {})
    cli  = params.get("cli", {})
    profile = params.get("profile", {})
    saved_instrument = params.get("instrument", {})
    noise_params = saved_instrument.get("receiver_noise", params.get("noise", {})) if isinstance(saved_instrument, dict) else params.get("noise", {})

    for key, inp in _inputs.items():
        if key[0] == "fog":
            _, scenario, field = key
            val = fog.get(scenario, {}).get(field)
            if val is not None:
                inp.value = val
            else:
                # Fall back to summary
                spec = summary.get("fog", {}).get(scenario, {}).get("spec", {})
                if field in spec:
                    inp.value = spec[field]

        elif key[0] == "haze":
            _, scenario, mode_name, field = key
            val = haze.get(scenario, {}).get("modes", {}).get(mode_name, {}).get(field)
            if val is not None:
                inp.value = val
            else:
                spec_modes = summary.get("haze", {}).get(scenario, {}).get("spec", {}).get("modes", [])
                for m in spec_modes:
                    if m.get("name") == mode_name and field in m:
                        inp.value = m[field]

        elif key[0] == "rain":
            _, scenario, field = key
            val = rain.get(scenario, {}).get(field)
            if val is not None:
                inp.value = val
            else:
                spec = summary.get("rain", {}).get(scenario, {}).get("spec", {})
                if field in spec:
                    inp.value = spec[field]

        elif key[0] == "cli":
            _, field = key
            g    = summary.get("global", {})
            inst = g.get("instrument_parameters", {})
            saved = params.get("instrument", {})
            if field == "laser_peak_power_W":
                inp.value = saved.get("laser_peak_power_W", inst.get("laser_peak_power_W", 50.0))
            elif field == "pulse_width_s":
                inp.value = saved.get("pulse_width_s", inst.get("pulse_width_s", 2e-7))
            elif field == "receiver_radius_m":
                inp.value = saved.get("receiver_radius_m", inst.get("receiver_radius_m", 0.05))
            elif field == "optical_efficiency":
                inp.value = saved.get("optical_efficiency", inst.get("optical_efficiency", 0.8))
            elif field == "wavelength_nm":
                inp.value = g.get("wavelength_nm", 1550.0)
            elif field == "range_max_m":
                inp.value = g.get("range_max_m", 2000.0)
            elif field == "range_step_m":
                inp.value = g.get("range_step_m", 1.0)
            elif field == "alpha_mol":
                inp.value = g.get("alpha_mol_m_inv", 1.6e-7)
            elif field == "beta_mol":
                inp.value = _beta_mol_input_from_global(g) or 1.9e-8
            elif field == "molecular-depol-ratio":
                inp.value = g.get("molecular_depolarization_ratio", 0.00365)
            # CLI overrides: params.json stores hyphen-form keys (wavelength-nm etc.)
            # Map underscore field names back to hyphen keys used in cli dict.
            _field_to_cli_key = {
                "wavelength_nm":       "wavelength-nm",
                "range_max_m":         "range-max-m",
                "range_step_m":        "range-step-m",
                "alpha_mol":           "alpha-mol",
                "beta_mol":            "beta-mol",
                "molecular-depol-ratio": "molecular-depol-ratio",
            }
            cli_key = _field_to_cli_key.get(field, field)
            cli_val = cli.get(cli_key)
            if cli_val is not None:
                inp.value = cli_val

        elif key[0] == "noise":
            _, field = key
            g = summary.get("global", {})
            noise_summary = g.get("noise_model", {})
            if field in noise_params:
                inp.value = noise_params[field]
            elif field in noise_summary:
                inp.value = noise_summary[field]
            elif field in _DEFAULTS["noise"]:
                inp.value = _DEFAULTS["noise"][field]
        elif key[0] == "profile":
            _, field = key
            profile_summary = summary.get("global", {}).get("atmosphere_profile_model", {})
            if field in profile:
                inp.value = profile[field]
            elif field in profile_summary:
                inp.value = profile_summary[field]
            elif field in _DEFAULTS["profile"]:
                inp.value = _DEFAULTS["profile"][field]
        elif key[0] == "high_altitude_aerosol":
            _, field = key
            hial_saved = params.get("high_altitude_aerosol", {})
            if field in hial_saved:
                inp.value = hial_saved[field]


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def _recover_orphan_run() -> None:
    try:
        result = cache_runtime.recover_pending_local_result()
        run_id = result.get("run_id")
        if run_id:
            _refresh_active_paths(str(run_id))
        if result.get("recovered"):
            print(f"[demo_ui] orphan run recovered: {run_id}")
        _diag_event(
            "orphan_recovery_checked",
            timeline=_STARTUP_TIMELINE,
            payload=result,
        )
    except Exception as e:
        print(f"[demo_ui] _recover_orphan_run failed: {e!r}")
        _diag_event(
            "orphan_recovery_checked",
            status="error",
            timeline=_STARTUP_TIMELINE,
            payload={"error": str(e)},
        )


@ui.page("/")
def index() -> None:
    # 启动时尝试恢复上次因 client 断开而未归档的孤儿运行
    _recover_orphan_run()

    summary = load_summary()
    _diag_event(
        "summary_loaded",
        timeline=_STARTUP_TIMELINE,
        payload={
            "summary_exists": _SUMMARY().exists(),
            "summary_path": str(_SUMMARY()),
            "fog_count": len(summary.get("fog", {})),
            "haze_count": len(summary.get("haze", {})),
            "rain_count": len(summary.get("rain", {})),
        },
    )

    ui.query("body").style("margin:0;padding:0;overflow:hidden")

    # ── header ────────────────────────────────────────────────────────────
    with ui.header().classes(
        "bg-slate-800 text-white items-center px-5 py-2 shadow-lg"
    ).style("min-height:48px"):
        ui.icon("radar", size="1.6rem")
        ui.label("大气激光雷达散射特性仿真  ·  结果展示").classes(
            "text-base font-bold ml-2 tracking-wide"
        )
        ui.space()
        g = summary.get("global", {})
        ui.label(f"λ = {g.get('wavelength_nm','')} nm").classes(
            "text-xs text-slate-300 font-mono"
        )
        ui.separator().props("vertical").classes("mx-3 opacity-30")
        sp = _SUMMARY()
        if sp.exists():
            ts = datetime.datetime.fromtimestamp(
                sp.stat().st_mtime
            ).strftime("%Y-%m-%d  %H:%M")
            ui.icon("schedule", size="xs", color="green-4")
            ui.label(f"上次计算：{ts}").classes("text-xs text-slate-300 ml-1")
        else:
            ui.icon("warning", size="xs", color="yellow-5")
            ui.label("尚未计算").classes("text-xs text-yellow-300 ml-1")

    # ── chart refresh callbacks (registered after right panel is built) ───
    chart_refresh_callbacks: list = []

    # ── body: left + right ────────────────────────────────────────────────
    with ui.row().classes("w-full gap-0").style(
        "height:calc(100vh - 48px); overflow:hidden"
    ):
        # left scroll area
        with ui.scroll_area().style(
            "width:300px; min-width:260px; max-width:340px;"
            "height:100%; background:#fff;"
            "border-right:1px solid #e2e8f0; flex-shrink:0;"
        ).classes("px-3 py-3"):
            build_left_panel(summary, chart_refresh_callbacks)
            _diag_event("left_panel_built", timeline=_STARTUP_TIMELINE)
            # Restore the active run's exact UI state from params.json so the
            # startup path is identical to the history-switch path.  Without
            # this, inputs are populated from summary.json only, which can
            # differ from params.json (e.g. instrument keys stored differently).
            _aid = cache_runtime.get_active_view_id()
            if _aid:
                _pf = cache_runtime.get_run_paths(_aid)["params"]
                if _pf.exists():
                    try:
                        _apply_params_json(
                            json.loads(_pf.read_text(encoding="utf-8")), summary
                        )
                        _trigger_plan_refresh()
                        _diag_event(
                            "params_restored",
                            timeline=_STARTUP_TIMELINE,
                            payload={"active_id": _aid, "params_path": str(_pf)},
                        )
                    except Exception:
                        pass

        # right scroll area  — build charts and register refresh callbacks
        with ui.scroll_area().classes("flex-1 px-5 py-4").style(
            "height:100%; background:#f1f5f9; min-width:0"
        ):
            _build_right_panel_with_refresh(summary, chart_refresh_callbacks)
            _diag_event("right_panel_built", timeline=_STARTUP_TIMELINE)
            _diag_event("ui_ready", timeline=_STARTUP_TIMELINE)
            _diag_update_gui_summary(
                {
                    "active_summary": str(_SUMMARY()),
                    "history_runs": len(_load_manifest().get("runs", [])),
                    "log_buffer_lines": len(_log_buf),
                }
            )


def _build_right_panel_with_refresh(summary: dict, callbacks: list) -> None:
    """Build right panel and register per-tab chart refresh callbacks."""

    # 恢复上次激活的标签页
    ui_state = _load_ui_state()
    last_active_tab = ui_state.get("active_tab")

    with ui.tabs().classes(
        "w-full bg-white rounded-lg shadow-sm sticky top-0 z-10"
    ) as tabs:
        t_fog       = ui.tab("雾 · 功率",        icon="water_drop")
        t_rain      = ui.tab("雨 · 功率",        icon="grain")
        t_highalt   = ui.tab("高空低气溶胶浓度",  icon="air")
        t_haze      = ui.tab("霾 · 功率",        icon="blur_on")
        t_depol     = ui.tab("霾 · 退偏",        icon="tune")
        t_layered   = ui.tab("分层大气",          icon="layers")
        t_snr       = ui.tab("信噪比",            icon="show_chart")
        t_all       = ui.tab("全场景对比",         icon="compare_arrows")

    # 记录标签页切换状态
    tab_name_map = {
        t_fog:     "雾 · 功率",
        t_rain:    "雨 · 功率",
        t_highalt: "高空低气溶胶浓度",
        t_haze:    "霾 · 功率",
        t_depol:   "霾 · 退偏",
        t_layered: "分层大气",
        t_snr:     "信噪比",
        t_all:     "全场景对比",
    }

    def _on_tab_change(e) -> None:
        name = tab_name_map.get(e.value)
        if name:
            _save_ui_state(active_tab=name)

    tabs.on_value_change(_on_tab_change)

    # 设置初始激活标签页（如果上次不是在查看日志）
    initial_tab = t_fog
    if last_active_tab and last_active_tab != "log_view":
        # 尝试匹配标签页名称
        tab_map = {
            "雾 · 功率":       t_fog,
            "雨 · 功率":       t_rain,
            "高空低气溶胶浓度": t_highalt,
            "霾 · 功率":       t_haze,
            "霾 · 退偏":       t_depol,
            "分层大气":        t_layered,
            "信噪比":          t_snr,
            "全场景对比":      t_all,
        }
        initial_tab = tab_map.get(last_active_tab, t_fog)

    with ui.tab_panels(tabs, value=initial_tab).classes(
        "w-full bg-white rounded-lg shadow-sm mt-2 p-5"
    ):
        with ui.tab_panel(t_fog):
            chart_tab(
                fig_builder=fig_fog_power,
                allow_log=True, default_log=True,
                summary=summary, cat="fog",
                summary_keys=[("radiation_fog","辐射雾"),("advection_fog","平流雾")],
                show_depol=False,
                csv_links=[("辐射雾","fig1_RadiationFog.csv"),("平流雾","fig2_AdvectionFog.csv")],
                ref_images=[("图 1  辐射雾","fig01_radiation_fog_power"),
                            ("图 2  平流雾","fig02_advection_fog_power")],
                callbacks=callbacks,
            )

        with ui.tab_panel(t_rain):
            chart_tab(
                fig_builder=fig_rain_power,
                allow_log=True, default_log=True,
                summary=summary, cat="rain",
                summary_keys=[("light_rain","小雨"),("moderate_rain","中雨"),("heavy_rain","大雨")],
                show_depol=False,
                csv_links=[("小雨","fig11a_LightRain.csv"),
                           ("中雨","fig11b_ModerateRain.csv"),
                           ("大雨","fig11c_HeavyRain.csv")],
                ref_images=[("图 11  小/中/大雨","fig11_rain_power")],
                callbacks=callbacks,
            )

        with ui.tab_panel(t_highalt):
            highalt_tab()

        with ui.tab_panel(t_haze):
            chart_tab(
                fig_builder=fig_haze_power,
                allow_log=True, default_log=True,
                summary=summary, cat="haze",
                summary_keys=[("urban_industrial_haze","城市/工业型霾"),
                              ("rural_continental_haze","乡村/大陆背景型霾"),
                              ("dust_desert_haze","沙尘型霾"),
                              ("maritime_haze","海洋性霾")],
                show_depol=True,
                csv_links=[("城市/工业型霾","fig3_UrbanIndustrialHaze.csv"),
                           ("乡村/大陆型霾","fig4_RuralContinentalHaze.csv"),
                           ("沙尘型霾","fig5_DustDesertHaze.csv"),
                           ("海洋性霾","fig6_MaritimeHaze.csv")],
                ref_images=[("图 3  城市/工业型霾","fig03_urban_industrial_haze_power"),
                            ("图 4  乡村/大陆型霾","fig04_rural_continental_haze_power"),
                            ("图 5  沙尘型霾","fig05_dust_desert_haze_power"),
                            ("图 6  海洋性霾","fig06_maritime_haze_power")],
                callbacks=callbacks,
            )

        with ui.tab_panel(t_depol):
            chart_tab(
                fig_builder=fig_haze_depol,
                allow_log=False, default_log=False,
                summary=summary, cat="haze",
                summary_keys=[("urban_industrial_haze","城市/工业型霾"),
                              ("rural_continental_haze","乡村/大陆背景型霾"),
                              ("dust_desert_haze","沙尘型霾"),
                              ("maritime_haze","海洋性霾")],
                show_depol=True,
                csv_links=[("城市/工业型霾退偏","fig7_UrbanIndustrialHazeDepol.csv"),
                           ("乡村/大陆型霾退偏","fig8_RuralContinentalHazeDepol.csv"),
                           ("沙尘型霾退偏","fig9_DustDesertHazeDepol.csv"),
                           ("海洋性霾退偏","fig10_MaritimeHazeDepol.csv")],
                ref_images=[("图 7  城市/工业型霾","fig07_urban_industrial_haze_depol"),
                            ("图 8  乡村/大陆型霾","fig08_rural_continental_haze_depol"),
                            ("图 9  沙尘型霾","fig09_dust_desert_haze_depol"),
                            ("图 10 海洋性霾","fig10_maritime_haze_depol")],
                callbacks=callbacks,
            )

        with ui.tab_panel(t_layered):
            layered_tab(callbacks)

        with ui.tab_panel(t_snr):
            with ui.card().classes("w-full p-4 shadow-none border"):
                ui.label("全场景信噪比 SNR").classes("font-semibold text-sm text-gray-700 mb-2")
                snr_plotly = ui.plotly(fig_all_snr()).classes("w-full").style("min-height:420px; overflow:hidden")

            with ui.expansion("SNR 数值摘要", icon="table_chart", value=True).classes("w-full"):
                ui.label("雾").classes("text-xs font-semibold text-gray-500 mt-1")
                snr_fog_keys = [("radiation_fog","辐射雾"),("advection_fog","平流雾")]
                snr_fog_table = render_summary_table(summary,"fog",snr_fog_keys,show_depol=False, show_snr=True)
                ui.label("霾").classes("text-xs font-semibold text-gray-500 mt-2")
                snr_haze_keys = [("urban_industrial_haze","城市/工业型霾"),
                                 ("rural_continental_haze","乡村/大陆背景型霾"),
                                 ("dust_desert_haze","沙尘型霾"),
                                 ("maritime_haze","海洋性霾")]
                snr_haze_table = render_summary_table(summary,"haze",snr_haze_keys,show_depol=True, show_snr=True)
                ui.label("雨").classes("text-xs font-semibold text-gray-500 mt-2")
                snr_rain_keys = [("light_rain","小雨"),("moderate_rain","中雨"),("heavy_rain","大雨")]
                snr_rain_table = render_summary_table(summary,"rain",snr_rain_keys,show_depol=False, show_snr=True)

            def redraw_snr() -> None:
                current_summary = load_summary()
                snr_plotly.update_figure(fig_all_snr())
                refresh_summary_table(snr_fog_table, current_summary, "fog", snr_fog_keys, show_depol=False, show_snr=True)
                refresh_summary_table(snr_haze_table, current_summary, "haze", snr_haze_keys, show_depol=True, show_snr=True)
                refresh_summary_table(snr_rain_table, current_summary, "rain", snr_rain_keys, show_depol=False, show_snr=True)

            with ui.row().classes("items-center gap-3 flex-wrap pt-1"):
                ui.label("SNR 数据下载：").classes("text-sm text-gray-500 font-medium")
                for label, export_name in [
                    ("辐射雾", "fig1_RadiationFog_SNR.csv"),
                    ("平流雾", "fig2_AdvectionFog_SNR.csv"),
                    ("城市霾", "fig3_UrbanIndustrialHaze_SNR.csv"),
                    ("乡村霾", "fig4_RuralContinentalHaze_SNR.csv"),
                    ("沙尘霾", "fig5_DustDesertHaze_SNR.csv"),
                    ("海洋霾", "fig6_MaritimeHaze_SNR.csv"),
                    ("小雨", "fig11a_LightRain_SNR.csv"),
                    ("中雨", "fig11b_ModerateRain_SNR.csv"),
                    ("大雨", "fig11c_HeavyRain_SNR.csv"),
                ]:
                    curve = _EXPORT_CURVES.get(export_name)
                    if curve and (_DATA_DIR() / curve[0]).exists():
                        async def _dl_snr(_name=export_name) -> None:
                            data = _build_csv_bytes(_name, _DATA_DIR())
                            if data is not None:
                                await _native_save(data, _name, [("CSV file", "*.csv")])
                        ui.button(f"↓ {label}", on_click=_dl_snr).props("dense flat").classes("text-sm font-mono text-green-700")

            callbacks.append(redraw_snr)

        with ui.tab_panel(t_all):
            log_state_all = [True]

            with ui.card().classes("w-full p-4 shadow-none border"):
                with ui.row().classes("items-center gap-3 mb-2 flex-wrap"):
                    ui.label("全场景回波功率对比").classes("font-semibold text-sm text-gray-700")
                    ui.space()
                    sc_tog = ui.toggle({"log":"对数","lin":"线性"}, value="log").props("dense").classes("text-xs")
                pwr_plotly = ui.plotly(fig_all_power(True)).classes("w-full").style("min-height:420px; overflow:hidden")

                def _on_all_scale(e) -> None:
                    log_state_all[0] = (e.value == "log")
                    pwr_plotly.update_figure(fig_all_power(log_state_all[0]))
                sc_tog.on_value_change(_on_all_scale)

            with ui.card().classes("w-full p-4 shadow-none border"):
                ui.label("全场景退偏比对比").classes("font-semibold text-sm text-gray-700 mb-2")
                depol_plotly = ui.plotly(fig_all_depol()).classes("w-full").style("min-height:420px; overflow:hidden")

            def redraw_all_power() -> None:
                pwr_plotly.update_figure(fig_all_power(log_state_all[0]))

            def redraw_all_depol() -> None:
                depol_plotly.update_figure(fig_all_depol())

            with ui.expansion("全场景数值摘要", icon="table_chart", value=True).classes("w-full"):
                ui.label("雾").classes("text-xs font-semibold text-gray-500 mt-1")
                all_fog_keys = [("radiation_fog","辐射雾"),("advection_fog","平流雾")]
                all_fog_table = render_summary_table(summary,"fog",all_fog_keys,show_depol=False)
                ui.label("霾").classes("text-xs font-semibold text-gray-500 mt-2")
                all_haze_keys = [("urban_industrial_haze","城市/工业型霾"),
                                 ("rural_continental_haze","乡村/大陆背景型霾"),
                                 ("dust_desert_haze","沙尘型霾"),
                                 ("maritime_haze","海洋性霾")]
                all_haze_table = render_summary_table(summary,"haze",all_haze_keys,show_depol=True)
                ui.label("雨").classes("text-xs font-semibold text-gray-500 mt-2")
                all_rain_keys = [("light_rain","小雨"),("moderate_rain","中雨"),("heavy_rain","大雨")]
                all_rain_table = render_summary_table(summary,"rain",all_rain_keys,show_depol=False)

            def redraw_all_summary() -> None:
                current_summary = load_summary()
                refresh_summary_table(all_fog_table, current_summary, "fog", all_fog_keys, show_depol=False)
                refresh_summary_table(all_haze_table, current_summary, "haze", all_haze_keys, show_depol=True)
                refresh_summary_table(all_rain_table, current_summary, "rain", all_rain_keys, show_depol=False)

    # Register "全场景对比" redraws into the callback list.
    callbacks.append(redraw_all_power)
    callbacks.append(redraw_all_depol)
    callbacks.append(redraw_all_summary)


# ---------------------------------------------------------------------------
# Startup self-check
# ---------------------------------------------------------------------------

def _startup_self_check() -> list[str]:
    """启动自检：检查关键路径、可执行文件、目录权限。

    返回错误列表（空列表表示通过）。
    """
    t0 = time.perf_counter()
    _diag_event("startup_self_check_begin", status="begin", timeline=_STARTUP_TIMELINE)
    errors = []

    # 使用统一路径解析
    from path_resolver import resolve_mie_python_executable, resolve_julia_executable

    # 1. 检查 Mie Python 可执行文件
    mie_python = resolve_mie_python_executable()
    mie_path = Path(mie_python)
    if not mie_path.exists():
        errors.append(f"Mie Python 不存在: {mie_python}")
    elif not os.access(mie_path, os.X_OK):
        errors.append(f"Mie Python 无执行权限: {mie_python}")

    # 2. 检查 Julia 可执行文件
    try:
        julia_exe = resolve_julia_executable()
        julia_path = Path(julia_exe)
        if not julia_path.exists():
            errors.append(f"Julia 不存在: {julia_exe}")
        elif not os.access(julia_path, os.X_OK):
            errors.append(f"Julia 无执行权限: {julia_exe}")
    except Exception as e:
        errors.append(str(e))

    # 3. 检查关键目录可写性
    critical_dirs = [
        (ROOT / "temp" / "lidar_1d", "工作目录"),
        (HISTORY_DIR.parent, "历史记录父目录"),
    ]

    for dir_path, desc in critical_dirs:
        if not dir_path.exists():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"{desc} 无法创建: {dir_path} ({e})")
        else:
            # 测试写入
            test_file = dir_path / ".write_test"
            try:
                test_file.write_text("test", encoding="utf-8")
                test_file.unlink()
            except Exception as e:
                errors.append(f"{desc} 无写权限: {dir_path} ({e})")

    # 4. 检查默认结果状态
    if _DEFAULT_RESULT_DIR.exists():
        if not (_DEFAULT_RESULT_DIR / "summary.json").exists():
            errors.append(f"默认结果目录存在但 summary.json 缺失: {_DEFAULT_RESULT_DIR}")

    # 5. 检查历史 manifest 一致性
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
            active_id = manifest.get("active_id")
            if active_id and active_id != _DEFAULT_RESULT_ID:
                run_dir = HISTORY_DIR / active_id
                if not run_dir.exists():
                    errors.append(f"manifest 指向不存在的运行: {active_id}")
        except Exception as e:
            errors.append(f"manifest.json 损坏: {e}")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    _diag_event(
        "startup_self_check_end",
        status="error" if errors else "ok",
        elapsed_ms=elapsed_ms,
        timeline=_STARTUP_TIMELINE,
        payload={"errors": errors},
    )
    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ in {"__main__", "__mp_main__"}:
    _diag_event(
        "app_startup_begin",
        status="begin",
        timeline=_STARTUP_TIMELINE,
        payload={"root": str(ROOT), "pid": os.getpid()},
    )
    # 启动自检
    check_errors = _startup_self_check()
    if check_errors:
        error_msg = "启动自检失败，发现以下问题：\n\n"
        for i, err in enumerate(check_errors, 1):
            error_msg += f"{i}. {err}\n"
        error_msg += "\n建议：\n"
        error_msg += "- 检查当前目录读写权限\n"
        error_msg += "- 确认 pixi 环境已初始化\n"
        error_msg += "- 确认 Julia 可执行文件可用，并且 julia_depot 已就位\n"

        # 写入日志文件
        log_file = ROOT / "startup_check_failed.log"
        try:
            log_file.write_text(error_msg, encoding="utf-8")
        except Exception:
            pass
        _diag_update_gui_summary({"startup_error_log": str(log_file), "errors": check_errors}, status="error")

        # 使用 NiceGUI 显示错误（避免 input() 在无终端时阻塞）
        @ui.page("/")
        def show_error():
            ui.query("body").style("margin:0;padding:20px;background:#f5f5f5")
            with ui.card().classes("w-full max-w-4xl mx-auto"):
                ui.label("启动自检失败").classes("text-h5 text-red-600 mb-4")
                ui.label(error_msg).classes("whitespace-pre-line text-sm font-mono")
                ui.button("退出", on_click=lambda: app.shutdown()).props("color=negative")

        ui.run(
            title="启动自检失败",
            native=True,
            window_size=(800, 600),
            reload=False,
            dark=False,
        )
        sys.exit(1)

    ui.run(
        title="大气激光雷达散射特性仿真",
        native=True,
        window_size=(1440, 900),
        reload=False,
        dark=False,
        # 长任务可能跑 30+ 分钟；窗口失焦时 WebView2 会节流 JS 定时器导致 WebSocket 心跳延迟。
        # 把重连容忍窗口拉长到 1 小时，配合 background_tasks 让计算任务完全独立于 client 生命周期。
        reconnect_timeout=3600,
    )
