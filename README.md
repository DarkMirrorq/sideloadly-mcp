# Sideloadly Windows MCP Server (`sideloadly-mcp`)

<p align="center">
  <a href="#english"><b>🇬🇧 English</b></a> &nbsp;|&nbsp; <a href="#turkce"><b>🇹🇷 Türkçe</b></a>
</p>

<p align="center">
  <a href="https://www.agensi.io/r/POYRAZYLDRMPHP">
    <img src="https://img.shields.io/badge/Install_on-Agensi.io-2563EB?style=for-the-badge" alt="Install on Agensi.io" />
  </a>
  <a href="https://agensi.io/skills/sideloadly-windows-mcp-ios-ipa-signing-auto-refresh">
    <img src="https://img.shields.io/badge/Verified_Agent_Skill-Live-16A34A?style=for-the-badge" alt="Verified Agent Skill" />
  </a>
</p>

---

<a id="english"></a>
## 🇬🇧 English

A production-ready **Model Context Protocol (MCP)** server that gives AI agents (Claude Code, Cursor, Windsurf, Copilot, Antigravity, and any MCP-compatible client) full programmatic control over **Sideloadly** on Windows — without fragile GUI clicking or screen scraping.

It communicates directly with Sideloadly's internal **SQLite database (`installations.db`)**, **MD5 IPA cache (`%LOCALAPPDATA%\cache\sideloadly`)**, and **local HTTP IPC server (`127.0.0.1:28811`)**, with automatic path discovery, SQLite lock retries, cold-start IPC retries, and real-time installation completion tracking.

> **One-Click Agent Skill on Agensi.io:** Want the verified ready-to-install skill package for Claude Code, Cursor, Copilot, and 20+ AI agents? [**Get it on Agensi.io →**](https://www.agensi.io/r/POYRAZYLDRMPHP) ([view skill listing](https://agensi.io/skills/sideloadly-windows-mcp-ios-ipa-signing-auto-refresh))

### Features & Tools (19 MCP Tools — Now with Zero-Mac iOS Cloud Build!)

> **New in v0.3.0 — Zero-Mac Windows-to-iPhone Pipeline:** Write **Flutter**, **Swift (Xcode / XcodeGen)**, or **React Native** code on Windows -> `sideloadly-mcp` reads your `GITHUB_PAT` from `.env`, pushes your project to a **private GitHub repository**, compiles an unsigned `.ipa` on GitHub Actions `macos-latest`, downloads the compiled `.ipa` artifact to Windows, and signs & installs it directly onto your iPhone via Sideloadly! If compilation fails, it extracts the Xcode/Flutter compiler error logs so your AI agent can fix the code automatically.

| Tool | Description |
| :--- | :--- |
| `sideloadly_doctor` | Full environment health check (Sideloadly paths, Anisette DLLs, Apple ID session, paired devices, IPC port `28811`, and `.env` GitHub PAT readiness). |
| `prepare_ios_cloud_build` | Auto-detect `flutter`, `swift`, or `react_native`, generate `.github/workflows/sideloadly-ios-build.yml`, scaffold XcodeGen `project.yml` if needed, and protect `.env` in `.gitignore`. |
| `trigger_ios_cloud_build` | Authenticate via `GITHUB_PAT` from `.env`, create/sync a private GitHub repository, and trigger the `macos-latest` unsigned `.ipa` build. |
| `get_ios_cloud_build_status` | Poll GitHub Actions build status, list `.ipa` artifacts, and automatically extract Xcode/Flutter compiler errors on failure. |
| `download_cloud_build_ipa` | Download and extract the compiled `.ipa` artifact from GitHub Actions and optionally sign & install it via Sideloadly. |
| `cloud_build_and_sideload` | **1-Call End-to-End Pipeline:** Prepare workflow -> push private repo -> wait for macOS build -> download `.ipa` -> sign & install onto iPhone via Sideloadly. |
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

#### 4. Optional Add-on: Zero-Mac iOS Cloud Build Setup (How to Get a GitHub PAT Token)
> **Note:** This is **100% optional**. You do **NOT** need a GitHub account or PAT token to sign, install, or auto-refresh existing `.ipa` files. You only need this if you want `sideloadly-mcp` to compile **Flutter**, **Swift**, or **React Native** source code on Windows via GitHub Actions `macos-latest`.

**Step 1 — Generate Your GitHub Personal Access Token (PAT):**
1. Go to GitHub: [**Settings → Developer settings → Personal access tokens → Tokens (classic)**](https://github.com/settings/tokens/new)
2. Click **Generate new token (classic)**.
3. Set a Note (e.g., `Sideloadly MCP iOS Cloud Builder`) and choose an Expiration.
4. Check the following **2 required scopes**:
   - ✅ **`repo`** *(Full control of private repositories — needed to create and push to your private build repo)*
   - ✅ **`workflow`** *(Update GitHub Action workflows — needed to push `.github/workflows/sideloadly-ios-build.yml` and download `.ipa` artifacts)*
5. Click **Generate token** and copy your token (`ghp_...`).
   *(Alternative: If using a **Fine-grained token**, grant **Read and write** permissions for **Actions**, **Administration** (to auto-create private repos), **Contents**, and **Workflows**).*

**Step 2 — Add the Token to `.env` (Never Hardcode in Code):**
Create a `.env` file inside your iOS project folder (or globally in `%LOCALAPPDATA%\Sideloadly\.env`):
```dotenv
GITHUB_PAT=ghp_your_personal_access_token_here
```
`sideloadly-mcp` automatically adds `.env` to `.gitignore` and uses ephemeral in-memory HTTP headers so your token is **never** committed or saved inside `.git/config`.

#### Running Tests
```powershell
python -m unittest discover -s tests -v
```

---

<a id="turkce"></a>
## 🇹🇷 Türkçe

Windows üzerinde **Sideloadly** uygulamasını yapay zeka ajanlarına (Claude Code, Cursor, Windsurf, Copilot, Antigravity ve tüm MCP uyumlu istemcilere) bağlayan, üretime hazır **Model Context Protocol (MCP)** sunucusudur.

Kırılgan arayüz tıklama (GUI clicker) yöntemleri yerine doğrudan Sideloadly'nin kendi iç **SQLite veritabanı (`installations.db`)**, **MD5 IPA önbelleği (`%LOCALAPPDATA%\cache\sideloadly`)** ve **yerel HTTP IPC sunucusu (`127.0.0.1:28811`)** ile haberleşir. Otomatik kurulum dizini keşfi, SQLite kilitlenme koruması, soğuk başlangıç (cold-start) yeniden denemeleri ve gerçek zamanlı kurulum tamamlama takibi içerir.

> **Agensi.io Üzerinden Tek Tıkla Kurulum:** Claude Code, Cursor, Copilot ve 20+ yapay zeka ajanı için doğrulanmış hazır skill paketini doğrudan kurmak için: [**Agensi.io'da İncele →**](https://www.agensi.io/r/POYRAZYLDRMPHP) ([skill sayfası](https://agensi.io/skills/sideloadly-windows-mcp-ios-ipa-signing-auto-refresh))

### Özellikler & Araçlar (19 MCP Aracı — Mac'siz GitHub Actions Bulut Derleme Destekli!)

> **v0.3.0 Yeniliği — Mac'siz Windows'tan iPhone'a Derleme & Kurulum Hattı:** Windows'ta **Flutter**, **Swift (Xcode / XcodeGen)** veya **React Native** kodunuzu yazın -> `sideloadly-mcp`, `.env` dosyanızdaki `GITHUB_PAT` token'ını okur, projeyi **private (gizli) bir GitHub reposuna** yükler, GitHub Actions `macos-latest` üzerinde imzasız `.ipa` olarak derler, derlenen `.ipa` dosyasını Windows'a indirir ve Sideloadly üzerinden ücretsiz Apple ID'niz ile imzalayıp doğrudan iPhone'unuza kurar! Derleme hatası olursa Xcode/Flutter hata satırlarını çekip AI ajanına verir.

| Araç | Açıklama |
| :--- | :--- |
| `sideloadly_doctor` | Tam ortam sağlık kontrolü (Sideloadly dizinleri, Anisette DLL'leri, Apple ID oturumu, eşleşmiş cihazlar, `28811` IPC portu ve `.env` GitHub PAT hazırlık durumu). |
| `prepare_ios_cloud_build` | `flutter`, `swift` veya `react_native` projesini algılar, `.github/workflows/sideloadly-ios-build.yml` üretir, Swift için gerekirse XcodeGen `project.yml` oluşturur ve `.env` dosyasını `.gitignore` korumasına alır. |
| `trigger_ios_cloud_build` | `.env` içindeki `GITHUB_PAT` ile kimlik doğrular, private GitHub reposu oluşturur/günceller ve `macos-latest` üzerinde `.ipa` derlemesini başlatır. |
| `get_ios_cloud_build_status` | GitHub Actions derleme durumunu sorgular, üretilen `.ipa` artifact'lerini listeler ve hata durumunda Xcode/Flutter derleyici loglarını ayıklar. |
| `download_cloud_build_ipa` | Tamamlanan bulut derlemesindeki `.ipa` paketini Windows'a indirir ve isteğe bağlı olarak Sideloadly ile cihaza kurar. |
| `cloud_build_and_sideload` | **Tek Komutla Uçtan Uca Boru Hattı:** Workflow hazırla -> Private repoya gönder -> macOS derlemesini bekle -> `.ipa` indir -> Sideloadly ile imzala ve iPhone'a yükle. |
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

#### 4. Opsiyonel Eklenti: Mac'siz iOS Bulut Derleme Kurulumu (GitHub PAT Token Nasıl Alınır?)
> **Not:** Bu özellik **%100 opsiyoneldir**. Elimizdeki hazır `.ipa` dosyalarını imzalamak, kurmak veya 7 günlük sürelerini yenilemek için GitHub hesabına veya token'a **ihtiyacınız yoktur**. Sadece Windows üzerinde yazdığınız **Flutter**, **Swift** veya **React Native** kaynak kodunu Mac olmadan GitHub Actions (`macos-latest`) üzerinde derleyip iPhone'a kurmak istediğinizde kullanılır.

**Adım 1 — GitHub Personal Access Token (PAT) Oluşturma:**
1. GitHub'da şu adrese gidin: [**Settings → Developer settings → Personal access tokens → Tokens (classic)**](https://github.com/settings/tokens/new)
2. **Generate new token (classic)** butonuna tıklayın.
3. Bir isim verin (ör. `Sideloadly MCP Cloud Build`) ve süresini seçin.
4. Aşağıdaki **2 yetki kutucuğunu (scope)** mutlaka işaretleyin:
   - ✅ **`repo`** *(Private repo oluşturmak ve kaynak kodunu güvenli şekilde yüklemek için)*
   - ✅ **`workflow`** *(GitHub Actions `.github/workflows/sideloadly-ios-build.yml` dosyasını yüklemek, derlemeyi başlatmak ve derlenen `.ipa` dosyasını indirmek için)*
5. En alttaki **Generate token** butonuna basın ve `ghp_...` ile başlayan token'ınızı kopyalayın.
   *(Alternatif: **Fine-grained token** kullanmak isterseniz Repository permissions altından **Actions**, **Administration**, **Contents** ve **Workflows** izinlerini **Read and write** yapmanız yeterlidir).*

**Adım 2 — Token'ı `.env` Dosyasına Ekleme:**
Proje klasörünüzün içine (veya tüm projelerde geçerli olması için `%LOCALAPPDATA%\Sideloadly\.env` dosyasına) şu satırı ekleyin:
```dotenv
GITHUB_PAT=ghp_kopyaladiginiz_token_buraya
```
`sideloadly-mcp`, `.env` dosyasını otomatik olarak `.gitignore` listesine ekler ve token'ı `.git/config` içine kaydetmeden yalnızca anlık RAM üzerinden gönderir.

#### Testleri Çalıştırma
```powershell
python -m unittest discover -s tests -v
```

---

## License / Lisans

MIT License — see [LICENSE](./LICENSE).
