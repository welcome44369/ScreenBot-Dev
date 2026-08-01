# ScreenBot Dev Codex Handoff

## 1. 專案定位

ScreenBot 是 Windows 目標視窗鎖定、自動 OCR、巨集與工作流工具。  
最終產品方向是以舊版 ScreenBot 的成熟 UI/UX，結合 ScreenBot Dev 的安全、效能與執行核心。

## 2. 最高級架構原則

除了 F8、緊急停止、視窗選取與 ScreenBot 自身管理外，ScreenBot 不存在全域功能。所有錄製、偵測、執行與輸入能力都必須屬於一個有效且凍結的 TargetSession。  
全域 Hook 只是一種底層事件取得方式，不代表產品功能具有全域作用範圍。

## 3. Repository 與分支

- Repository: `https://github.com/welcome44369/ScreenBot-Dev.git`
- Worktree: `F:\ScreenBot_dev`
- Integration branch: `agents/ui-core-integration-r0`
- Main development branch: `dev/screenbot-next`
- Legacy UI reference: `F:\ScreenBot`（Read-only）

## 4. 啟動方式

- 正式：`F:\ScreenBot_dev\launcher.bat`
- 診斷：
  - `Set-Location F:\ScreenBot_dev`
  - `python main.py`

## 5. 開發者驗收優先級

Developer interactive evidence > Focused tests > Static analysis > Large autonomous validation

## 6. 已完成階段

- R0a Clean Shutdown: PASS
- R0b Full OCR Trigger UI: PASS
- R0c Six Trigger Conditions: PASS
- R0d-1 Loop Mode UI: PASS
- R0d-1 Runtime Loop Semantics: PARTIAL — additional live validation required
- R0d-1 Stop Condition Runtime: PARTIAL — additional live validation required
- R0d-2 Reversible Workflow UI: PASS — developer manually confirmed workflow can run and UI can be monitored
- R0e-0 Shortcut Audit: CONFIRMED static F8 contract defect
- R0e-0a F8 Toggle Repair: CANCELLED / ROLLED BACK

Persistence baseline: `eb82e73 feat(ui): allow workflow monitoring during
collapsed execution` contains the accepted R0a Clean Shutdown persistence and
R0d-2 reversible workflow monitoring.

## 7. Trigger 契約

- `initial_absent`
- `initial_present`
- `edge_present`
- `edge_absent`
- `state_absent`
- `state_present`

Legacy mapping:

- `appear` → `edge_present`
- `disappear` → `edge_absent`

## 8. Workflow 循環契約

- `manual_stop`
- `max_cycles`
- `stop_trigger`

Legacy mapping:

- `once` → `max_cycles`
- `max_cycles = 1`

## 9. Protected Core

- TargetSession / generation
- Window ownership / stacking
- Overlay ownership
- Z-order / topmost
- F8 target lifecycle
- Start-collapse handoff
- Foreground guarded input
- Capture / OCR performance
- Workflow execution architecture
- Clean shutdown

## 10. 高優先級已知問題

- P0 — Startup UI visibility anomaly after `launcher.bat`
- P0 — F8 Runtime produced no lock response during abnormal startup
- P1 — F8 static contract remains lock/refresh only; unlock absent
- P1 — Macro recorder records the Stop Recording button click
- P1 — Recorder still uses overly global input semantics
- P1 — Runtime logs may end without terminal event
- P2 — Loop and stop-condition semantics require more live validation

注意：P0 的 F8 Runtime 無反應與 P1 的 F8 缺少 unlock branch 是不同問題，必須分開追蹤。

## 11. 下一步建議順序

1. 修正 Startup UI visibility 並證明 Hotkey registration 正常
2. 重新設計並驗收 F8 lock/unlock toggle
3. TargetSession-scoped Macro Recorder
4. Runtime observability / deterministic terminal logs
5. 繼續 UI parity
6. Background Input Foundation B1a

Background Input Foundation B1a 狀態：PAUSED

## 12. Git 安全規則

禁止：

- `git reset`
- `git restore`
- `git clean`
- `git stash`
- `git rebase`
- `git commit --amend`
- `git add .`
- `git add -A`
- `git push --force`

必須逐檔 stage。不得 push 到 `screenbot-readonly` 或 `dev/screenbot-next`。

## 13. Codex 任務啟動模板

1. 確認 Worktree、Branch、HEAD、Status
2. 先唯讀追蹤架構
3. 列出預計修改檔案
4. 只處理一個問題
5. 執行 focused tests
6. 不自行擴大任務
7. 等待 Developer interactive acceptance
8. 驗收後才逐檔 Stage
