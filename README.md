# 🎥 AutoScreenRecorder (自動化螢幕錄製程式)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

這是一款專為 Windows 設計的高效能、自動化螢幕與音訊錄製工具。基於 Python 生態系打造，結合了高效的擷取技術與智慧型自動歸檔功能。

---

## ✨ 功能特色

*   **🚀 高效能擷取**：使用 `dxcam` (Desktop Duplication API) 實現極低 CPU 佔用、高幀率的畫面擷取。
*   **🎙️ 全方位音訊錄製**：
    *   同步擷取**系統播放音效** (WASAPI Loopback)。
    *   同步擷取**麥克風輸入**。
    *   支援自訂音量比例混合。
*   **⏸️ 智慧自動暫停**：
    *   偵測鍵盤滑鼠閒置（Idle）。
    *   偵測音訊靜音（Silence）。
    *   當系統無活動時自動暫停錄影，節省硬碟空間。
*   **📂 自動化歸檔**：
    *   依照 `Recordings/YYYY/MM/DD/YYYYMMDD_HHMMSS.mp4` 結構自動分類。
    *   支援**優雅關閉 (Graceful Shutdown)**，確保程式意外中斷時影片仍能正確保存。
*   **🧹 自動清理舊錄影**：可設定只保留最近 N 天的錄影，超過期限的檔案會自動刪除，並清掉變空的日期資料夾。
*   **🖱️ 完整滑鼠擷取**：自動將系統滑鼠游標精確渲染至影片畫面中。
*   **🔄 安全錄製機制**：
    *   預設先錄製為 `.mkv` 格式（防止當機導致檔案毀損）。
    *   程式結束時自動轉檔 (Remux) 為 `.mp4`（支援 ffmpeg 或 PyAV 回退機制）。
*   **⚙️ 開機自啟動**：可透過設定自動寫入 Windows 登錄檔，實現登入即開始錄影。

---

## 🛠️ 環境要求

1.  **作業系統**：Windows 10/11 (需支援 Desktop Duplication API)
2.  **Python 版本**：Python 3.9+
3.  **依賴套件**：
    ```bash
    pip install -r requirements.txt
    ```

---

## 🚀 快速上手

1.  **安裝依賴**：
    ```bash
    pip install -r requirements.txt
    ```
2.  **執行程式**：
    ```bash
    python recorder.py
    ```
3.  **停止錄影**：在命令列視窗按下 `Ctrl + C`，或直接關閉視窗，程式會自動完成檔案轉檔與保存。

---

## ⚙️ 設定檔說明 (`config.json`)

程式在第一次執行時會產生預設的 `config.json`：

```json
{
    "fps": 30,
    "resolution": {
        "width": 1920,
        "height": 1080
    },
    "mic_volume": 1.0,
    "sys_volume": 1.0,
    "start_on_boot": true,
    "auto_pause": true,
    "idle_threshold": 5.0,
    "silence_threshold": 0.01,
    "retention_days": 0
}
```

*   **fps**: 錄影幀率。
*   **resolution**: 影片解析度。
*   **mic_volume / sys_volume**: 麥克風與系統音量倍率 (0.0 ~ 2.0+)。
*   **start_on_boot**: 是否隨 Windows 啟動（建議開啟）。
*   **auto_pause**: 是否開啟自動暫停功能。
*   **idle_threshold**: 判斷閒置的秒數門檻。
*   **silence_threshold**: 判斷靜音的音量門檻。
*   **retention_days**: 錄影保留天數。設為 `0` 表示永久保留（預設）；設為 `30` 表示只保留最近 30 天的錄影。

---

## 🧹 錄影保留天數 (`retention_days`)

想要自動清掉舊錄影，只要把 `config.json` 的 `retention_days` 改成大於 `0` 的數字即可：

```json
{
    "retention_days": 30
}
```

*   程式每次啟動時會檢查一次，之後**每 6 小時**再檢查一次，因此長時間連續錄影也會定期清理。
*   檔案的錄影日期取自檔名 `YYYYMMDD_HHMMSS`；若檔名不符合格式，則改用檔案的修改時間判斷。
*   只有 `Recordings/` 資料夾內的 `.mp4` / `.mkv` 會被刪除，正在錄製中的檔案一定會被保留，其他檔案與資料夾不受影響。
*   刪除後若 `Recordings/YYYY/MM/DD/` 變成空資料夾，會一併移除。
*   每次清理結果都會寫入 `recorder.log`，例如 `Retention cleanup: removed 3 recording(s) older than 30 day(s), freed 1234.5 MB.`。
*   設為 `0` 或負數即停用此功能（不會刪除任何檔案）。

---

## 📁 目錄結構

```text
screen-recorder/
├── recorder.py         # 主程式邏輯
├── config.json         # 使用者設定
├── requirements.txt    # 依賴套件清單
├── run.bat             # 快速啟動批次檔
├── LICENSE             # MIT 授權協議
└── Recordings/         # 錄影檔案存放區 (自動產生)
    └── 2026/
        └── 03/
            └── 25/
                └── 20260325_100000.mp4
```
