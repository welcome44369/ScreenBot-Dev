# ScreenBot

ScreenBot is a lightweight Windows automation shell for recording and playing back mouse and keyboard actions against a locked target window.

## 安裝

1. 建議使用本專案內的虛擬環境：
   - `F:\ScreenBot\.venv\Scripts\python.exe -m pip install -r requirements.txt`

2. 若尚未安裝，請安裝 `PySide6`, `keyboard`, `mouse`, `pywin32`, `psutil`, `pyinstaller`。

## 執行方式

- 開發環境：雙擊 `launcher.bat` 開啟應用程式，該批次檔會使用 `pythonw` 隱藏命令列視窗。
- 封裝後：使用 `dist\ScreenBot.exe`。

## 功能

- 小型浮動控制氣泡，永遠置頂、可拖曳、記住位置。
- 全域熱鍵：
  - `F8`：在待機時開始 5 秒倒數並鎖定前景視窗，運行腳本；運行中再次按下停止腳本。
  - `F9`：運行中暫停、暫停中繼續。
  - `Esc`：顯示確認視窗後安全退出，可在 `config/settings.json` 重新設定為 `ctrl+esc`。
- 腳本管理：`scripts` 資料夾內的 JSON 腳本，可選擇並載入。
- 錄製：在已鎖定目標視窗狀態下錄製滑鼠點擊、滾輪、鍵盤按鍵；儲存為相對目標視窗比例座標。
- 播放：根據錄製的相對座標重新轉換為當前目標視窗座標，支援暫停、繼續與停止。

## 錄製流程

1. 使用 `F8` 進入 5 秒倒數並鎖定前景視窗。
2. 於控制面板按「開始錄製」。
3. 對目標視窗執行滑鼠與鍵盤操作。
4. 按「停止錄製」，輸入腳本名稱並儲存到 `scripts` 資料夾。

## 播放流程

1. 使用 `F8` 鎖定目標視窗並啟動選中的腳本。
2. 可按 `F9` 暫停或繼續。
3. 再按 `F8` 可停止腳本並回到待機。

## 封裝

- 執行 `build.bat` 建立 `dist\ScreenBot.exe`。

## 已知限制

- 目前版本僅支援時間序列腳本播放，畫面條件觸發尚未實作。
- 不進行 OCR、物件辨識或反作弊繞過。
- 需在有權限的 Windows 環境下執行，某些遊戲可能會攔截全域鍵盤事件。
- 錄製可能抓到敏感鍵盤輸入；腳本本身可能包含敏感內容，請妥善保存。

## 專案架構

- `main.py`：啟動應用程式。
- `app/`：核心應用程式模組。
- `scripts/`：腳本儲存目錄。
- `logs/`：日誌資料夾。
- `config/`：設定檔。

