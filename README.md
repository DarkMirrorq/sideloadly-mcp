# Sideloadly Windows MCP Server (`sideloadly-mcp`)

Windows üzerinde **Sideloadly** uygulamasını yapay zeka asistanlarına (Antigravity, Claude Desktop, Cursor, Windsurf vb.) bağlayan **Model Context Protocol (MCP)** sunucusudur.

Herhangi bir arayüz tıklama otomasyonuna (GUI clicker) ihtiyaç duymadan, doğrudan Sideloadly'nin kendi iç **SQLite veritabanı (`installations.db`)**, **MD5 IPA önbelleği (`cache/sideloadly`)** ve **yerel HTTP IPC sunucusu (`127.0.0.1:28811`)** ile haberleşir.

---

## ✨ Özellikler & Araçlar (MCP Tools)

1. **`sideloadly_doctor`**: Bilgisayardaki Sideloadly kurulumunu, Anisette DLL'lerini (`an/`), Apple ID oturumunu (`sessions.json`), bağlı cihazları ve `28811` IPC portunu kontrol eder. Yeni bilgisayarlarda eksik adımları raporlar.
2. **`list_devices`**: Sideloadly'ye kayıtlı tüm iPhone, iPad, Apple TV ve M1 Mac cihazları listeler.
3. **`list_installations`**: Kurulu uygulamaları, Bundle ID'lerini, 7 günlük imzalama süresinden kalan saatleri (`hours_left`), önbellekteki `.ipa` dosyalarını ve varsa son hata kodlarını (`last_error`) listeler.
4. **`get_apple_id_quota`**: Ücretsiz Apple geliştirici hesabında kalan App ID hakkını (`Remaining`) ve en yakın sıfırlanma zamanını (`NearestTtl`) gösterir.
5. **`inspect_ipa`**: Bilgisayardaki herhangi bir `.ipa` dosyasının içindeki `Payload/*.app/Info.plist` dosyasını okuyarak Bundle ID, versiyon, minimum iOS sürümü, PlugIns ve Frameworks listesini çıkarır.
6. **`trigger_refresh`**: Kurulu bir uygulamayı (`installation_id` veya uygulama adı / Bundle ID ile) veritabanında kuyruğa sokar ve `http://127.0.0.1:28811/enqueue?id=<id>` üzerinden Sideloadly'ye anında yeniletir (Re-sign & Install).
7. **`install_or_update_ipa`**: Bilgisayardaki yeni bir `.ipa` dosyasını MD5 hash'i ile Sideloadly önbelleğine kopyalar, `stored_files` ve `installations` tablolarına kaydeder ve arka planda otomatik kurulumu başlatır.
8. **`get_device_installed_apps`**: Cihazda kurulu olan tüm kullanıcı uygulamalarını (`sideloadlydaemon.log` üzerinden) filtreleyerek listeler.
9. **`get_daemon_logs`**: Arka plan servisinin (`sideloadlydaemon.log`) son loglarını okur.
10. **`get_sideloadly_options` / `set_sideloadly_option`**: Sideloadly ayarlarını (`anisette-mode`, `zipstream`, `ignore-preflight`, `custom-chunk-size`) okur ve günceller.
11. **`control_sideloadly`**: Sideloadly arayüzünü öne getirme (`raise`), `.ipa` dosyasını arayüzde açma, arka planda sessiz başlatma (`start_silent`) veya yeniden başlatma (`restart_gui`) işlemlerini yapar.

---

## 🚀 Yeni Bir Bilgisayarda Kurulum

### 1. Ön Gereksinimler
- Windows üzerinde **Sideloadly** kurulu olmalıdır.
- iTunes ve iCloud (Microsoft Store olmayan Apple web sürümleri) kurulu olmalıdır.
- Sideloadly arayüzünden **1 kez Apple ID ile giriş yapılmış** ve iOS cihaz kablo ile bağlanıp **"Bu Bilgisayara Güven"** denmiş olmalıdır.

### 2. Paketi Kurma

```powershell
pip install git+https://github.com/DarkMirrorq/sideloadly-mcp.git
```

### 3. MCP Yapılandırması (`mcp_config.json`)

**Seçenek A — `pip` ile kurulum sonrası:**
```json
{
  "mcpServers": {
    "sideloadly": {
      "command": "sideloadly-mcp"
    }
  }
}
```

**Seçenek B — `uvx` ile sıfır kurulumla (önerilen):**
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
