# 自動化螢幕錄製程式

這是一款基於純 Python 生態系打造的自動化螢幕與音訊錄製工具。
支援開機自動啟動，並將錄影結果依照「年份/月份/日期」的目錄結構自動歸檔保存。
關機或關閉程式時，會觸發優雅關閉（Graceful Shutdown）並正確保存影片檔案。

## 功能特色
* **開機自動啟動**：透過寫入 Windows 登錄檔，登入後自動執行背景錄影。
* **自動歸檔儲存**：依照 `[程式目錄]/Recordings/年份/月份/日期/YYYYMMDD_HHMMSS.mp4` 格式儲存。
* **同步音訊錄製**：使用 `soundcard` 套件擷取麥克風與系統播放音訊，混合後與畫面合併。
* **自訂設定**：可透過 `config.json` 更改 FPS、解析度、麥克風及系統音量。
* **優雅退出**：捕捉中斷訊號 (`Ctrl+C`, 系統關機等)，確保影像檔案無損寫入硬碟。

## 環境需求與安裝
1. 安裝 Python 3.9+
2. 執行以下指令安裝依賴套件：
   ```bash
   pip install -r requirements.txt
   ```

## 設定檔說明 (`config.json`)
預設會在第一次執行時讀取或自動產生：
```json
{
    "fps": 30,
    "resolution": {
        "width": 1920,
        "height": 1080
    },
    "mic_volume": 1.0,
    "sys_volume": 1.0,
    "start_on_boot": true
}
```
* **fps**: 錄影幀率
* **resolution**: 輸出影片寬高
* **mic_volume**: 麥克風音量倍率
* **sys_volume**: 系統音量倍率
* **start_on_boot**: 是否在每次啟動時寫入開機自啟動登錄檔

## 執行方式
```bash
python recorder.py
```
若要關閉錄影並儲存，在命令列按下 `Ctrl+C` 即可。

## 注意事項
- 程式依賴 `av` 及 `soundcard`。其中 `soundcard` 在 Windows 上預設可抓取 WASAPI Loopback 介面（即系統音效）。
- 若因權限或裝置變更而導致擷取失敗，請確認預設的輸入與輸出裝置是否正確設定。