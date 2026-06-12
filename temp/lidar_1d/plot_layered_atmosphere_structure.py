from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cache_runtime  # noqa: E402
from atmosphere_profile import build_ideal_layered_profile  # noqa: E402


SVG_WIDTH = 1500
SVG_HEIGHT = 860
MARGIN_LEFT = 80
MARGIN_RIGHT = 40
MARGIN_TOP = 70
MARGIN_BOTTOM = 60


def _load_active_summary() -> tuple[str | None, dict]:
    active = cache_runtime.load_active_view()
    run_id = active.get("run_id")
    summary_path = cache_runtime.get_run_paths(run_id)["summary"]
    if not summary_path.exists():
        raise FileNotFoundError(f"未找到活动结果 summary.json: {summary_path}")
    return run_id, json.loads(summary_path.read_text(encoding="utf-8"))


def _resolve_profile_and_wavelength(summary: dict) -> tuple[dict, float]:
    global_block = summary.get("global", {})
    profile = dict(global_block.get("atmosphere_profile_model", {}) or {})
    if not profile:
        layered = summary.get("layered_atmosphere", {})
        profile = dict(layered.get("profile_model", {}) or {})
    if not profile:
        raise ValueError("当前活动结果不包含分层大气参数")
    if str(profile.get("mode", "")).strip().lower() != "ideal_layered":
        raise ValueError("当前活动结果不是 ideal_layered 分层大气模式")
    wavelength_nm = float(global_block.get("wavelength_nm", 1550.0))
    return profile, wavelength_nm


def _x_map(value: float, left: float, width: float, x_min: float, x_max: float) -> float:
    if x_max <= x_min:
        return left
    return left + (value - x_min) / (x_max - x_min) * width


def _y_map(value: float, top: float, height: float, y_min: float, y_max: float) -> float:
    if y_max <= y_min:
        return top + height
    return top + height - (value - y_min) / (y_max - y_min) * height


def _x_map_log(value: float, left: float, width: float, x_min: float, x_max: float) -> float:
    v = max(value, 1.0e-30)
    lo = math.log10(max(x_min, 1.0e-30))
    hi = math.log10(max(x_max, 1.0e-30))
    cur = math.log10(v)
    if hi <= lo:
        return left
    return left + (cur - lo) / (hi - lo) * width


def _path_from_xy(xs: list[float], ys: list[float]) -> str:
    points = [f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys)]
    return "M " + " L ".join(points)


def _series_path(values, ranges, left, top, width, height, vmin, vmax, rmin, rmax, *, log_x: bool) -> str:
    xs: list[float] = []
    ys: list[float] = []
    mapper = _x_map_log if log_x else _x_map
    for value, r in zip(values, ranges):
        xs.append(mapper(float(value), left, width, vmin, vmax))
        ys.append(_y_map(float(r), top, height, rmin, rmax))
    return _path_from_xy(xs, ys)


def _ticks_linear(vmin: float, vmax: float, count: int = 5) -> list[float]:
    if vmax <= vmin:
        return [vmin]
    step = (vmax - vmin) / max(count - 1, 1)
    return [vmin + i * step for i in range(count)]


def _ticks_log(vmin: float, vmax: float) -> list[float]:
    lo = int(math.floor(math.log10(max(vmin, 1.0e-30))))
    hi = int(math.ceil(math.log10(max(vmax, 1.0e-30))))
    return [10.0 ** p for p in range(lo, hi + 1)]


def _fmt_sci(value: float) -> str:
    return f"{value:.1e}"


def main() -> int:
    run_id, summary = _load_active_summary()
    profile, wavelength_nm = _resolve_profile_and_wavelength(summary)
    layered = build_ideal_layered_profile(
        wavelength_nm=wavelength_nm,
        profile=profile,
        range_max_m=float(profile.get("range_max_m", summary.get("global", {}).get("range_max_m", 30000.0))),
        range_step_m=float(profile.get("range_step_m", summary.get("global", {}).get("range_step_m", 1.0))),
    )

    ranges = [float(v) for v in layered.range_m]
    rmin = 0.0
    rmax = float(ranges[-1])

    struct_left = MARGIN_LEFT
    panel_gap = 30
    panel_width = (SVG_WIDTH - MARGIN_LEFT - MARGIN_RIGHT - 2 * panel_gap) / 3
    beta_left = struct_left + panel_width + panel_gap
    alpha_left = beta_left + panel_width + panel_gap
    top = MARGIN_TOP
    height = SVG_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM

    hb = float(profile["aerosol_boundary_scale_height_m"])
    rc = float(profile["aerosol_layer_center_m"])
    w = float(profile["aerosol_layer_width_m"])
    near_top = min(rmax, 2.5 * hb)
    high_lo = max(0.0, rc - 1.5 * w)
    high_hi = min(rmax, rc + 1.5 * w)

    beta_vals = {
        "分子后向散射": [float(v) for v in layered.beta_molecular_m_inv_sr],
        "气溶胶后向散射": [float(v) for v in layered.beta_aerosol_m_inv_sr],
        "总后向散射": [float(v) for v in layered.beta_total_m_inv_sr],
    }
    alpha_vals = {
        "分子消光": [float(v) for v in layered.alpha_molecular_m_inv],
        "气溶胶消光": [float(v) for v in layered.alpha_aerosol_m_inv],
        "总消光": [float(v) for v in layered.alpha_total_m_inv],
    }
    beta_min = min(min(v) for v in beta_vals.values())
    beta_max = max(max(v) for v in beta_vals.values())
    alpha_min = min(min(v) for v in alpha_vals.values())
    alpha_max = max(max(v) for v in alpha_vals.values())

    out_dir = Path(__file__).resolve().parent / "outputs_high_precision_latest" / "local" / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "fig15_layered_atmosphere_structure_overview.svg"

    colors = {
        "分子后向散射": "#3366cc",
        "气溶胶后向散射": "#cc3333",
        "总后向散射": "#228833",
        "分子消光": "#3366cc",
        "气溶胶消光": "#cc3333",
        "总消光": "#228833",
    }
    dashes = {
        "分子后向散射": "5,5",
        "气溶胶后向散射": "9,5",
        "总后向散射": "none",
        "分子消光": "5,5",
        "气溶胶消光": "9,5",
        "总消光": "none",
    }

    parts: list[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}">')
    parts.append('<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>')
    title = f"当前分层大气结构总览 | run = {run_id or 'local'} | λ = {wavelength_nm:.0f} nm"
    parts.append(f'<text x="{SVG_WIDTH/2:.1f}" y="34" text-anchor="middle" font-size="24" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#1f2937">{escape(title)}</text>')

    # Panel frames
    for left, name in ((struct_left, "当前大气结构示意"), (beta_left, "后向散射剖面"), (alpha_left, "消光剖面")):
        parts.append(f'<rect x="{left:.2f}" y="{top:.2f}" width="{panel_width:.2f}" height="{height:.2f}" fill="none" stroke="#cfd8e3" stroke-width="1.2"/>')
        parts.append(f'<text x="{left + panel_width / 2:.2f}" y="{top - 16:.2f}" text-anchor="middle" font-size="18" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#334155">{escape(name)}</text>')

    # Structure panel
    parts.append(f'<rect x="{struct_left:.2f}" y="{_y_map(near_top, top, height, rmin, rmax):.2f}" width="{panel_width:.2f}" height="{_y_map(0.0, top, height, rmin, rmax)-_y_map(near_top, top, height, rmin, rmax):.2f}" fill="#f7d9c4" opacity="0.88"/>')
    parts.append(f'<rect x="{struct_left:.2f}" y="{_y_map(high_hi, top, height, rmin, rmax):.2f}" width="{panel_width:.2f}" height="{_y_map(high_lo, top, height, rmin, rmax)-_y_map(high_hi, top, height, rmin, rmax):.2f}" fill="#d9e7ff" opacity="0.96"/>')
    parts.append(f'<line x1="{struct_left:.2f}" y1="{_y_map(rc, top, height, rmin, rmax):.2f}" x2="{struct_left + panel_width:.2f}" y2="{_y_map(rc, top, height, rmin, rmax):.2f}" stroke="#335c99" stroke-width="1.5" stroke-dasharray="7,5"/>')
    parts.append(f'<text x="{struct_left + 10:.2f}" y="{_y_map(rc, top, height, rmin, rmax) - 8:.2f}" font-size="13" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#335c99">高空层中心 = {rc:.0f} m</text>')
    parts.append(f'<text x="{struct_left + 10:.2f}" y="{_y_map(near_top, top, height, rmin, rmax) - 8:.2f}" font-size="13" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#8c4f1f">近地层特征高度 ≈ {hb:.0f} m</text>')
    parts.append(f'<text x="{struct_left + panel_width * 0.08:.2f}" y="{top + 28:.2f}" font-size="14" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#6b3f1f">近地气溶胶背景层</text>')
    parts.append(f'<text x="{struct_left + panel_width * 0.08:.2f}" y="{_y_map(high_hi, top, height, rmin, rmax) + 24:.2f}" font-size="14" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#274c8d">高空增强层</text>')

    # Structure y-axis ticks
    for tick in _ticks_linear(rmin, rmax, 7):
        y = _y_map(tick, top, height, rmin, rmax)
        parts.append(f'<line x1="{struct_left - 6:.2f}" y1="{y:.2f}" x2="{struct_left:.2f}" y2="{y:.2f}" stroke="#475569" stroke-width="1"/>')
        parts.append(f'<line x1="{struct_left:.2f}" y1="{y:.2f}" x2="{struct_left + panel_width:.2f}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{struct_left - 10:.2f}" y="{y + 4:.2f}" text-anchor="end" font-size="11" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#475569">{int(round(tick))}</text>')

    # Profile panels helper
    for left, xmin, xmax, title_key, values_map in (
        (beta_left, beta_min, beta_max, "beta", beta_vals),
        (alpha_left, alpha_min, alpha_max, "alpha", alpha_vals),
    ):
        ticks = _ticks_log(xmin, xmax)
        for tick in ticks:
            x = _x_map_log(tick, left, panel_width, xmin, xmax)
            parts.append(f'<line x1="{x:.2f}" y1="{top:.2f}" x2="{x:.2f}" y2="{top + height:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
            parts.append(f'<text x="{x:.2f}" y="{top + height + 18:.2f}" text-anchor="middle" font-size="10" font-family="Consolas, monospace" fill="#475569">{escape(_fmt_sci(tick))}</text>')
        for tick in _ticks_linear(rmin, rmax, 7):
            y = _y_map(tick, top, height, rmin, rmax)
            parts.append(f'<line x1="{left:.2f}" y1="{y:.2f}" x2="{left + panel_width:.2f}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
        for label, values in values_map.items():
            path = _series_path(values, ranges, left, top, panel_width, height, xmin, xmax, rmin, rmax, log_x=True)
            stroke_dash = dashes[label]
            dash_attr = "" if stroke_dash == "none" else f' stroke-dasharray="{stroke_dash}"'
            stroke_width = "3.0" if "总" in label else "2.1"
            parts.append(f'<path d="{path}" fill="none" stroke="{colors[label]}" stroke-width="{stroke_width}"{dash_attr}/>')
        # legend
        legend_x = left + 16
        legend_y = top + height - 66
        for i, label in enumerate(values_map.keys()):
            y = legend_y + i * 20
            dash_attr = "" if dashes[label] == "none" else f' stroke-dasharray="{dashes[label]}"'
            parts.append(f'<line x1="{legend_x:.2f}" y1="{y:.2f}" x2="{legend_x + 26:.2f}" y2="{y:.2f}" stroke="{colors[label]}" stroke-width="2.4"{dash_attr}/>')
            parts.append(f'<text x="{legend_x + 34:.2f}" y="{y + 4:.2f}" font-size="12" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#334155">{escape(label)}</text>')

    parts.append(f'<text x="{beta_left + panel_width / 2:.2f}" y="{SVG_HEIGHT - 14:.2f}" text-anchor="middle" font-size="12" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#334155">β(R)  (m^-1 sr^-1), 对数坐标</text>')
    parts.append(f'<text x="{alpha_left + panel_width / 2:.2f}" y="{SVG_HEIGHT - 14:.2f}" text-anchor="middle" font-size="12" font-family="Microsoft YaHei, SimHei, sans-serif" fill="#334155">α(R)  (m^-1), 对数坐标</text>')
    parts.append('</svg>')

    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"[done] saved layered structure overview: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
