---
name: sideloadly-ios-sideloading
description: >-
  Build, sign, install, update, and auto-refresh iOS/iPadOS .ipa apps on Windows
  using the Sideloadly MCP server and its Zero-Mac GitHub Actions iOS Cloud Build
  pipeline (Flutter, Swift, React Native). Use whenever the user wants to compile
  an iOS app on Windows without a Mac, sideload an IPA, refresh expiring 7-day iOS
  apps, inspect IPA Info.plist metadata, check Apple ID quotas, or troubleshoot
  Sideloadly queue/daemon issues.
---

# Sideloadly iOS Sideloading & Zero-Mac Cloud Build Skill (Windows)

Use this skill together with the **`sideloadly-mcp`** MCP server to compile iOS apps on Windows (Flutter, Swift, React Native) via private GitHub Actions `macos-latest` runners and automatically sign, install, and refresh `.ipa` files via Sideloadly without manual GUI clicking.

## Prerequisites (One-Time Setup on Any Windows PC)

Before running installation tools on a new machine, verify the environment:
1. **Sideloadly** is installed on Windows (with non-Store Apple iTunes & iCloud).
2. The user has signed into their Apple ID inside Sideloadly **once** (creating `sessions.json` / `cert-*.pem` and downloading local Anisette DLLs).
3. The target iPhone/iPad is paired via USB ("Trust This Computer").
4. `sideloadly-mcp` is configured in the agent's MCP config (`uvx --from git+https://github.com/DarkMirrorq/sideloadly-mcp sideloadly-mcp`).
5. *(Optional — Only for Zero-Mac GitHub Actions iOS Cloud Builds)* Normal `.ipa` sideloading and 7-day auto-refresh do **NOT** require a GitHub account or token. If the user wants to compile **Flutter**, **Swift (Xcode / XcodeGen)**, or **React Native / Expo** source code from Windows without a Mac:
   - **Step A — Generate a GitHub Personal Access Token (PAT):**
     1. Open **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**: `https://github.com/settings/tokens/new`
     2. Click **Generate new token (classic)** and check two scopes:
        - ✅ **`repo`** *(Full control of private repositories — needed to auto-create and push to the user's private build repository)*
        - ✅ **`workflow`** *(Update GitHub Action workflows — needed to push `.github/workflows/sideloadly-ios-build.yml` and download `.ipa` artifacts)*
        *(Or use a Fine-grained token with Read & Write permissions on **Actions**, **Administration**, **Contents**, and **Workflows**).*
   - **Step B — Save Token in `.env`:**
     Place `GITHUB_PAT=ghp_...` inside a `.env` file in the project root (or globally at `%LOCALAPPDATA%\Sideloadly\.env`). `sideloadly-mcp` automatically adds `.env` to `.gitignore` and uses ephemeral in-memory HTTP headers so the token is never committed or written to `.git/config`.

## Standard Workflows

### 1. Environment Check (Always Run First on a New Machine)
Call `sideloadly_doctor()`.
- If `healthy` is `false`, follow the returned `recommendations` list and guide the user before attempting installs.
- Confirm `configured_apple_ids`, `registered_devices_count > 0`, and (only if compiling from source) `cloud_build_optional_addon.github_pat_configured`.

### 2. Zero-Mac iOS Cloud Build & Sideload (Flutter / Swift / React Native on Windows)
When the user wants to compile an iOS project on Windows and install it onto their iPhone:
1. Ensure a `.env` file contains `GITHUB_PAT=ghp_...` (never hardcode tokens in code or git remotes).
2. Call `cloud_build_and_sideload(project_dir=...)` for the complete 1-step pipeline, OR run step-by-step:
   - `prepare_ios_cloud_build(project_dir=...)`: Auto-detects `flutter`, `swift`, or `react_native`, writes `.github/workflows/sideloadly-ios-build.yml`, scaffolds `project.yml` for Windows Swift projects without an `.xcodeproj`, and protects `.env` in `.gitignore`.
   - `trigger_ios_cloud_build(project_dir=..., private=True)`: Creates/updates the user's private GitHub repository and triggers the `macos-latest` unsigned `.ipa` build.
   - `get_ios_cloud_build_status(repo_full_name=..., run_id=...)`: Monitors the build. If `conclusion == "failure"`, read `error_logs` (extracted Xcode/Flutter compiler errors), fix the source code on Windows, and re-trigger!
   - `download_cloud_build_ipa(repo_full_name=..., run_id=..., install_via_sideloadly=True)`: Downloads the compiled `.ipa` artifact and installs it onto the connected iOS device via Sideloadly.

### 3. Installing or Updating an Existing `.ipa` File
1. Call `inspect_ipa(ipa_path)` to verify the `.ipa` is valid and show the user its `bundle_name`, `bundle_id`, `version`, and `minimum_os_version`.
2. (Optional) Call `get_apple_id_quota()` if installing a brand-new Bundle ID on a free Apple ID (free accounts are limited to 10 App IDs per 7 days and 3 active signed apps).
3. Call `install_or_update_ipa(ipa_path=...)`:
   - Leave `device_udid` and `apple_id` omitted to auto-select the primary device and Apple ID.
   - Keep `wait_for_completion=True` (default) to wait until Sideloadly finishes signing and installing, or set `wait_for_completion=False` and poll `get_installation_status(installation_id)`.
4. Report the final `status` (`installed` or `failed`), `final_bundle_id`, and `last_updated`.

### 4. Refreshing Expiring 7-Day Apps
1. Call `list_installations()` to inspect `hours_left`, `is_expired`, `needs_refresh`, and `last_error`.
2. For any app needing refresh, call `trigger_refresh(installation_id=...)` or `trigger_refresh(app_name_or_bundle_id=...)`.

### 5. Troubleshooting Common Errors
- **Stuck Queue / Timeout (`timeout_waiting_for_install`):**
  1. Call `cancel_queue()` to clear stuck pending flags.
  2. Call `control_sideloadly(action="restart_gui")` or `control_sideloadly(action="raise")` to check if Sideloadly is waiting on an Apple 2FA / password prompt.
  3. Retry `trigger_refresh`.
- **`Unexpected state!` or `Device not connected`:**
  - Verify the iOS device is unlocked and connected via USB or on the same Wi-Fi network.
  - Check `get_daemon_logs(tail_lines=50)` for device detection details.
- **Sideloadly Installed in a Custom Folder:**
  - Set the `SIDELOADLY_DIR` environment variable in `mcp_config.json` to the folder containing `sideloadly.exe`.

## Tool Reference (19 Tools)
| Tool | Purpose |
| :--- | :--- |
| `sideloadly_doctor` | Health check: paths, Anisette DLLs, Apple ID session, devices, IPC port `28811`, and `.env` GitHub PAT readiness |
| `prepare_ios_cloud_build` | Detect `flutter`, `swift`, or `react_native`, generate GitHub Actions macOS `.ipa` workflow, protect `.env` in `.gitignore` |
| `trigger_ios_cloud_build` | Authenticate with `.env` `GITHUB_PAT`, push to a private GitHub repo, and trigger `macos-latest` build |
| `get_ios_cloud_build_status` | Poll GitHub Actions build status, artifacts, and auto-extract Xcode/Flutter compiler error logs on failure |
| `download_cloud_build_ipa` | Download compiled `.ipa` artifact from GitHub Actions and install via Sideloadly |
| `cloud_build_and_sideload` | Full 1-call Zero-Mac pipeline: prepare -> push private repo -> build on macOS -> download `.ipa` -> Sideloadly install |
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
