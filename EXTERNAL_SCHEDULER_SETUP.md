# Alpha Watch 外部排程備援設定

目的：當 GitHub 原生 `schedule` 漏跑時，由獨立的 cron-job.org 再觸發一次 `Alpha Watch Data Refresh`。外部排程是備援，不會取代 GitHub 原生排程。

## 1. 建立 GitHub Fine-grained token

1. 打開 GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。
2. 建立 token，Repository access 選 **Only select repositories**，只選 `littlefive95/littlefive95.github.io`。
3. Repository permissions 只需要 **Actions: Read and write**，並設定合理的到期日。
4. 產生 token 後只複製到 cron-job.org 的 Authorization header。**不要貼到 Issue、程式碼、這份文件或公開聊天室。**

## 2. 在 cron-job.org 建立排程

前往 https://console.cron-job.org/ ，建立一個 cron job：

- Title：`Alpha Watch backup refresh`
- URL：`https://api.github.com/repos/littlefive95/littlefive95.github.io/actions/workflows/pages.yml/dispatches`
- Method：`POST`
- Schedule：每小時第 **12、42 分鐘**執行（minute 12 and 42; every hour, every day）。
- Request body（JSON）：

```json
{"ref":"main"}
```

新增以下自訂 Header：

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_FINE_GRAINED_TOKEN
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

將 `YOUR_FINE_GRAINED_TOKEN` 換成剛產生的 token。請勿把 token 放進 URL 或 request body。若介面有「保存回應／Save responses」選項，請關閉，避免儲存不必要的回應內容。

GitHub 接受 workflow dispatch 時通常回傳 HTTP 204，代表觸發請求已接受，**不代表**整個資料建置已完成；真正結果仍以 GitHub Actions 的 run 狀態和資料快照為準。

## 3. 測試

1. 在 cron-job.org 對該 job 按 Test run。
2. 預期 HTTP 狀態為 204。
3. 打開 https://github.com/littlefive95/littlefive95.github.io/actions ，確認新的 `Alpha Watch Data Refresh` run 已出現並成功。
4. 確認 `data.json`、`taiwan_data.json`、`crypto_data.json` 的 `asOf` 時間更新。
5. 若不想再使用外部備援，停用或刪除 cron-job.org 的 job，並撤銷該 Fine-grained token。

## 4. 失敗通知

GitHub 工作流程現在會在建置失敗、資料快照超過 60 分鐘或使用 last-good fallback 時，建立或更新一張指派給 repository owner 的 Issue。下一次完整更新成功後，Issue 會被留言並關閉。

外部排程的失敗通知則可在 cron-job.org 的 notification settings 中啟用。外部排程能在 GitHub 原生 schedule 未觸發時另行呼叫 workflow dispatch，但若 GitHub 接受請求後工作本身失敗，請以 GitHub Issue 與 Actions run 的結果為準。
