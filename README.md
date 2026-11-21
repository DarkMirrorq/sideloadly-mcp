# Sideloadly Windows MCP Server (`sideloadly-mcp`)

A production-ready **Model Context Protocol (MCP)** server that gives AI agents (Claude Code, Cursor, Windsurf, Copilot, Antigravity, and any MCP-compatible client) full programmatic control over **Sideloadly** on Windows — without fragile GUI clicking or screen scraping.

It communicates directly with Sideloadly's internal **SQLite database (`installations.db`)**, **MD5 IPA cache (`%LOCALAPPDATA%\cache\sideloadly`)**, and **local HTTP IPC server (`127.0.0.1:28811`)**, with automatic path discovery, SQLite lock retries, cold-start IPC retries, and real-time installation completion tracking.

> **Agent Skill Included:** Includes a ready-to-use [`SKILL.md`](./SKILL.md) following the open Agent Skills specification so AI agents know exactly when and how to diagnose, sign, install, and refresh iOS `.ipa` files.

---

## Features & Tools (14 MCP Tools)

| Tool | Description |
| :--- | :--- |
| `sideloadly_doctor` | Full environment health check (Sideloadly paths, Anisette DLLs, Apple ID session, paired devices, IPC port `28811`). |
| `list_devices` | List all registered iPhone/iPad/Apple TV devices and active installation counts. |
| `list_installations` | List all Sideloadly installations with 7-day expiration countdown (`hours_left`), cached IPA status, and errors. |
| `get_installation_status` | Poll the live status of a single installation by ID (ideal when `wait_for_completion=False`). |
| `get_apple_id_quota` | Check remaining 7-day free Apple ID App ID quota (`Remaining`), nearest reset (`NearestTtl`), and certificates. |
| `inspect_ipa` | Parse any `.ipa` file's `Payload/*.app/Info.plist` (Bundle ID, Display Name, Version, Minimum iOS, PlugIns, Frameworks). |
| `install_or_update_ipa` | Hash `.ipa` (MD5), copy to Sideloadly cache, upsert DB records, enqueue via IPC with retries, and wait for completion. |
| `trigger_refresh` | Immediately re-sign and reinstall an existing app by `installation_id`, app name, or Bundle ID. |
| `cancel_queue` | Clear pending or stuck enqueued tasks from `installations.db`. |
| `get_device_installed_apps` | List user-installed apps detected on the connected iOS/iPadOS device with optional search filtering. |
| `get_daemon_logs` | Read the latest lines from `sideloadlydaemon.log`. |
| `get_sideloadly_options` | Read Sideloadly option flags (`anisette-mode`, `zipstream`, `ignore-preflight`, `custom-chunk-size`). |
| `set_sideloadly_option` | Update a Sideloadly option flag safely. |
| `control_sideloadly` | Control Sideloadly processes: `status`, `raise`, `start_gui`, `start_silent`, `start_daemon`, `restart_gui`. |

---

## Quick Start (Any Windows PC)

### 1. Prerequisites
- **Windows 10/11** with **Sideloadly** installed.
- **iTunes & iCloud** (Apple web versions, not Microsoft Store) installed as required by Sideloadly.
- Sign into your Apple ID inside Sideloadly GUI **once**, and connect your iOS/iPadOS device via USB (**Trust This Computer**).

### 2. Configure in Your AI Agent (`mcp_config.json`)

**Option A — Zero-install via `uvx` (Recommended):**
```json
{
  "mcpServers": {
    "sideloadly": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/DarkMirrorq/sideloadly-mcp",
        "sideloadly-mcp"
      ]
    }
  }
}
```

**Option B — Install via `pip`:**
```powershell
pip install git+https://github.com/DarkMirrorq/sideloadly-mcp.git
```
```json
{
  "mcpServers": {
    "sideloadly": {
      "command": "sideloadly-mcp"
    }
  }
}
```

### 3. Custom Sideloadly Install Path (Optional)
`sideloadly-mcp` automatically discovers Sideloadly via `%LOCALAPPDATA%\Sideloadly`, `Program Files`, Windows Registry (`sideloadly:` URI handler), and running processes. If you use a portable/custom folder, set `SIDELOADLY_DIR` in your MCP `env`:

```json
"env": {
  "SIDELOADLY_DIR": "D:\\Apps\\Sideloadly"
}
```

---

## Running Tests

```powershell
python -m unittest discover -s tests -v
```

---

## License

MIT License — see [LICENSE](./LICENSE).
