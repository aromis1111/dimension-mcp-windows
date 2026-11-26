"""adobe-dimension MCP server (Windows).

Control method: Chrome DevTools Protocol into Dimension's Electron renderer.
Dimension is started with --remote-debugging-port, and the tools call Dimension's
own controllers (documentController, renderController, sceneGraphController).

Safety rules (design spec from cavi-ai/adobe-mcp, adapted):
- Startup and tool listing never launch Dimension. Only dimension_launch does.
- Every scene is an owned copy inside ADOBE_MCP_WORKSPACE/scenes. Originals are only read.
- Unsaved changes are discarded only for workspace copies; anything else is refused.
- Each native operation is logged to ADOBE_MCP_WORKSPACE/logs/dimension-ops.jsonl before it runs.
- If Dimension stops answering (usually a modal dialog), the result is "unknown". No blind retry.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import dimension_js as JS
from .cdp import CDP, CDPError, CDPTimeout, CDPUnavailable

DEFAULT_EXE = r"C:\Program Files\Adobe\Adobe Dimension\Adobe Dimension.exe"
PROCESS_NAME = "Adobe Dimension.exe"
QUALITY = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


# ---------------------------------------------------------------- config
def _workspace() -> Path:
    raw = os.environ.get("ADOBE_MCP_WORKSPACE", "").strip()
    if not raw:
        raise SystemExit("ADOBE_MCP_WORKSPACE is required (absolute path to the workspace folder).")
    p = Path(raw)
    if not p.is_absolute():
        raise SystemExit(f"ADOBE_MCP_WORKSPACE must be an absolute path, got: {raw}")
    for sub in ("scenes", "renders", "logs"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


WORKSPACE = _workspace()
SCENES = WORKSPACE / "scenes"
RENDERS = WORKSPACE / "renders"
LOG = WORKSPACE / "logs" / "dimension-ops.jsonl"
EXE = Path(os.environ.get("ADOBE_DIMENSION_EXE", DEFAULT_EXE))
PORT = int(os.environ.get("ADOBE_DIMENSION_CDP_PORT", "9333"))
cdp = CDP(PORT)

mcp = FastMCP("adobe-dimension")


# ---------------------------------------------------------------- helpers
def _log(op: str, phase: str, **data) -> None:
    rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "op": op, "phase": phase, **data}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _in_workspace(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(WORKSPACE.resolve())
    except (OSError, ValueError):
        return False


def _process_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return PROCESS_NAME.lower() in out.lower()


def _unknown(op: str, err: Exception) -> dict:
    _log(op, "unknown", error=str(err))
    return {
        "status": "unknown",
        "detail": f"Dimension did not answer ({err}). It may be showing a dialog or still be busy. "
                  "Look at the Dimension window, then call dimension_status. Do not retry blindly.",
    }


def _not_connected() -> dict:
    if _process_running():
        return {"status": "error", "detail": "Dimension is running but not on the control port "
                f"{PORT}. It was opened normally. Save your work, close Dimension, then call dimension_launch."}
    return {"status": "error", "detail": "Dimension is not running. Call dimension_launch first."}


def _state() -> dict | None:
    """App state, or None if the app object isn't ready. Raises CDPUnavailable/CDPTimeout."""
    st = cdp.eval_json(JS.STATE, timeout=15)
    if not isinstance(st, dict) or not st.get("app_ready"):
        return None
    return st


def _run_op(op: str, chain: str, timeout: float) -> dict:
    """Start a promise chain in the page and poll until it settles."""
    op_id = uuid.uuid4().hex[:8]
    started = cdp.eval_json(JS.start_op(op_id, chain), timeout=15)
    if not isinstance(started, dict) or not started.get("started"):
        raise CDPError(f"could not start {op}: {started}")
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(1.0)
        last = cdp.eval_json(JS.POLL_OP, timeout=15)
        if isinstance(last, dict) and last.get("id") == op_id and last.get("done"):
            return last
    raise CDPTimeout(f"{op} still running after {timeout:.0f}s (last phase: {(last or {}).get('phase')})")


# ---------------------------------------------------------------- session lock
# Each Claude session runs its own copy of this server, but there is only one Dimension.
# The session that opens a copy owns Dimension until it closes the copy or exits.
LOCK = WORKSPACE / "logs" / "session.lock"
MY_PID = os.getpid()


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True  # can't tell: assume alive, the safe side
    return f'"{int(pid)}"' in out


def _other_owner() -> dict | None:
    """The lock record if another live session owns Dimension, else None (stale locks are ignored)."""
    try:
        rec = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = rec.get("pid")
    if not pid or pid == MY_PID or not _pid_alive(pid):
        return None
    return rec


def _take_lock(document: str) -> None:
    LOCK.write_text(json.dumps({"pid": MY_PID, "document": document,
                                "since": _dt.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")


def _release_lock() -> None:
    try:
        if json.loads(LOCK.read_text(encoding="utf-8")).get("pid") == MY_PID:
            LOCK.unlink()
    except (OSError, ValueError):
        pass


atexit.register(_release_lock)


def _busy_elsewhere(op: str) -> dict | None:
    owner = _other_owner()
    if not owner:
        return None
    _log(op, "refused_locked", owner=owner)
    return {"status": "error", "detail": f"another Claude session (server pid {owner['pid']}) is using Dimension "
            f"with {owner.get('document')} since {owner.get('since')}. Finish there with dimension_close_copy, "
            "or close that session, then try again."}


def _png_size(path: Path) -> list[int] | None:
    try:
        with path.open("rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return list(struct.unpack(">II", head[16:24]))
    except OSError:
        return None


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s[:120] or "render"


# ---------------------------------------------------------------- tools
@mcp.tool()
def dimension_status() -> dict:
    """Report whether Dimension is installed, running, and reachable, and what it has open.
    Never launches Dimension."""
    info = {
        "exe": str(EXE), "installed": EXE.exists(), "workspace": str(WORKSPACE),
        "control_port": PORT, "running": _process_running(), "connected": False,
    }
    owner = _other_owner()
    if owner:
        info["owned_by_other_session"] = owner
    try:
        st = _state()
        info["connected"] = True
        if st is None:
            info["app"] = "starting (UI not ready yet)"
        else:
            st.pop("app_ready", None)
            st["document_is_workspace_copy"] = _in_workspace(st.get("document_path"))
            info["app"] = st
    except CDPUnavailable:
        if info["running"]:
            info["note"] = "Dimension is running without the control port. Close it, then use dimension_launch."
    except CDPTimeout as e:
        info["connected"] = "unknown"
        info["note"] = f"Control port open but the page is not answering ({e}); a dialog may be open."
    return info


@mcp.tool()
def dimension_launch(wait_seconds: int = 150) -> dict:
    """Start Dimension with the control port and wait until its UI is ready.
    Refuses if Dimension is already running without the control port (it won't kill your session)."""
    if cdp.available():
        return {"status": "ok", "detail": "already running with control port", "state": dimension_status().get("app")}
    if _process_running():
        return _not_connected()
    if not EXE.exists():
        return {"status": "error", "detail": f"Dimension not found at {EXE}. Set ADOBE_DIMENSION_EXE."}
    _log("launch", "start", exe=str(EXE), port=PORT)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(EXE), f"--remote-debugging-port={PORT}"], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + max(20, wait_seconds)
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = _state()
            if st:
                time.sleep(3)  # let the start screen finish wiring up
                _log("launch", "done", state=st)
                return {"status": "ok", "detail": "Dimension launched", "state": st}
        except (CDPUnavailable, CDPTimeout, CDPError):
            pass
    _log("launch", "unknown")
    return {"status": "unknown", "detail": f"Dimension started but its UI was not ready after {wait_seconds}s. Call dimension_status."}


@mcp.tool()
def dimension_open_copy(source_path: str, wait_seconds: int = 600) -> dict:
    """Copy a .dn scene into the workspace and open the copy. The original is never opened or changed.
    If source_path is already inside the workspace scenes folder, it is opened as is.
    Refuses if the currently open document has unsaved changes and is not a workspace copy."""
    src = Path(source_path)
    if src.suffix.lower() != ".dn" or not src.is_file():
        return {"status": "error", "detail": f"not an existing .dn file: {source_path}"}
    if (refusal := _busy_elsewhere("open_copy")):
        return refusal
    try:
        st = _state()
    except CDPUnavailable:
        return _not_connected()
    except CDPTimeout as e:
        return _unknown("open_copy", e)
    if st is None:
        return {"status": "error", "detail": "Dimension UI is not ready yet. Call dimension_status."}
    if st["is_rendering"]:
        return {"status": "error", "detail": "a render is running. Wait for it or check dimension_render_inspect."}

    if _in_workspace(str(src)):
        copy = src
        copied = False
    else:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        copy = SCENES / f"{_safe_name(src.stem)}__{stamp}.dn"
        _log("open_copy", "copy", source=str(src), copy=str(copy))
        src_stat = src.stat()
        shutil.copy2(src, copy)
        if copy.stat().st_size != src_stat.st_size:
            return {"status": "error", "detail": "copy size does not match the original; copy not opened", "copy": str(copy)}
        (copy.with_suffix(".source.json")).write_text(json.dumps({
            "source": str(src), "size": src_stat.st_size,
            "source_mtime": _dt.datetime.fromtimestamp(src_stat.st_mtime).isoformat(), "copied_at": stamp,
        }, indent=1), encoding="utf-8")
        copied = True

    allow_discard = _in_workspace(st.get("document_path"))
    _log("open_copy", "load", copy=str(copy), discard_previous_copy=allow_discard, previous=st.get("document_path"))
    try:
        res = _run_op("open_copy", JS.open_chain(str(copy), allow_discard), timeout=wait_seconds)
    except CDPTimeout as e:
        return _unknown("open_copy", e)
    except (CDPUnavailable, CDPError) as e:
        return {"status": "error", "detail": str(e)}
    if res.get("error"):
        err = res["error"]
        _log("open_copy", "error", error=err)
        if "DIRTY_NOT_OURS" in err:
            return {"status": "error", "detail": "the document open in Dimension has unsaved changes and is not a "
                    "workspace copy. Save or close it in Dimension yourself, then try again.", "open_document": st.get("document_path")}
        return {"status": "error", "detail": err, "copy": str(copy)}
    opened = (res.get("result") or {}).get("document_path")
    ok = bool(opened) and Path(opened).resolve() == copy.resolve()
    if ok:
        _take_lock(str(copy))
    _log("open_copy", "done" if ok else "unknown", opened=opened)
    return {"status": "ok" if ok else "unknown", "copy": str(copy), "copied": copied, "source": str(src),
            "open_document": opened, "seconds": round((time.time() * 1000 - res.get("t0", time.time() * 1000)) / 1000, 1)}


@mcp.tool()
def dimension_inspect(max_depth: int = 4, max_nodes: int = 400) -> dict:
    """Describe the open scene: object tree (groups, meshes, materials, label graphic layers, visibility),
    cameras and their render sizes, canvas size, and current render settings."""
    try:
        res = cdp.eval_json(JS.inspect_js(max_depth, max_nodes), timeout=30)
    except CDPUnavailable:
        return _not_connected()
    except CDPTimeout as e:
        return _unknown("inspect", e)
    if isinstance(res, dict):
        res["document_is_workspace_copy"] = _in_workspace(res.get("document_path"))
    return res


@mcp.tool()
def dimension_render_start(camera: str | None = None, width: int | None = None, height: int | None = None,
                           quality: str | None = None, file_name: str | None = None) -> dict:
    """Start a PNG render of the open workspace copy into ADOBE_MCP_WORKSPACE/renders.
    camera: a camera name from dimension_inspect, or "viewport" (default: the selected or first saved camera).
    width/height: override the camera size in pixels (give both); omit to use the camera's size.
    quality: "low", "medium" or "high" (default: leave the scene's preset).
    file_name: base name without extension (default: scene name + timestamp). Never overwrites a file.
    Returns immediately; poll dimension_render_inspect."""
    if (width is None) != (height is None):
        return {"status": "error", "detail": "give both width and height, or neither"}
    if width is not None and not (16 <= width <= 16000 and 16 <= height <= 16000):
        return {"status": "error", "detail": "width/height must be between 16 and 16000"}
    q = None
    if quality:
        q = QUALITY.get(quality.lower())
        if not q:
            return {"status": "error", "detail": "quality must be low, medium or high"}
    if (refusal := _busy_elsewhere("render_start")):
        return refusal
    try:
        st = _state()
    except CDPUnavailable:
        return _not_connected()
    except CDPTimeout as e:
        return _unknown("render_start", e)
    if st is None or not st.get("has_document"):
        return {"status": "error", "detail": "no document open. Use dimension_open_copy first."}
    if not _in_workspace(st.get("document_path")):
        return {"status": "error", "detail": "the open document is not a workspace copy. Open one with dimension_open_copy."}
    if st["is_rendering"]:
        return {"status": "error", "detail": "a render is already running"}
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe_name(file_name) if file_name else f"{_safe_name(Path(st['document_path']).stem.split('__')[0])}-{stamp}"
    args = {"camera": camera, "width": width, "height": height, "quality": q, "name": name, "location": str(RENDERS)}
    _log("render_start", "start", **args)
    try:
        res = _run_op("render_start", JS.render_chain(str(RENDERS), name, camera, width, height, q), timeout=60)
    except CDPTimeout as e:
        return _unknown("render_start", e)
    except (CDPUnavailable, CDPError) as e:
        return {"status": "error", "detail": str(e)}
    if res.get("error"):
        _log("render_start", "error", error=res["error"])
        return {"status": "error", "detail": res["error"]}
    info = res.get("result") or {}
    _log("render_start", "started", **info)
    return {"status": "started", **info, "next": "poll dimension_render_inspect every 10-30 s"}


@mcp.tool()
def dimension_render_inspect(cancel: bool = False) -> dict:
    """Report progress of the current or last render, and the output file once complete
    (path, bytes, pixel size). cancel=true stops a running render."""
    try:
        if cancel:
            _log("render_cancel", "start")
            cres = cdp.eval_json(JS.CANCEL_RENDER, timeout=15)
            _log("render_cancel", "done", result=cres)
        res = cdp.eval_json(JS.RENDER_STATE, timeout=15)
    except CDPUnavailable:
        return _not_connected()
    except CDPTimeout as e:
        return _unknown("render_inspect", e)
    if not isinstance(res, dict) or not res.get("app_ready"):
        return {"status": "error", "detail": "Dimension UI not ready", "raw": res}
    jobs = res.get("jobs") or []
    last = res.get("last_request") or {}
    job = jobs[-1] if jobs else None
    if res["is_rendering"]:
        status = "rendering"
    elif job and job.get("status") == "complete":
        status = "complete"
    elif job:
        status = job.get("status") or "unknown"
    else:
        status = "idle"
    out = {"status": status, "progress": res.get("progress"), "seconds_elapsed": res.get("seconds_elapsed"),
           "seconds_remaining": res.get("seconds_remaining"), "camera": last.get("camera"), "preset": last.get("preset"),
           "requested_size": last.get("size"), "job": job}
    path = (job or {}).get("output_path") or last.get("expected_path")
    if path:
        p = Path(path)
        out["output_path"] = str(p)
        out["exists"] = p.exists()
        if p.exists():
            out["bytes"] = p.stat().st_size
            out["pixel_size"] = _png_size(p)
            if status == "complete" and out["bytes"] < 20000:
                out["warning"] = "file is very small; it may be blank. Look at it before using it."
    if cancel:
        out["cancel_requested"] = True
    return out


@mcp.tool()
def dimension_close_copy(delete_copy: bool = False) -> dict:
    """Close the open workspace copy without saving (Dimension shows a new empty document).
    Refuses if the open document is not a workspace copy. delete_copy=true also deletes the copied .dn."""
    if (refusal := _busy_elsewhere("close_copy")):
        return refusal
    try:
        st = _state()
    except CDPUnavailable:
        return _not_connected()
    except CDPTimeout as e:
        return _unknown("close_copy", e)
    if st is None:
        return {"status": "error", "detail": "Dimension UI not ready"}
    path = st.get("document_path")
    if not _in_workspace(path):
        return {"status": "error", "detail": "the open document is not a workspace copy; close it in Dimension yourself.",
                "open_document": path}
    if st["is_rendering"]:
        return {"status": "error", "detail": "a render is running; cancel it with dimension_render_inspect(cancel=true) first"}
    _log("close_copy", "start", copy=path, delete_copy=delete_copy)
    try:
        res = _run_op("close_copy", JS.close_chain(True), timeout=120)
    except CDPTimeout as e:
        return _unknown("close_copy", e)
    except (CDPUnavailable, CDPError) as e:
        return {"status": "error", "detail": str(e)}
    if res.get("error"):
        _log("close_copy", "error", error=res["error"])
        return {"status": "error", "detail": res["error"]}
    deleted = False
    if delete_copy and path:
        try:
            Path(path).unlink()
            sidecar = Path(path).with_suffix(".source.json")
            if sidecar.exists():
                sidecar.unlink()
            deleted = True
        except OSError as e:
            _log("close_copy", "delete_failed", error=str(e))
    _release_lock()
    _log("close_copy", "done", deleted=deleted)
    return {"status": "ok", "closed": path, "deleted_copy": deleted, "now_open": (res.get("result") or {}).get("document_path")}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
