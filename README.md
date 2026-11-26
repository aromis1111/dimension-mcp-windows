# dimension-mcp-windows

**An unofficial MCP server and Claude skill that drive Adobe Dimension on Windows.**
Open a copy of a `.dn` scene, read what's in it (objects, materials, label graphics, cameras), and render PNGs,
all from Claude or any other MCP client.

> Not made, endorsed or supported by Adobe. "Adobe" and "Adobe Dimension" are trademarks of Adobe Inc.
> This project ships none of Adobe's code. It talks to a copy of Dimension you have installed and licensed.

As far as we know this is the first Windows MCP server for Dimension. The only other one we found,
[cavi-ai/adobe-mcp](https://github.com/cavi-ai/adobe-mcp), is macOS-only. Its design rules (owned copies,
log before acting, report "unknown" instead of retrying, never launch at startup) are reused here.

## How it works

Adobe offers no scripting API, plugin host or command-line renderer for Dimension. But Dimension 4.x is an
Electron app. Started with `--remote-debugging-port`, its window accepts Chrome DevTools Protocol connections.
The server runs JavaScript in that window and calls Dimension's **own** controllers, the same code its buttons use:

| Step | Dimension internals called |
|---|---|
| open | `documentController.attemptLoadDocument(path)` |
| inspect | `sceneGraphController.sceneTreeManager.root`, `renderController.renderItems`, `documentSettingsController` |
| render | `appModeController.changeMode('RENDER')`, then camera, size, preset and folder on `renderController`, then `beforeStart()` |
| close | `eudocument.markSaved()`, then `documentController.attemptNewDocument()` |

There is no screen-clicking and no image recognition.

## Tools

| Tool | What it does |
|---|---|
| `dimension_status` | installed / running / connected, what is open, and whether another session owns Dimension. Never launches. |
| `dimension_launch` | starts Dimension with the control port and waits for its UI |
| `dimension_open_copy(source_path)` | copies a `.dn` into `<workspace>\scenes\` and opens the **copy** |
| `dimension_inspect(max_depth, max_nodes)` | scene tree (groups, meshes, materials, label graphic layers, hidden flags), cameras and sizes, canvas, render settings |
| `dimension_render_start(camera, width, height, quality, file_name)` | PNG render into `<workspace>\renders\`; returns at once |
| `dimension_render_inspect(cancel)` | progress, ETA, and the output file (bytes, pixel size); can cancel |
| `dimension_close_copy(delete_copy)` | closes the workspace copy without saving |

## Install

Needs Windows, Adobe Dimension 4.x, and [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/aromis1111/dimension-mcp-windows.git
```

Add this to `%APPDATA%\Claude\claude_desktop_config.json` under `mcpServers`, using your own paths.
Write the file without a BOM: PowerShell 5's `Set-Content -Encoding UTF8` adds one and Claude Desktop won't load the file.

```json
"adobe-dimension": {
  "command": "uv",
  "args": ["--directory", "C:\\path\\to\\dimension-mcp-windows", "run", "dimension-mcp"],
  "env": { "ADOBE_MCP_WORKSPACE": "C:\\path\\to\\a\\workspace\\folder" }
}
```

| Variable | Required | Default |
|---|---|---|
| `ADOBE_MCP_WORKSPACE` | yes (absolute path) | none |
| `ADOBE_DIMENSION_EXE` | no | `C:\Program Files\Adobe\Adobe Dimension\Adobe Dimension.exe` |
| `ADOBE_DIMENSION_CDP_PORT` | no | `9333` |

Restart Claude Desktop. The workspace gets `scenes\`, `renders\` and `logs\dimension-ops.jsonl`; every native
operation is logged before it runs. Avoid OneDrive-synced folders for the workspace, because scenes can be over 1 GB.

### The skill

`skill/adobe-dimension/SKILL.md` teaches Claude to use Dimension and these tools well: the workflow, render
settings, gotchas, and internals for extending the server. Install it for Claude Code with:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-skill.ps1
```

For claude.ai or the Claude Desktop chat, zip the `skill\adobe-dimension` folder and upload it under
Settings > Capabilities > Skills.

## Safety rules (enforced in code)

- Originals are only read. Everything opened is a copy inside the workspace.
- Unsaved changes are discarded only when the open document is a workspace copy. Otherwise the tool refuses.
- One Claude session owns Dimension at a time (`logs\session.lock`). Other sessions are refused until it closes its copy.
- Renders never overwrite: names are timestamped and Dimension's "file exists" check runs first.
- If Dimension stops answering, the result is `"status": "unknown"`. No blind retry.
- Nothing launches Dimension except `dimension_launch`.

## Limits

- **Undocumented internals.** Tested only on Dimension 4.1.x (Electron app version `v1.0.2-adobe.16`) on one Windows 11 PC.
  A Dimension update can break it. Adobe has a rebuilt Dimension in beta, with general availability planned for 2027, and that will almost certainly need a new server.
- **The control port is open to local programs.** While Dimension runs with the port (bound to 127.0.0.1),
  any program on the machine can drive it. Close Dimension when you are done.
- **Dimension must be started by `dimension_launch`.** If it is already open the normal way, the tools refuse until you close it.
- **Native dialogs block.** Missing-texture warnings, unsaved-changes prompts on your own documents and crashes freeze
  the page, and the tools then return `unknown`. Someone has to click the dialog.
- **Render mode is required.** A render started in Design mode came out blank and at the wrong size, so the server always switches.
- **White is 254, not 255.** A white-background render measured RGB 254 (corners 251-253). Marketplaces that require
  pure white (e.g. Amazon main images) need a clamp in post.
- PNG only. Scene editing (materials, visibility, cameras) is not exposed yet.
- The embedded Chromium is old: injected JS must be ES5-style, and the protocol ignores `awaitPromise`, so long work is polled.

## Legal note

This project uses Dimension's own JavaScript, as the app ships it, through the debugging interface that Electron provides.
It copies no Adobe code and bypasses no licensing. Check that your use fits Adobe's license terms; this project is
offered as-is under the MIT license.

## Contributing

Issues and PRs are welcome, especially reports from other Dimension versions and GPUs, and scene-editing tools.
The skill's lessons log is meant to grow: add what you learn.
