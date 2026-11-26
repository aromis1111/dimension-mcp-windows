---
name: adobe-dimension
description: >
  How to use Adobe Dimension (4.x, Windows) well, and how to drive it through the dimension-mcp-windows MCP server
  (tools dimension_status / launch / open_copy / inspect / render_start / render_inspect / close_copy). Use for any
  request to open, inspect or render a .dn scene, make product shots or packshots in Dimension, or extend the
  server. Also a living log of what works: append each new setting, render time, gotcha or technique.
---

# adobe-dimension

Server: [dimension-mcp-windows](https://github.com/aromis1111/dimension-mcp-windows) (unofficial). It runs locally on the
Windows PC that has Dimension installed, and talks to Dimension on 127.0.0.1. The workspace (`ADOBE_MCP_WORKSPACE`) holds
`scenes\` (owned copies + `.source.json`), `renders\` (PNG) and `logs\dimension-ops.jsonl`.

## Hard rules

- **Never open or edit an original .dn.** Always use `dimension_open_copy(<original>)`, which works on a copy in the workspace.
- **Dimension must be started by `dimension_launch`.** If the user opened it normally, ask them to save and close it. Never kill it.
- **One session at a time.** If `dimension_status` shows `owned_by_other_session`, don't fight it. Tell the user.
- Don't drive Dimension with screen clicks while the server is driving it.
- **`unknown` means look, don't retry.** It usually means a native dialog is blocking Dimension. Ask the user to look at the
  window, then call `dimension_status`.
- Give renders meaningful names (product, camera, size). The server never overwrites.
- **Pure white needs a post step.** Dimension's white renders at 254 (corners 251-253). Clamp near-white to 255 for marketplaces
  that require it, and check the pixels.
- Preview first: render at 1000 px or quality low and **look at it** before a full-size final. A PNG under 20 KB is almost certainly blank.
- When done, `dimension_close_copy` and quit Dimension (the debugging port is open while it runs).

## Standard workflow

1. `dimension_status`. If it is not running, `dimension_launch` (about 20-40 s).
2. `dimension_open_copy(source_path)`. About 10 s for a 30 MB scene; a 300 MB scene took about 50 s.
3. `dimension_inspect`. Tell the user in plain words which products are visible, which variants are hidden, and which cameras exist.
4. `dimension_render_start(camera=..., width=..., height=..., quality=..., file_name=...)`, then poll `dimension_render_inspect`
   every 10-30 s.
5. Look at the PNG. Check white level, framing, label sharpness, and noise in glass.
6. `dimension_close_copy` (add `delete_copy=true` for throwaway copies).

## Render settings that matter (Dimension 4.1.x)

- Two modes: **Design** (editing) and **Render** (final export, Ctrl+Tab). The server switches to Render by itself.
- Ray tracing ("Lantern" internally). **Render with GPU** needs an NVIDIA RTX card.
- **Preset** Low / Medium / High sets samples (High showed 512 samples in the UI on a test scene). Other settings: Max time
  (0 = no limit), Denoise export (can soften fine label text), Displacement, Resolution Full / 1/2 / 1/4 (previews).
- **Render size comes from each camera** (Properties > Camera size). Override camera size forces one W x H. The document canvas
  size is not the render size.
- Formats: PNG (the server's only format so far), PSD, JPG (4.1+). Transparency is unreliable in 4.1, so render on white and cut out after.
- With several cameras ticked, Dimension queues them and appends `-<camera name>` to the file name.

## Reading a scene (inspect output)

- **Cameras** are scene objects (old camera bookmarks are converted on load). `viewport` is the free working view.
- **Lighting is image-based:** an environment light (HDR) plus light layers. Older files show `SHAPE_LIGHT` nodes (Key / Fill / Back)
  under `ENVIRONMENT_LIGHT`. Hidden ones are off. Version 4 dropped the old physical/sun lights.
- **Environment:** Ground plane = shadow catcher (shadow and reflection opacity), background color, environment background opacity/blur.
- **Materials:** Adobe Standard Material. Glass and liquids use the Interior settings: Translucency; IOR about 1.5 for glass, about 1.33 for
  water or oil; Absorption color and distance in scene units. Glass is the slow, noisy part of most renders.
- **Labels** are image material layers on the label mesh (inspect lists them as `graphic_layers`). Decal mode crosses UV seams; Fill uses the model UVs.
- Set scene units before relying on absorption or scattering distances.

## Render-time log (add rows)

| Scene | Size | Preset | GPU | Time |
|---|---|---|---|---|
| 10 ml amber glass bottle with paper label | 1000x1000 | High | yes | 17.5 s |
| same | 2000x2000 | High | yes | 68 s |
| diffuser + 6 bottles kit | 1000x1000 | Low | yes | about 10 s |

## Internals cheat-sheet (for extending the server)

- Control: Chrome DevTools Protocol on `--remote-debugging-port`, page `main.html` (`http://127.0.0.1:9333/json/list`).
- App object: `AppController.instance`, reached through `element._jsx_data.repeat.$_savedScope.app` on a DOM node. It holds about 45
  controllers: `documentController`, `renderController`, `sceneGraphController`, `cameraController`, `materialController`,
  `lightSetController`, `environmentController`, `selectionController`, `historyController` ...
- Native engine: `Core.nativeModule.port.main` (`documentLoad`, `documentSave`, `getRoot`, `EUNode.getProperties/set`,
  `runCommandJSONString`). Prefer the JS controllers, which keep the UI in sync.
- **Old Chromium:** no async/await, no `Object.entries`. The protocol ignores `awaitPromise`, so start work in the page and poll.
- **ASI trap:** never put a fragment that starts with a newline right after `return `. JS inserts a semicolon and returns `undefined`.
- Native dialogs (`Dialog.showMessageBox`) are synchronous and freeze the page. Avoid them up front: call `eudocument.markSaved(path)`
  before loading or closing a workspace copy, set `renderController.isRenderSaved = true` before `beforeStart()`, and check that
  `findExistingFiles()` is empty.
- `appModeController.changeMode(...)` resets the camera checkboxes, so set `checkedRenderItemIds` and call
  `updateCheckedRenderItems()` after the switch.
- Quality: `renderController.lanternRenderSettings.properties.preset.value = 'LOW' | 'MEDIUM' | 'HIGH'`.
- DevTools `Page.captureScreenshot` can't see the 3D viewport, because it is a native plugin surface. Use an OS screen capture.
- Source: `resources\app.asar` > `main.js` (webpack, readable). Search for `XController.prototype.<fn> = function`.

## Lessons log (newest at the bottom)

- A render queue started in Design mode produced a blank white PNG at the viewport size and ignored camera and size. Render in Render mode.
- Switching modes reset the checked camera to the scene's last-used one. Set the camera after the switch.
- White background measured 254, corners 251-253.
- Two Claude sessions shared one Dimension, and one tried to replace the other's open copy. The session lock now prevents that.
- Opening a copy can fail with Dimension's own "unknown exception" while another document sits in Render mode. Check `dimension_status`,
  then retry once from Design mode. (Seen once; not yet reproduced.)
