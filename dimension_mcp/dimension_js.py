"""JavaScript run inside Adobe Dimension's renderer page over CDP.

Rules for this file (Dimension 2025 embeds an old Chromium):
- no async/await, no Object.entries, no spread; Promises and arrow functions are fine.
- every snippet returns JSON.stringify(...).
- long work runs as an "op": started here, state kept on window.__mcpOp, polled by Python.

The app object is AppController.instance. It is not a global; it is reachable from
the saved scope of a DOM view (found by probing on 2026-11-25).
"""
import json

FIND_APP = r"""
var app = (function () {
  var c = window.__mcpApp;
  if (c && c.renderController && c.documentController) return c;
  var els = document.querySelectorAll('*');
  for (var i = 0; i < els.length; i++) {
    try { var s = els[i]._jsx_data.repeat.$_savedScope; if (s && s.app && s.app.renderController) { window.__mcpApp = s.app; return s.app; } } catch (e) {}
  }
  return null;
})();
"""


def wrap(body: str) -> str:
    return "(function () {" + FIND_APP + "if (!app) return JSON.stringify({app_ready: false});\n" + body + "\n})()"


def lit(value) -> str:
    """Python value -> JS literal (JSON is valid JS)."""
    return json.dumps(value)


STATE = wrap(r"""
var dc = app.documentController, rc = app.renderController;
return JSON.stringify({
  app_ready: true,
  document_path: dc.filepath || null,
  has_document: !!dc.eudocument,
  mode: app.appModeController.appMode,
  is_rendering: !!rc.isRenderingQueue,
  progress: rc.progress,
  busy_blocking: !!(app.busyController && app.busyController.isBlocking)
});
""")


def start_op(op_id: str, chain: str) -> str:
    """chain: JS expression building a Promise from `app` and `op`. Its value becomes op.result."""
    return wrap(
        "var op = window.__mcpOp = {id: " + lit(op_id) + ", done: false, error: null, result: null, phase: 'start', t0: Date.now()};\n"
        "try {\n"
        "  Promise.resolve().then(function () { return " + chain + "; })\n"
        "    .then(function (r) { op.result = (r === undefined ? null : r); op.done = true; },\n"
        "          function (e) { op.error = String((e && e.message) || e); op.done = true; });\n"
        "} catch (e) { op.error = String(e && e.message || e); op.done = true; }\n"
        "return JSON.stringify({started: true});"
    )


POLL_OP = wrap("return JSON.stringify(window.__mcpOp || null);")

# Shared helper text: discard changes ONLY when Python has said the open doc is a workspace copy.
DISCARD_IF_DIRTY = r"""
(function (allowDiscard) {
  var dc = app.documentController;
  if (!dc.eudocument) return Promise.resolve(false);
  return Promise.resolve(dc.isDocumentDirty()).then(function (d) {
    op.was_dirty = d;
    if (!d) return false;
    if (!allowDiscard) throw new Error('DIRTY_NOT_OURS');
    op.phase = 'discard';
    return Promise.resolve(dc.eudocument.markSaved(dc.filepath)).then(function () { return dc.isDocumentDirty(); })
      .then(function (d2) { if (d2) throw new Error('STILL_DIRTY_AFTER_DISCARD'); return true; });
  });
})(%s)
""".strip()  # must not start with a newline: it follows `return ` (JS would insert a semicolon)


def open_chain(path: str, allow_discard: bool) -> str:
    return (
        "(function () {\n"
        "  var dc = app.documentController, rc = app.renderController;\n"
        "  if (rc.isRenderingQueue) throw new Error('RENDERING');\n"
        "  if (app.busyController && app.busyController.isBlocking) throw new Error('BUSY');\n"
        "  return " + (DISCARD_IF_DIRTY % lit(allow_discard)) + ".then(function () {\n"
        "    op.phase = 'design_mode';\n"
        "    if (app.appModeController.appMode !== 'DESIGN') return app.appModeController.changeMode('DESIGN');\n"
        "  }).then(function () {\n"
        "    op.phase = 'load';\n"
        "    return dc.attemptLoadDocument(" + lit(path) + ");\n"
        "  }).then(function () { return {document_path: dc.filepath}; });\n"
        "})()"
    )


def close_chain(allow_discard: bool) -> str:
    return (
        "(function () {\n"
        "  var dc = app.documentController, rc = app.renderController;\n"
        "  if (rc.isRenderingQueue) throw new Error('RENDERING');\n"
        "  return " + (DISCARD_IF_DIRTY % lit(allow_discard)) + ".then(function () {\n"
        "    op.phase = 'design_mode';\n"
        "    if (app.appModeController.appMode !== 'DESIGN') return app.appModeController.changeMode('DESIGN');\n"
        "  }).then(function () {\n"
        "    op.phase = 'new_document';\n"
        "    return dc.attemptNewDocument();\n"
        "  }).then(function (ok) { return {new_document: ok !== false, document_path: dc.filepath || null}; });\n"
        "})()"
    )


def inspect_js(max_depth: int, max_nodes: int) -> str:
    return wrap(r"""
var dc = app.documentController, rc = app.renderController;
if (!dc.eudocument) return JSON.stringify({app_ready: true, has_document: false});
var MAXD = %d, MAXN = %d, count = 0, truncated = false;
var walk = function (it, d) {
  if (!it) return null;
  if (count >= MAXN) { truncated = true; return null; }
  count++;
  var n = {name: it.label || null, type: it.type};
  if (it.isVisible === false) n.hidden = true;
  if (it.isLocked) n.locked = true;
  if (it.isCameraType) { n.width = it.renderWidth; n.height = it.renderHeight; }
  var mats = (it.materials || []).map(function (m) { return m && (m.label || m.name); }).filter(Boolean);
  if (mats.length) n.materials = mats;
  var kids = (it.children || []).filter(function (c) { return c.type !== 'MATERIAL' && c.type !== 'MATERIAL_LAYER'; });
  var layers = [];
  (it.children || []).forEach(function (c) { if (c.type === 'MATERIAL') (c.children || []).forEach(function (l) { if (l.type === 'MATERIAL_LAYER') layers.push(l.label); }); });
  if (layers.length) n.graphic_layers = layers;
  if (kids.length) {
    if (d >= MAXD) n.children_not_shown = kids.length;
    else n.children = kids.map(function (c) { return walk(c, d + 1); }).filter(Boolean);
  }
  return n;
};
var root = app.sceneGraphController.sceneTreeManager.root;
var tree = (root.children || []).map(function (c) { return walk(c, 1); }).filter(Boolean);
var cams = rc.renderItems.map(function (r) {
  return r.id === 'VIEWPORT_CAMERA' ? {name: 'viewport'} : {name: r.label, width: r.renderWidth || r.width, height: r.renderHeight || r.height};
});
var active = null; try { active = app.cameraController.activeCamera && (app.cameraController.activeCamera.label || app.cameraController.activeCamera.name); } catch (e) {}
var preset = null; try { preset = rc.lanternRenderSettings.properties.preset.value; } catch (e) {}
return JSON.stringify({
  app_ready: true, has_document: true,
  document_path: dc.filepath, mode: app.appModeController.appMode,
  canvas: app.documentSettingsController.documentSettingsAsObj,
  cameras: cams, active_camera: active,
  render: {engine: rc.renderModeRenderEngine, preset: preset, format: rc.parameters.format, location: rc.parameters.location,
           override_size: rc.shouldOverrideCameraSize ? [rc.overrideWidth, rc.overrideHeight] : null, is_rendering: !!rc.isRenderingQueue},
  scene: tree, nodes_listed: count, truncated: truncated
});
""" % (max_depth, max_nodes))


def render_chain(location: str, name: str, camera: str | None, width: int | None, height: int | None, preset: str | None) -> str:
    return (
        "(function () {\n"
        "  var rc = app.renderController;\n"
        "  if (rc.isRenderingQueue) throw new Error('ALREADY_RENDERING');\n"
        "  var CAM = " + lit(camera) + ", W = " + lit(width) + ", H = " + lit(height) + ", Q = " + lit(preset) + ";\n"
        "  op.phase = 'render_mode';\n"
        "  return Promise.resolve(app.appModeController.changeMode('RENDER'))\n"
        "  .then(function () { return new Promise(function (r) { setTimeout(r, 800); }); })\n"
        "  .then(function () {\n"
        "    if (app.appModeController.appMode !== 'RENDER') throw new Error('NOT_IN_RENDER_MODE');\n"
        "    op.phase = 'configure';\n"
        "    var items = rc.renderItems.slice ? rc.renderItems.slice() : rc.renderItems.toArray();\n"
        "    var cams = items.filter(function (i) { return i.id !== 'VIEWPORT_CAMERA'; });\n"
        "    var item = null;\n"
        "    if (CAM === null) item = (rc.selectedRenderItems && rc.selectedRenderItems[0]) || cams[0] || items[0];\n"
        "    else if (CAM.toLowerCase() === 'viewport') item = items.filter(function (i) { return i.id === 'VIEWPORT_CAMERA'; })[0];\n"
        "    else item = cams.filter(function (i) { return (i.label || '').toLowerCase() === CAM.toLowerCase(); })[0];\n"
        "    if (!item) throw new Error('NO_SUCH_CAMERA: ' + CAM + ' (cameras: ' + cams.map(function (i) { return i.label; }).join(', ') + ', viewport)');\n"
        "    rc.checkedRenderItemIds = [item.id]; rc.userDidSelectItemsToRender = true; rc.updateCheckedRenderItems();\n"
        "    rc.setRenderLocation(" + lit(location) + ");\n"
        "    rc.parameters.exportName = " + lit(name) + ";\n"
        "    rc.setOutputFormat('png');\n"
        "    if (W && H) { rc.shouldOverrideCameraSize = true; rc.overrideWidth = W; rc.overrideHeight = H; } else { rc.shouldOverrideCameraSize = false; }\n"
        "    if (Q) { rc.lanternRenderSettings.properties.preset.value = Q; }\n"
        "    var existing = rc.findExistingFiles();\n"
        "    if (existing.length) throw new Error('FILE_EXISTS: ' + existing.join(', '));\n"
        "    var base = (rc.renderItems.length === 1 || item.id === 'VIEWPORT_CAMERA') ? rc.parameters.exportName : rc.parameters.exportName + '-' + item.label;\n"
        "    var info = {camera: item.id === 'VIEWPORT_CAMERA' ? 'viewport' : item.label,\n"
        "      expected_path: rc.parameters.getModifiedPath(base, 'png'),\n"
        "      size: (W && H) ? [W, H] : [item.renderWidth || null, item.renderHeight || null],\n"
        "      preset: rc.lanternRenderSettings.properties.preset.value, engine: rc.renderModeRenderEngine, started_at: Date.now()};\n"
        "    window.__mcpLastRender = info;\n"
        "    rc.isRenderSaved = true;  // otherwise beforeStart opens a blocking 'render not saved' dialog\n"
        "    op.phase = 'start';\n"
        "    return Promise.resolve(rc.beforeStart()).then(function () { return info; });\n"
        "  });\n"
        "})()"
    )


RENDER_STATE = wrap(r"""
var rc = app.renderController, jobs = [];
try { for (var j = 0; j < rc.renderQueue.items.length; j++) { var it = rc.renderQueue.items.at(j); jobs.push({name: it.name, status: it.status, output_path: it.outputPath || null}); } } catch (e) {}
return JSON.stringify({
  app_ready: true, mode: app.appModeController.appMode,
  is_rendering: !!rc.isRenderingQueue, queue_done: !!rc.isQueueDone,
  progress: rc.progress, seconds_elapsed: rc.secondsElapsed, seconds_remaining: rc.secondsRemaining,
  renderer_status: rc.lastRendererStatus, jobs: jobs, last_request: window.__mcpLastRender || null
});
""")

CANCEL_RENDER = wrap("var rc = app.renderController; if (!rc.isRenderingQueue) return JSON.stringify({cancelled: false, reason: 'not rendering'}); rc.cancel(); return JSON.stringify({cancelled: true});")
