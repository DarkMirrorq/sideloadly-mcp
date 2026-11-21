# Sideloadly Windows MCP Server (`sideloadly-mcp`)

<p align="center">
  <a href="#english"><b>🇬🇧 English</b></a> &nbsp;|&nbsp; <a href="#turkce"><b>🇹🇷 Türkçe</b></a>
</p>

---

<a id="english"></a>
## 🇬🇧 English

A production-ready **Model Context Protocol (MCP)** server that gives AI agents (Claude Code, Cursor, Windsurf, Copilot, Antigravity, and any MCP-compatible client) full programmatic control over **Sideloadly** on Windows — without fragile GUI clicking or screen scraping.

It communicates directly with Sideloadly's internal **SQLite database (`installations.db`)**, **MD5 IPA cache (`%LOCALAPPDATA%\cache\sideloadly`)**, and **local HTTP IPC server (`127.0.0.1:28811`)**, with automatic path discovery, SQLite lock retries, cold-start IPC retries, and real-time installation completion tracking.

> **Agent Skill Included:** Includes a ready-to-use [`SKILL.md`](./SKILL.md) following the open Agent Skills specification so AI agents know exactly when and how to diagnose, sign, install, and refresh iOS `.ipa` files.

### Features & Tools (14 MCP Tools)

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

### Quick Start (Any Windows PC)

#### 1. Prerequisites
- **Windows 10/11** with **Sideloadly** installed.
- **iTunes & iCloud** (Apple web versions, not Microsoft Store) installed as required by Sideloadly.
- Sign into your Apple ID inside Sideloadly GUI **once**, and connect your iOS/iPadOS device via USB (**Trust This Computer**).

#### 2. Configure in Your AI Agent (`mcp_config.json`)

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

#### 3. Custom Sideloadly Install Path (Optional)
`sideloadly-mcp` automatically discovers Sideloadly via `%LOCALAPPDATA%\Sideloadly`, `Program Files`, Windows Registry (`sideloadly:` URI handler), and running processes. If you use a portable/custom folder, set `SIDELOADLY_DIR` in your MCP `env`:

```json
"env": {
  "SIDELOADLY_DIR": "D:\\Apps\\Sideloadly"
}
```

#### Running Tests
```powershell
python -m unittest discover -s tests -v
```

---

<a id="turkce"></a>
## 🇹🇷 Türkçe

Windows üzerinde **Sideloadly** uygulamasını yapay zeka ajanlarına (Claude Code, Cursor, Windsurf, Copilot, Antigravity ve tüm MCP uyumlu istemcilere) bağlayan, üretime hazır **Model Context Protocol (MCP)** sunucusudur.

Kırılgan arayüz tıklama (GUI clicker) yöntemleri yerine doğrudan Sideloadly'nin kendi iç **SQLite veritabanı (`installations.db`)**, **MD5 IPA önbelleği (`%LOCALAPPDATA%\cache\sideloadly`)** ve **yerel HTTP IPC sunucusu (`127.0.0.1:28811`)** ile haberleşir. Otomatik kurulum dizini keşfi, SQLite kilitlenme koruması, soğuk başlangıç (cold-start) yeniden denemeleri ve gerçek zamanlı kurulum tamamlama takibi içerir.

> **Agent Skill Dahil:** Açık Agent Skills standardına uygun [`SKILL.md`](./SKILL.md) dosyası paketle birlikte gelir; böylece yapay zeka ajanları iOS `.ipa` dosyalarını ne zaman ve nasıl teşhis edip imzalayacağını, kuracağını ve yenileyeceğini bilir.

### Özellikler & Araçlar (14 MCP Aracı)

| Araç | Açıklama |
| :--- | :--- |
| `sideloadly_doctor` | Tam ortam sağlık kontrolü (Sideloadly dizinleri, Anisette DLL'leri, Apple ID oturumu, eşleşmiş cihazlar, `28811` IPC portu). |
| `list_devices` | Kayıtlı tüm iPhone/iPad/Apple TV cihazlarını ve aktif uygulama sayılarını listeler. |
| `list_installations` | Kurulu tüm uygulamaları, 7 günlük süre sayacını (`hours_left`), önbellekteki `.ipa` durumunu ve hataları listeler. |
| `get_installation_status` | Tek bir kurulumun canlı durumunu ID ile sorgular (`wait_for_completion=False` kullanımında takip için idealdir). |
| `get_apple_id_quota` | Ücretsiz Apple ID hesabında kalan 7 günlük App ID kotasını (`Remaining`), en yakın sıfırlanma tarihini (`NearestTtl`) ve sertifikaları gösterir. |
| `inspect_ipa` | Herhangi bir `.ipa` dosyasının `Payload/*.app/Info.plist` verilerini (Bundle ID, Ad, Sürüm, Minimum iOS, PlugIns, Frameworks) okur. |
| `install_or_update_ipa` | `.ipa` dosyasının MD5 hash'ini alır, Sideloadly önbelleğine kopyalar, veritabanını günceller, IPC üzerinden kuyruğa sokar ve kurulumun bitmesini bekler. |
| `trigger_refresh` | Mevcut bir uygulamayı `installation_id`, uygulama adı veya Bundle ID ile anında yeniden imzalayıp yükler. |
| `cancel_queue` | `installations.db` içindeki bekleyen veya takılı kalmış kuyruk görevlerini temizler. |
| `get_device_installed_apps` | Bağlı iOS/iPadOS cihazda kurulu kullanıcı uygulamalarını isteğe bağlı arama filtresiyle listeler. |
| `get_daemon_logs` | `sideloadlydaemon.log` dosyasının son satırlarını okur. |
| `get_sideloadly_options` | Sideloadly ayarlarını (`anisette-mode`, `zipstream`, `ignore-preflight`, `custom-chunk-size`) okur. |
| `set_sideloadly_option` | Sideloadly ayar dosyalarını güvenli şekilde günceller. |
| `control_sideloadly` | Sideloadly süreçlerini yönetir: `status`, `raise`, `start_gui`, `start_silent`, `start_daemon`, `restart_gui`. |

### Hızlı Kurulum (Herhangi Bir Windows Bilgisayarda)

#### 1. Ön Gereksinimler
- **Windows 10/11** ve kurulu **Sideloadly**.
- Sideloadly'nin gerektirdiği **iTunes ve iCloud** (Microsoft Store olmayan Apple web sürümleri).
- Sideloadly arayüzünden **1 kez Apple ID ile giriş yapılmış** ve iOS/iPadOS cihaz USB ile bağlanıp **"Bu Bilgisayara Güven"** denmiş olmalıdır.

#### 2. Yapay Zeka Ajanınıza Ekleme (`mcp_config.json`)

**Seçenek A — `uvx` ile sıfır kurulumla (Önerilen):**
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

**Seçenek B — `pip` ile kurulum:**
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

#### 3. Özel Sideloadly Kurulum Dizini (İsteğe Bağlı)
`sideloadly-mcp`, Sideloadly'yi `%LOCALAPPDATA%\Sideloadly`, `Program Files`, Windows Registry (`sideloadly:` URI kaydı) ve çalışan süreçler üzerinden otomatik bulur. Farklı/taşınabilir bir klasör kullanıyorsanız MCP `env` bloğunda `SIDELOADLY_DIR` tanımlayabilirsiniz:

```json
"env": {
  "SIDELOADLY_DIR": "D:\\Apps\\Sideloadly"
}
```

#### Testleri Çalıştırma
```powershell
python -m unittest discover -s tests -v
```

---

## License / Lisans

MIT License — see [LICENSE](./LICENSE).
