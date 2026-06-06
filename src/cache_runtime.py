from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from .path_resolver import get_root as _get_root
except Exception:  # pragma: no cover - direct script imports use the flat src path.
    from path_resolver import get_root as _get_root


ROOT = _get_root()
DEFAULT_RESULT_ID = "default"
MAX_HISTORY = 10
CACHE_MAX_VERSIONS = 5
MAX_HISTORY_DISPLAY_NAME_CHARS = 40


@dataclass(frozen=True)
class PathLayout:
    root: Path

    @property
    def lidar_root(self) -> Path:
        return self.root / "temp" / "lidar_1d"

    @property
    def local_output_root(self) -> Path:
        return self.lidar_root / "outputs_high_precision_latest" / "local"

    @property
    def default_result_root(self) -> Path:
        return self.lidar_root / "default_result"

    @property
    def history_root(self) -> Path:
        return self.lidar_root / "run_history"

    @property
    def manifest_path(self) -> Path:
        return self.history_root / "manifest.json"

    @property
    def runtime_state_root(self) -> Path:
        return self.lidar_root / "runtime_state"

    @property
    def active_view_path(self) -> Path:
        return self.runtime_state_root / "active_view.json"

    @property
    def pending_run_path(self) -> Path:
        return self.runtime_state_root / "pending_run.json"

    @property
    def cache_store_root(self) -> Path:
        return self.lidar_root / "cache_store"

    @property
    def cache_runtime_root(self) -> Path:
        return self.cache_store_root / "runtime"

    @property
    def cache_seed_root(self) -> Path:
        return self.cache_store_root / "seeds"

    @property
    def cache_index_root(self) -> Path:
        return self.cache_store_root / "index"

    def run_dir(self, run_id: str) -> Path:
        return self.history_root / run_id

    def cache_tier_root(self, source: str, precision: str, *parts: str) -> Path:
        base = self.cache_runtime_root if source == "runtime" else self.cache_seed_root
        return base.joinpath(precision, *parts)

    def cache_index_path(self, layer: str) -> Path:
        return self.cache_index_root / f"{layer}_index.json"

    def indexed_artifact_path(self, layer: str, source: str, precision: str, semantic_key: str) -> Path:
        digest = hashlib.sha256(
            f"{layer}|{source}|{precision}|{semantic_key}".encode("utf-8")
        ).hexdigest()[:16]
        return self.cache_tier_root(source, precision, layer) / f"{digest}.json"


LAYOUT = PathLayout(ROOT)


def _normalize_manifest(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {"runs": [], "active_id": None}
    runs = data.get("runs")
    if not isinstance(runs, list):
        runs = []
    return {"runs": runs, "active_id": data.get("active_id")}


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        try:
            if path.exists() and path.is_file():
                path.unlink()
            tmp.replace(path)
        except PermissionError:
            path.write_text(text, encoding="utf-8")
            try:
                tmp.unlink()
            except Exception:
                pass


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary_sha256(summary_path: Path) -> str | None:
    return _sha256_file(summary_path)


def _summary_cache_identity(summary_path: Path) -> dict | None:
    summary = _read_json(summary_path, {})
    cache_identity = summary.get("cache_identity")
    return cache_identity if isinstance(cache_identity, dict) else None


def load_manifest() -> dict:
    return _normalize_manifest(_read_json(LAYOUT.manifest_path, {"runs": [], "active_id": None}))


def save_manifest(manifest: dict) -> None:
    _write_json_atomic(LAYOUT.manifest_path, _normalize_manifest(manifest))


def sanitize_history_display_name(name: object) -> str:
    cleaned = " ".join(str(name or "").split())
    return cleaned[:MAX_HISTORY_DISPLAY_NAME_CHARS]


def history_entry_custom_name(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return ""
    if "display_name" in entry:
        return sanitize_history_display_name(entry.get("display_name"))
    label = sanitize_history_display_name(entry.get("label"))
    if not label:
        return ""
    if label in {str(entry.get("timestamp") or ""), str(entry.get("id") or "")}:
        return ""
    return label


def history_record_label(run_id: str, entry: dict | None = None) -> str:
    if run_id == DEFAULT_RESULT_ID:
        return "默认结果"
    name = history_entry_custom_name(entry)
    return name or run_id


def rename_history_run(run_id: str, display_name: object) -> dict:
    if run_id == DEFAULT_RESULT_ID:
        raise ValueError("默认结果不可重命名")
    manifest = load_manifest()
    for entry in manifest.get("runs", []):
        if entry.get("id") != run_id:
            continue
        if not run_exists(run_id):
            raise FileNotFoundError(f"运行结果不存在：{run_id}")
        cleaned = sanitize_history_display_name(display_name)
        if cleaned:
            entry["display_name"] = cleaned
            entry["name_updated_at"] = time.time()
        else:
            entry["display_name"] = ""
            entry["name_updated_at"] = time.time()
        save_manifest(manifest)
        return entry
    raise FileNotFoundError(f"历史记录不存在：{run_id}")


def run_exists(run_id: str) -> bool:
    if run_id == DEFAULT_RESULT_ID:
        return (LAYOUT.default_result_root / "summary.json").exists()
    run_dir = LAYOUT.run_dir(run_id)
    return run_dir.exists() and (run_dir / "summary.json").exists()


def ensure_manifest_cache_identity() -> None:
    try:
        from backfill_cache_identity import backfill_one
    except Exception:
        return

    manifest = load_manifest()
    changed = False
    for entry in manifest.get("runs", []):
        run_id = entry.get("id")
        if not run_id:
            continue
        run_dir = LAYOUT.run_dir(run_id)
        summary_path = run_dir / "summary.json"
        params_path = run_dir / "params.json"
        if not summary_path.exists():
            continue
        cache_identity = entry.get("cache_identity")
        if not (isinstance(cache_identity, dict) and cache_identity.get("identity") and not cache_identity.get("legacy")):
            try:
                backfill_one(summary_path, params_path if params_path.exists() else None)
            except Exception:
                pass
            cache_identity = _summary_cache_identity(summary_path)
            if isinstance(cache_identity, dict) and cache_identity.get("identity"):
                entry["cache_identity"] = cache_identity
                changed = True
        summary_sha256 = _summary_sha256(summary_path)
        if summary_sha256 and entry.get("summary_sha256") != summary_sha256:
            entry["summary_sha256"] = summary_sha256
            changed = True
    if changed:
        save_manifest(manifest)


def load_active_view() -> dict:
    active = _read_json(LAYOUT.active_view_path, {})
    run_id = active.get("run_id") if isinstance(active, dict) else None
    if isinstance(run_id, str) and run_exists(run_id):
        return {"run_id": run_id, "updated_at": active.get("updated_at")}

    manifest = load_manifest()
    candidate = manifest.get("active_id")
    if not isinstance(candidate, str) or not run_exists(candidate):
        if (LAYOUT.default_result_root / "summary.json").exists():
            candidate = DEFAULT_RESULT_ID
        else:
            candidate = None
            for entry in manifest.get("runs", []):
                run_id = entry.get("id")
                if isinstance(run_id, str) and run_exists(run_id):
                    candidate = run_id
                    break
    if candidate:
        return set_active_view(candidate)
    return {"run_id": None, "updated_at": None}


def set_active_view(run_id: str) -> dict:
    if not run_exists(run_id):
        raise FileNotFoundError(f"运行结果不存在：{run_id}")
    payload = {"run_id": run_id, "updated_at": time.time()}
    _write_json_atomic(LAYOUT.active_view_path, payload)
    manifest = load_manifest()
    manifest["active_id"] = run_id
    save_manifest(manifest)
    return payload


def get_active_view_id() -> str | None:
    return load_active_view().get("run_id")


def get_run_paths(run_id: str | None = None) -> dict[str, Path]:
    resolved = run_id or get_active_view_id()
    if resolved == DEFAULT_RESULT_ID:
        root = LAYOUT.default_result_root
    elif resolved:
        root = LAYOUT.run_dir(resolved)
    else:
        root = LAYOUT.local_output_root
    return {
        "run_id": resolved,
        "root": root,
        "data": root / "data",
        "figures": root / "figures",
        "summary": root / "summary.json",
        "params": root / "params.json",
    }


def load_run_summary(run_id: str | None = None) -> dict:
    return _read_json(get_run_paths(run_id)["summary"], {})


def load_run_params(run_id: str | None = None) -> dict:
    return _read_json(get_run_paths(run_id)["params"], {})


def _history_timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _unique_run_id() -> tuple[str, str]:
    ts = _history_timestamp()
    run_id = f"run_{ts}"
    suffix = 1
    while LAYOUT.run_dir(run_id).exists():
        run_id = f"run_{ts}_{suffix:02d}"
        suffix += 1
    return run_id, ts


def _copy_output_tree(src_root: Path, dst_root: Path) -> None:
    dst_root.mkdir(parents=True, exist_ok=True)
    for folder in ("data", "figures"):
        src_dir = src_root / folder
        dst_dir = dst_root / folder
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src_dir.exists():
            for item in src_dir.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst_dir / item.name)


def _find_duplicate_history_entry(identity: str | None, summary_sha256: str | None) -> dict | None:
    manifest = load_manifest()
    for entry in manifest.get("runs", []):
        entry_identity = ((entry.get("cache_identity") or {}).get("identity"))
        entry_sha = entry.get("summary_sha256")
        if summary_sha256 and entry_sha == summary_sha256 and (identity is None or entry_identity == identity):
            return entry
    return None


def _validate_result_root(root: Path) -> tuple[bool, str | None]:
    summary = root / "summary.json"
    data_dir = root / "data"
    figures_dir = root / "figures"
    if not summary.exists():
        return False, "summary_missing"
    summary_data = _read_json(summary, None)
    if not isinstance(summary_data, dict):
        return False, "summary_invalid"
    if not data_dir.exists() or not any(data_dir.glob("*.csv")):
        return False, "data_missing"
    if not figures_dir.exists() or not any(figures_dir.glob("*")):
        return False, "figures_missing"
    return True, None


def archive_local_result(params: dict, *, label: str = "", precision: str | None = None, origin: str = "manual") -> dict[str, object]:
    src_root = LAYOUT.local_output_root
    ok, reason = _validate_result_root(src_root)
    if not ok:
        raise RuntimeError(f"当前 local 输出不完整：{reason}")

    src_summary = src_root / "summary.json"
    cache_identity = _summary_cache_identity(src_summary)
    summary_sha256 = _summary_sha256(src_summary)
    identity = cache_identity.get("identity") if isinstance(cache_identity, dict) else None
    duplicate = _find_duplicate_history_entry(identity, summary_sha256)
    if duplicate is not None:
        run_id = duplicate.get("id")
        if isinstance(run_id, str) and run_exists(run_id):
            set_active_view(run_id)
            return {"run_id": run_id, "created": False, "duplicate": True}

    run_id, ts = _unique_run_id()
    dst_root = LAYOUT.run_dir(run_id)
    _copy_output_tree(src_root, dst_root)
    shutil.copy2(src_summary, dst_root / "summary.json")

    params_with_precision = dict(params or {})
    if precision and "precision" not in params_with_precision:
        params_with_precision["precision"] = precision
    _write_json_atomic(dst_root / "params.json", params_with_precision)

    manifest = load_manifest()
    entry = {
        "id": run_id,
        "timestamp": ts,
        "label": label or ts,
        "precision": precision or params_with_precision.get("precision"),
        "origin": origin,
        "summary_sha256": summary_sha256,
    }
    if isinstance(cache_identity, dict):
        entry["cache_identity"] = cache_identity
    manifest["runs"].insert(0, entry)
    while len(manifest["runs"]) > MAX_HISTORY:
        old = manifest["runs"].pop()
        old_id = old.get("id")
        if isinstance(old_id, str):
            old_dir = LAYOUT.run_dir(old_id)
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
    manifest["active_id"] = run_id
    save_manifest(manifest)
    set_active_view(run_id)
    return {"run_id": run_id, "created": True, "duplicate": False}


def begin_pending_run(info: dict[str, object]) -> None:
    payload = dict(info)
    payload["status"] = "pending"
    payload["updated_at"] = time.time()
    _write_json_atomic(LAYOUT.pending_run_path, payload)


def read_pending_run() -> dict | None:
    pending = _read_json(LAYOUT.pending_run_path, None)
    return pending if isinstance(pending, dict) and pending else None


def clear_pending_run() -> None:
    if LAYOUT.pending_run_path.exists():
        LAYOUT.pending_run_path.unlink()


def recover_pending_local_result() -> dict[str, object]:
    pending = read_pending_run()
    if pending is None:
        return {"recovered": False, "reason": "no_pending"}

    src_root = LAYOUT.local_output_root
    ok, reason = _validate_result_root(src_root)
    if not ok:
        return {"recovered": False, "reason": reason}

    summary_path = src_root / "summary.json"
    cache_identity = _summary_cache_identity(summary_path)
    summary_sha256 = _summary_sha256(summary_path)
    identity = cache_identity.get("identity") if isinstance(cache_identity, dict) else None
    duplicate = _find_duplicate_history_entry(identity, summary_sha256)
    if duplicate is not None:
        run_id = duplicate.get("id")
        if isinstance(run_id, str) and run_exists(run_id):
            set_active_view(run_id)
            clear_pending_run()
            return {"recovered": False, "reason": "duplicate_history", "run_id": run_id}

    result = archive_local_result(
        pending.get("params", {}) if isinstance(pending.get("params"), dict) else {},
        label=str(pending.get("label") or "自动恢复"),
        precision=str(pending.get("precision")) if pending.get("precision") else None,
        origin="recovered",
    )
    clear_pending_run()
    return {"recovered": True, **result}


def build_run_index() -> dict:
    ensure_manifest_cache_identity()
    by_identity: dict[str, list[dict[str, object]]] = {}
    by_optical: dict[tuple, list[dict[str, object]]] = {}
    manifest = load_manifest()

    def _add_record(run_id: str, label: str, precision: str | None, cache_identity: dict) -> None:
        if not (isinstance(cache_identity, dict) and cache_identity.get("identity") and not cache_identity.get("legacy")):
            return
        record = {
            "run_id": run_id,
            "label": label,
            "precision": precision or cache_identity.get("precision_profile"),
            "cache_identity": cache_identity,
        }
        ident = cache_identity["identity"]
        by_identity.setdefault(ident, []).append(record)
        opt_key = (
            cache_identity.get("fog_key"),
            cache_identity.get("haze_key"),
            cache_identity.get("haze_mueller_key"),
            cache_identity.get("rain_key"),
            record["precision"],
        )
        by_optical.setdefault(opt_key, []).append(record)

    for entry in manifest.get("runs", []):
        run_id = entry.get("id")
        if isinstance(run_id, str):
            _add_record(
                run_id,
                history_record_label(run_id, entry),
                entry.get("precision"),
                entry.get("cache_identity") or {},
            )

    default_summary = LAYOUT.default_result_root / "summary.json"
    if default_summary.exists():
        cache_identity = _summary_cache_identity(default_summary)
        if isinstance(cache_identity, dict):
            _add_record(DEFAULT_RESULT_ID, "默认结果", cache_identity.get("precision_profile"), cache_identity)

    return {"by_identity": by_identity, "by_optical": by_optical}


def classify_identity_match(current: dict, index: dict) -> tuple[str, dict | None]:
    if not current:
        return "miss", None
    by_identity = index.get("by_identity", {})
    exact = by_identity.get(current.get("identity"))
    if exact:
        return "full", exact[0]
    opt_key = (
        current.get("fog_key"),
        current.get("haze_key"),
        current.get("haze_mueller_key"),
        current.get("rain_key"),
        current.get("precision_profile"),
    )
    candidates = index.get("by_optical", {}).get(opt_key, [])
    if candidates:
        return "optical_only", candidates[0]
    return "miss", None


def cache_index_path(layer: str) -> Path:
    return LAYOUT.cache_index_path(layer)


def cache_tier_root(source: str, precision: str, *parts: str) -> Path:
    return LAYOUT.cache_tier_root(source, precision, *parts)


def load_cache_index(layer: str) -> dict:
    path = cache_index_path(layer)
    data = _read_json(path, {"records": []})
    if not isinstance(data, dict):
        return {"records": []}
    records = data.get("records")
    if not isinstance(records, list):
        records = []
    return {"records": records}


def save_cache_index(layer: str, data: dict) -> None:
    path = cache_index_path(layer)
    _write_json_atomic(path, {"records": data.get("records", [])})


def upsert_cache_index(layer: str, record: dict[str, object]) -> None:
    index = load_cache_index(layer)
    records = [
        entry for entry in index.get("records", [])
        if not (
            entry.get("semantic_key") == record.get("semantic_key")
            and entry.get("precision_profile") == record.get("precision_profile")
            and entry.get("source") == record.get("source")
            and entry.get("node_type") == record.get("node_type")
        )
    ]
    records.append(record)
    save_cache_index(layer, {"records": records})


def record_cache_artifact(
    layer: str,
    *,
    precision: str,
    semantic_key: str,
    artifact_path: Path,
    source: str,
    visibility: str,
    input_hashes: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    payload = {
        "node_type": layer,
        "precision_profile": precision,
        "semantic_key": semantic_key,
        "input_hashes": input_hashes or {},
        "output_hash": _sha256_file(artifact_path),
        "artifact_path": str(artifact_path),
        "source": source,
        "visibility": visibility,
        "created_at": time.time(),
        "status": "ok" if artifact_path.exists() else "missing",
    }
    if extra:
        payload.update(extra)
    upsert_cache_index(layer, payload)


def _load_json_or_none(path: Path) -> dict | None:
    data = _read_json(path, None)
    return data if isinstance(data, dict) else None


def load_mueller_cache(cache_dir: Path, haze_mueller_key: str) -> dict:
    stored = _load_json_or_none(cache_dir / "mueller_cache.json")
    if stored is None:
        return {}
    bucket = stored.get(haze_mueller_key, {})
    return bucket if isinstance(bucket, dict) else {}


def _evict_old_versions(versions: dict) -> None:
    if len(versions) > CACHE_MAX_VERSIONS:
        excess = len(versions) - CACHE_MAX_VERSIONS
        for key in list(versions.keys())[:excess]:
            del versions[key]


def save_mueller_cache(cache_dir: Path, haze_mueller_key: str, mc: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_file = cache_dir / "mueller_cache.json"
    stored = _load_json_or_none(data_file) or {}
    bucket = stored.setdefault(haze_mueller_key, {})
    if isinstance(bucket, dict):
        bucket.update(mc)
    else:
        stored[haze_mueller_key] = dict(mc)
    _evict_old_versions(stored)
    _write_json_atomic(data_file, stored)


def load_optical_cache(cache_dir: Path, fog_key: str, haze_key: str, rain_key: str) -> dict:
    stored = _load_json_or_none(cache_dir / "optical_cache.json")
    if stored is None:
        return {}
    result: dict = {}
    for group, key in (("fog", fog_key), ("haze", haze_key), ("rain", rain_key)):
        versions = stored.get(f"{group}_versions", {})
        if isinstance(versions, dict) and key in versions:
            result[group] = versions[key]
    return result


def save_optical_cache(cache_dir: Path, fog_key: str, haze_key: str, rain_key: str, oc: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_file = cache_dir / "optical_cache.json"
    stored = _load_json_or_none(data_file) or {}
    fog_versions = stored.setdefault("fog_versions", {})
    haze_versions = stored.setdefault("haze_versions", {})
    rain_versions = stored.setdefault("rain_versions", {})
    if "fog" in oc:
        fog_versions[fog_key] = oc["fog"]
        _evict_old_versions(fog_versions)
    if "haze" in oc:
        haze_versions[haze_key] = oc["haze"]
        _evict_old_versions(haze_versions)
    if "rain" in oc:
        rain_versions[rain_key] = oc["rain"]
        _evict_old_versions(rain_versions)
    for legacy in ("fog", "haze", "rain", "_hashes"):
        stored.pop(legacy, None)
    _write_json_atomic(data_file, stored)


def load_indexed_layer_artifact(layer: str, precision: str, semantic_key: str) -> tuple[dict | None, dict[str, object]]:
    index = load_cache_index(layer)
    info: dict[str, object] = {
        "layer": layer,
        "precision_profile": precision,
        "semantic_key": semantic_key,
        "hit": False,
        "hit_source": None,
        "artifact_path": None,
        "candidate_records": [],
        "miss_reason": None,
    }
    records = [
        rec for rec in index.get("records", [])
        if rec.get("precision_profile") == precision
        and rec.get("semantic_key") == semantic_key
        and rec.get("status") == "ok"
    ]
    info["candidate_records"] = records
    for source in ("runtime", "seed"):
        source_records = [rec for rec in records if rec.get("source") == source]
        if not source_records:
            continue
        artifact_path = LAYOUT.indexed_artifact_path(layer, source, precision, semantic_key)
        if not artifact_path.exists():
            info["miss_reason"] = "cache_file_missing"
            continue
        artifact = _load_json_or_none(artifact_path)
        if artifact is None:
            info["miss_reason"] = "json_read_failed"
            continue
        info["hit"] = True
        info["hit_source"] = source
        info["artifact_path"] = str(artifact_path)
        return artifact, info
    info["miss_reason"] = info["miss_reason"] or ("cache_file_missing" if records else "key_not_found")
    return None, info


def save_indexed_layer_artifact(
    layer: str,
    precision: str,
    semantic_key: str,
    artifact: dict[str, object],
    *,
    source: str = "runtime",
    visibility: str = "hidden",
    input_hashes: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    artifact_path = LAYOUT.indexed_artifact_path(layer, source, precision, semantic_key)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(artifact_path, artifact)
    record_cache_artifact(
        layer,
        precision=precision,
        semantic_key=semantic_key,
        artifact_path=artifact_path,
        source=source,
        visibility=visibility,
        input_hashes=input_hashes,
        extra=extra,
    )
    return artifact_path


def optical_cache_lookup_details(cache_dir: Path, group: str, cache_key: str) -> dict[str, object]:
    data_file = cache_dir / "optical_cache.json"
    info: dict[str, object] = {
        "cache_file": str(data_file),
        "cache_file_exists": data_file.exists(),
        "cache_key": cache_key,
        "version_count": 0,
        "candidate_exists": False,
        "read_ok": False,
        "miss_reason": None,
    }
    stored = _load_json_or_none(data_file)
    if stored is None:
        info["miss_reason"] = "cache_file_missing" if not data_file.exists() else "json_read_failed"
        return info
    versions = stored.get(f"{group}_versions", {})
    if not isinstance(versions, dict):
        versions = {}
    info["read_ok"] = True
    info["version_count"] = len(versions)
    info["candidate_exists"] = cache_key in versions
    if not info["candidate_exists"]:
        info["miss_reason"] = "key_not_found"
    return info


def mueller_cache_lookup_details(cache_dir: Path, cache_key: str) -> dict[str, object]:
    data_file = cache_dir / "mueller_cache.json"
    info: dict[str, object] = {
        "cache_file": str(data_file),
        "cache_file_exists": data_file.exists(),
        "cache_key": cache_key,
        "version_count": 0,
        "candidate_exists": False,
        "read_ok": False,
        "miss_reason": None,
    }
    stored = _load_json_or_none(data_file)
    if stored is None:
        info["miss_reason"] = "cache_file_missing" if not data_file.exists() else "json_read_failed"
        return info
    info["read_ok"] = True
    info["version_count"] = len(stored)
    info["candidate_exists"] = cache_key in stored
    if not info["candidate_exists"]:
        info["miss_reason"] = "key_not_found"
    return info


def resolve_lookup_miss_reason(local_info: dict[str, object], fallback_info: dict[str, object]) -> str | None:
    if bool(local_info.get("candidate_exists")) or bool(fallback_info.get("candidate_exists")):
        return None
    if local_info.get("miss_reason") == "json_read_failed":
        return "json_read_failed"
    if fallback_info.get("miss_reason") == "json_read_failed":
        return "json_read_failed"
    return fallback_info.get("miss_reason") or local_info.get("miss_reason")


def _optical_layer_source(group: str, precision: str, cache_key: str) -> str:
    local_cache_dir = LAYOUT.local_output_root / "optical_cache"
    local_hit = load_optical_cache(
        local_cache_dir,
        cache_key if group == "fog" else "",
        cache_key if group == "haze" else "",
        cache_key if group == "rain" else "",
    )
    if group in local_hit:
        return "local"
    artifact, info = load_indexed_layer_artifact(
        {"fog": "fog_scene", "haze": "haze_scene", "rain": "rain_scene"}[group],
        precision,
        cache_key,
    )
    if artifact is not None:
        return str(info.get("hit_source") or "runtime")
    default_cache_dir = LAYOUT.default_result_root / "optical_cache"
    default_hit = load_optical_cache(
        default_cache_dir,
        cache_key if group == "fog" else "",
        cache_key if group == "haze" else "",
        cache_key if group == "rain" else "",
    )
    if group in default_hit:
        return "default_result"
    return "miss"


def _mueller_layer_source(precision: str, cache_key: str) -> str:
    local_cache_dir = LAYOUT.local_output_root / "optical_cache"
    if load_mueller_cache(local_cache_dir, cache_key):
        return "local"
    artifact, info = load_indexed_layer_artifact("haze_mueller", precision, cache_key)
    if artifact is not None:
        return str(info.get("hit_source") or "runtime")
    default_cache_dir = LAYOUT.default_result_root / "optical_cache"
    if load_mueller_cache(default_cache_dir, cache_key):
        return "default_result"
    return "miss"


def _source_text(source: str) -> str:
    return {
        "local": "当前输出缓存",
        "runtime": "运行缓存",
        "seed": "默认缓存",
        "default_result": "默认展示缓存",
        "miss": "未命中",
    }.get(source, source)


def _plan_label(plan_type: str) -> str:
    return {
        "full_hit": "命中完整结果",
        "output_only": "优先重建输出",
        "partial_recompute": "部分复用并补算",
        "full_recompute": "全量重算",
        "unknown": "状态未识别",
    }.get(plan_type, plan_type)


def build_execution_plan(
    current: dict | None,
    *,
    precision: str,
    physics_changed: bool | None = None,
    instrument_changed: bool | None = None,
) -> dict[str, object]:
    if not current:
        return {
            "plan_type": "full_recompute",
            "headline": _plan_label("full_recompute"),
            "summary_lines": ["当前参数无法生成缓存键", "将完整重算"],
            "matched_record": None,
            "layer_status": {},
        }

    index = build_run_index()
    match_status, record = classify_identity_match(current, index)
    if match_status == "full" and record:
        label = str(record.get("label") or record.get("run_id") or "已有结果")
        return {
            "plan_type": "full_hit",
            "headline": _plan_label("full_hit"),
            "summary_lines": [f"完整结果：命中 {label}", "将直接切换，不执行重算"],
            "matched_record": record,
            "layer_status": {"result": "hit"},
        }

    if physics_changed is False and instrument_changed is True:
        return {
            "plan_type": "output_only",
            "headline": _plan_label("output_only"),
            "summary_lines": ["仅仪器参数变化", "将优先复用光学缓存并重建输出"],
            "matched_record": record,
            "layer_status": {"result": "miss", "instrument_only": True},
        }

    fog_source = _optical_layer_source("fog", precision, str(current.get("fog_key") or ""))
    haze_source = _optical_layer_source("haze", precision, str(current.get("haze_key") or ""))
    rain_source = _optical_layer_source("rain", precision, str(current.get("rain_key") or ""))
    mueller_source = _mueller_layer_source(precision, str(current.get("haze_mueller_key") or ""))
    layer_status = {
        "result": "miss",
        "fog_optical": fog_source,
        "haze_optical": haze_source,
        "rain_optical": rain_source,
        "haze_mueller": mueller_source,
    }
    hits = {
        "fog": fog_source != "miss",
        "haze": haze_source != "miss",
        "rain": rain_source != "miss",
        "mueller": mueller_source != "miss",
    }
    hit_count = sum(1 for value in hits.values() if value)
    if hit_count == 0:
        return {
            "plan_type": "full_recompute",
            "headline": _plan_label("full_recompute"),
            "summary_lines": ["完整结果：未命中", "主要散射缓存：未命中"],
            "matched_record": record,
            "layer_status": layer_status,
        }

    summary_lines: list[str] = ["完整结果：未命中"]
    optical_hits = [name for name in ("fog", "haze", "rain") if hits[name]]
    optical_misses = [name for name in ("fog", "haze", "rain") if not hits[name]]
    name_map = {"fog": "雾光学", "haze": "霾光学", "rain": "雨光学"}
    if optical_hits and not optical_misses:
        summary_lines.append("雾/霾/雨光学：缓存已命中")
    elif optical_hits:
        summary_lines.append(
            f"{'/'.join(name_map[name] for name in optical_hits)}：已命中；"
            f"{'/'.join(name_map[name] for name in optical_misses)}：需补算"
        )
    else:
        summary_lines.append("雾/霾/雨光学：未命中")
    if not hits["haze"] or not hits["mueller"]:
        summary_lines.append(
            f"霾相关高耗时层：Mueller{_source_text(mueller_source)}"
            if hits["mueller"]
            else "霾相关高耗时层：需补算"
        )
    else:
        summary_lines.append("霾相关高耗时层：缓存已命中")

    return {
        "plan_type": "partial_recompute",
        "headline": _plan_label("partial_recompute"),
        "summary_lines": summary_lines[:3],
        "matched_record": record,
        "layer_status": layer_status,
    }


def active_view_report() -> dict[str, object]:
    active = load_active_view()
    run_id = active.get("run_id")
    paths = get_run_paths(run_id)
    return {
        "run_id": run_id,
        "summary_path": str(paths["summary"]),
        "data_dir": str(paths["data"]),
        "figures_dir": str(paths["figures"]),
    }


def _cmd_stats() -> int:
    ensure_manifest_cache_identity()
    manifest = load_manifest()
    print(json.dumps({
        "active_view": active_view_report(),
        "pending_run": read_pending_run(),
        "history_runs": len(manifest.get("runs", [])),
        "history_ids": [entry.get("id") for entry in manifest.get("runs", [])],
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_repair_state() -> int:
    ensure_manifest_cache_identity()
    active = load_active_view()
    print(json.dumps({"active_view": active, "pending_run": read_pending_run()}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cache/runtime state maintenance helpers.")
    parser.add_argument("command", choices=["stats", "repair-state"])
    args = parser.parse_args(argv)
    if args.command == "stats":
        return _cmd_stats()
    return _cmd_repair_state()


if __name__ == "__main__":
    raise SystemExit(main())
