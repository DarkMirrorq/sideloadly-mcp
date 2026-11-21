---
name: sideloadly-ios-sideloading
description: >-
  Sign, install, update, and auto-refresh iOS/iPadOS .ipa apps on Windows using
  the Sideloadly MCP server. Use whenever the user wants to sideload an IPA,
  refresh expiring 7-day iOS apps, inspect IPA Info.plist metadata, check Apple ID
  App ID quotas, list connected iOS devices, or troubleshoot Sideloadly queue/daemon issues.
---

# Sideloadly iOS Sideloading Skill (Windows)

Use this skill together with the **`sideloadly-mcp`** MCP server to automate iOS/iPadOS IPA installation, re-signing, and lifecycle management on Windows without manual GUI clicking.

## Prerequisites (One-Time Setup on Any Windows PC)

Before running installation tools on a new machine, verify the environment:
1. **Sideloadly** is installed on Windows (with non-Store Apple iTunes & iCloud).
2. The user has signed into their Apple ID inside Sideloadly **once** (creating `sessions.json` / `cert-*.pem` and downloading local Anisette DLLs).
3. The target iPhone/iPad is paired via USB ("Trust This Computer").
4. `sideloadly-mcp` is configured in the agent's MCP config (`uvx --from git+https://github.com/DarkMirrorq/sideloadly-mcp sideloadly-mcp`).

## Standard Workflows

### 1. Environment Check (Always Run First on a New Machine)
Call `sideloadly_doctor()`.
- If `healthy` is `false`, follow the returned `recommendations` list and guide the user before attempting installs.
- Confirm `configured_apple_ids` and `registered_devices_count > 0`.

### 2. Installing or Updating an `.ipa` File
1. Call `inspect_ipa(ipa_path)` to verify the `.ipa` is valid and show the user its `bundle_name`, `bundle_id`, `version`, and `minimum_os_version`.
2. (Optional) Call `get_apple_id_quota()` if installing a brand-new Bundle ID on a free Apple ID (free accounts are limited to 10 App IDs per 7 days and 3 active signed apps).
3. Call `install_or_update_ipa(ipa_path=...)`:
   - Leave `device_udid` and `apple_id` omitted to auto-select the primary device and Apple ID.
   - Keep `wait_for_completion=True` (default) to wait until Sideloadly finishes signing and installing, or set `wait_for_completion=False` and poll `get_installation_status(installation_id)`.
4. Report the final `status` (`installed` or `failed`), `final_bundle_id`, and `last_updated`.

### 3. Refreshing Expiring 7-Day Apps
1. Call `list_installations()` to inspect `hours_left`, `is_expired`, `needs_refresh`, and `last_error`.
2. For any app needing refresh, call `trigger_refresh(installation_id=...)` or `trigger_refresh(app_name_or_bundle_id=...)`.

### 4. Troubleshooting Common Errors
- **Stuck Queue / Timeout (`timeout_waiting_for_install`):**
  1. Call `cancel_queue()` to clear stuck pending flags.
  2. Call `control_sideloadly(action="restart_gui")` or `control_sideloadly(action="raise")` to check if Sideloadly is waiting on an Apple 2FA / password prompt.
  3. Retry `trigger_refresh`.
- **`Unexpected state!` or `Device not connected`:**
  - Verify the iOS device is unlocked and connected via USB or on the same Wi-Fi network.
  - Check `get_daemon_logs(tail_lines=50)` for device detection details.
- **Sideloadly Installed in a Custom Folder:**
  - Set the `SIDELOADLY_DIR` environment variable in `mcp_config.json` to the folder containing `sideloadly.exe`.

## Tool Reference
| Tool | Purpose |
| :--- | :--- |
| `sideloadly_doctor` | Health check: paths, Anisette DLLs, Apple ID session, devices, IPC port `28811` |
| `list_devices` | List registered iOS/iPadOS devices |
| `list_installations` | List installed apps, 7-day expiry countdown, cached IPAs, and errors |
| `get_installation_status` | Poll live status of a single installation by ID |
| `get_apple_id_quota` | Check remaining 7-day App ID quota and certificate files |
| `inspect_ipa` | Extract `Info.plist` metadata, PlugIns, and Frameworks from an `.ipa` |
| `install_or_update_ipa` | Cache IPA (MD5), upsert DB, enqueue via IPC, and wait for install |
| `trigger_refresh` | Re-sign & reinstall an existing app immediately |
| `cancel_queue` | Clear stuck/pending enqueued tasks from `installations.db` |
| `get_device_installed_apps` | List user apps installed on the connected iOS device |
| `get_daemon_logs` | Read tail of `sideloadlydaemon.log` |
| `get_sideloadly_options` / `set_sideloadly_option` | Read/write Sideloadly options (`anisette-mode`, `zipstream`, etc.) |
| `control_sideloadly` | `status`, `raise`, `start_gui`, `start_silent`, `start_daemon`, `restart_gui` |
