"""
title: Repair Quotation Suite
description: 維修報價自動化工具套件。包含六支子工具：(1) query_all_mails - 批次抓取 Notes 中所有待處理維修詢價信件；(2) query_mail - 依序號抓取單封信件的 Subject/From/Body；(3) parts_pu_query - 輸入設備型號與維修路徑關鍵字（格式：model~parts_pu），查詢零件料號、名稱與採購成本；(4) process_email_quotation - 核心自動化流程，解析信件 Body 中的設備型號與維修路徑零件，批次完成報價查詢並推送到 n8n 工作流；(5) run_all_quotations - 批次處理所有待處理信件，每封產生報價單並寄出總覽；(6) generate_pdf_mail_n8n - 將 Markdown 摘要報告傳送至 n8n，產生 PDF 並寄送至指定信箱。
author: internal
version: 2.2.0
"""

import aiohttp
import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("RepairQuotationSuite")


class Tools:
    def __init__(self):
        # ── API 連線設定 ──────────────────────────────────────────
        self.base_url = "http://10.1.0.99:8880/api/v1"
        self.service_user = "APP READER"
        self.service_pass = "rdpassword"
        self.n8n_base_url = "http://10.1.0.101:5678"

        # ── Agent / Gateway 鍵值 ──────────────────────────────────
        self.agent_name = "1-OL-gwQryAgentWpayload"
        self.agent_move_folder = "080Movingfolders"  # 移動信件至 SuccessQt 資料夾
        self.gwkey_getunid = "GetUnidBy080Mail"
        self.gwkey_mail = "Query080MailByUNID"
        self.gwkey_model_pu = "NS-GetDocsByModelPU"

        # ── 預設收件人設定（當 Prompt 未指定時使用）─────────────────
        self.default_email = "wendy_wang@dimension.com.tw"

        # ── 內部狀態 ──────────────────────────────────────────────
        self._auth_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    # ═══════════════════════════════════════════════════════════════
    # 內部輔助方法
    # ═══════════════════════════════════════════════════════════════

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """建立或複用 aiohttp ClientSession（停用 SSL 驗證）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False)
            )
        return self._session

    async def _close(self) -> None:
        """釋放 Session 資源，應在使用結束後呼叫。"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_auth_token(self) -> Optional[str]:
        """向 /api/v1/auth 取得 Bearer Token。"""
        logger.debug("正在取得 Auth Token，使用者: %s", self.service_user)
        try:
            session = await self._ensure_session()
            async with session.post(
                f"{self.base_url}/auth",
                json={"username": self.service_user, "password": self.service_pass},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error("認證失敗，HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
                if isinstance(data, str):
                    data = json.loads(data)
                token = data.get("bearer") or data.get("token")
                if token:
                    logger.debug("成功取得 Token: %s...", token[:10])
                return token
        except Exception as exc:
            logger.error("Auth 發生異常: %s", exc)
            return None

    async def _call_agent(self, payload: dict) -> dict:
        """共用的 Agent 呼叫邏輯，含 Token 自動刷新（最多重試一次）。"""
        for attempt in range(2):
            if not self._auth_token:
                self._auth_token = await self._get_auth_token()
            if not self._auth_token:
                return {"error": "auth_failed"}

            session = await self._ensure_session()
            headers = {
                "Authorization": f"Bearer {self._auth_token}",
                "Content-Type": "application/json",
            }
            try:
                async with session.post(
                    f"{self.base_url}/run/agent?dataSource=dctapigw",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        logger.warning("Token 已失效，重新取得...")
                        self._auth_token = None
                        continue
                    text = await resp.text()
                    if not text.strip():
                        return {"error": "empty_response"}
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"error": "invalid_json", "raw": text[:200]}
            except Exception as exc:
                logger.error("Agent 呼叫失敗: %s", exc)
                return {"error": str(exc)}
        return {"error": "auth_retry_failed"}

    def _parse_ret_v_parts(self, ret_v: str) -> List[Dict[str, Any]]:
        """解析零件報價字串 → list of {pn, productcost, pnname}。"""
        if not ret_v or ret_v.lower() in ("done", "", "-na-"):
            return []
        results = []
        for item in ret_v.split("::"):
            if not item.strip():
                continue
            parts = item.split("^")
            results.append(
                {
                    "pn": parts[0].strip() if len(parts) > 0 else "-",
                    "productcost": parts[1].strip() if len(parts) > 1 else "-",
                    "pnname": parts[2].strip() if len(parts) > 2 else "-",
                }
            )
        return results

    def _parse_mail_ret_v(self, ret_v: str) -> Optional[Dict[str, str]]:
        """解析郵件 ret_v 字串 → {subject, sender, body}。若為結束標記則回傳 None。"""
        if ret_v in ("", "done", "-NA-"):
            return None
        parts = ret_v.split("^")
        body = parts[2].strip() if len(parts) > 2 else ""
        if len(body) > 4000:
            body = body[:4000] + "\n...(內容已截斷)"
        return {
            "subject": parts[0].strip() if len(parts) > 0 else "無標題",
            "sender": parts[1].strip() if len(parts) > 1 else "",
            "body": body,
        }

    async def _fetch_all_unids(self) -> List[str]:
        """取得所有待處理郵件的 UNID 清單。"""
        payload = {
            "agentName": self.agent_name,
            "payload": {"GWKEY": self.gwkey_getunid, "SrhValue": "getunid"},
        }
        data = await self._call_agent(payload)
        if "error" in data:
            logger.error("取得 UNID 失敗: %s", data["error"])
            return []
        res = data.get("agentResponse", "")
        unid_str = res.get("RET_V", "") if isinstance(res, dict) else str(res)
        if not unid_str or unid_str in ("done", "-NA-"):
            return []
        return [u.strip() for u in unid_str.split("::") if u.strip()]

    async def _fetch_mail_by_unid(self, unid: str) -> Optional[Dict[str, str]]:
        """以 UNID 抓取單封郵件，回傳解析結果或 None。"""
        payload = {
            "agentName": self.agent_name,
            "payload": {"GWKEY": self.gwkey_mail, "SrhValue": unid},
        }
        data = await self._call_agent(payload)
        if "error" in data:
            return None
        res = data.get("agentResponse", "")
        ret_v = res.get("RET_V", "") if isinstance(res, dict) else str(res)
        return self._parse_mail_ret_v(ret_v)

    async def _move_to_success_folder(
        self, unids: List[str], move_type: str = "Success"
    ) -> str:
        """呼叫 Notes Agent 080Movingfolders，將指定 UNID 清單的信件移至對應資料夾。"""
        if not unids:
            return "no_unids"

        unid_str = "::".join(unids)
        logger.info(
            "移動資料夾，Type=%s，共 %d 封 UNID: %s", move_type, len(unids), unid_str
        )

        payload = {
            "agentName": self.agent_move_folder,
            "payload": {"UNID": unid_str, "Type": move_type},
        }
        print(
            f"🚀 [MoveFolder] 送出 payload: {json.dumps(payload, ensure_ascii=False)}"
        )

        data = await self._call_agent(payload)
        print(f"📥 [MoveFolder] 原始回傳: {json.dumps(data, ensure_ascii=False)}")

        if "error" in data:
            logger.error("移動資料夾失敗: %s", data["error"])
            return f"error: {data['error']}"

        res = data.get("agentResponse", {})
        print(
            f"📦 [MoveFolder] agentResponse: {json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else res}"
        )

        status = res.get("Status", "") if isinstance(res, dict) else str(res)
        detail = res.get("BIRD-DeMsg", "") if isinstance(res, dict) else ""

        if detail:
            logger.warning("移動資料夾部分失敗: %s", detail)
        else:
            logger.info("移動資料夾完成，Status: %s", status)

        return status

    # ═══════════════════════════════════════════════════════════════
    # 公開工具方法
    # ═══════════════════════════════════════════════════════════════

    async def query_all_mails(self) -> str:
        """批次抓取所有待處理維修詢價信件。"""
        logger.info("=== 開始批量抓取郵件 ===")
        unid_list = await self._fetch_all_unids()
        if not unid_list:
            return "⚠️ 查無郵件資料"

        logger.info("取得 %d 個 UNID", len(unid_list))
        result = []
        for index, unid in enumerate(unid_list, start=1):
            mail = await self._fetch_mail_by_unid(unid)
            if not mail:
                logger.warning("UNID %s 無法取得或為結束標記，略過", unid)
                continue
            result.append(
                f"### 📧 第 {index} 封信\n"
                f"**Subject**: {mail['subject']}\n"
                f"**From**: {mail['sender']}\n"
                f"---\n"
                f"#### 📝 Body\n{mail['body']}\n"
            )

        logger.info("抓取完成，共 %d 封", len(result))
        return "\n".join(result) if result else "⚠️ 查無郵件資料"

    async def _query_mail(self, input_value: str) -> str:
        """依 UNID 查詢單封維修詢價信件。"""
        mail = await self._fetch_mail_by_unid(str(input_value).strip())
        if not mail:
            return "⚠️ 查無郵件資料"
        return (
            f"### 📧 Notes 郵件查詢結果\n"
            f"**Subject**: {mail['subject']}\n"
            f"**From**: {mail['sender']}\n"
            f"---\n"
            f"#### 📝 Body\n{mail['body']}"
        )

    async def _parts_pu_query(self, model: str, parts_pu: str) -> Dict[str, Any]:
        """（內部使用）查詢零件料號、名稱與採購成本。"""
        model, parts_pu = model.strip(), parts_pu.strip()
        if not model or not parts_pu:
            return {"error": "missing_input", "data": []}

        srh_str = f"{model}~{parts_pu}"
        logger.debug("查詢零件: %s", srh_str)

        payload = {
            "agentName": self.agent_name,
            "payload": {
                "GWKEY": self.gwkey_model_pu,
                "SrhValue": srh_str,
                "SrhDelimiter": "~",
            },
        }
        data = await self._call_agent(payload)
        if "error" in data:
            return {"model": model, "parts_pu": parts_pu, "data": []}

        agent_res = data.get("agentResponse", {})
        if isinstance(agent_res, str):
            items = self._parse_ret_v_parts(agent_res)
        elif isinstance(agent_res, dict):
            ret_v = agent_res.get("RET_V", "")
            items = (
                self._parse_ret_v_parts(ret_v)
                if isinstance(ret_v, str)
                else [
                    {
                        "pn": i.get("pn", "-"),
                        "productcost": i.get("productcost", "-"),
                        "pnname": i.get("pnname", "-"),
                    }
                    for i in ret_v
                ]
            )
        else:
            items = []

        return {"model": model, "parts_pu": parts_pu, "data": items}

    async def _generate_pdf_mail_n8n(
        self, summary: str, subject: str, email: str
    ) -> str:
        """將 Markdown 報告傳送至 n8n，由 n8n 產生 PDF 並寄送至指定信箱。"""
        url = f"{self.n8n_base_url}/webhook/generate-pdf-email_2026"
        logger.info("傳送 PDF 任務至 n8n，收件人: %s，主旨: %s", email, subject)
        session = await self._ensure_session()
        try:
            async with session.post(
                url,
                json={"summary": summary, "subject": subject, "email": email},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                print(f"📨 [n8n] HTTP status: {resp.status}")
                print(f"📨 [n8n] response body: {text}")
                if resp.status == 200:
                    return "✅ 已成功傳送至 n8n，PDF 報告將於稍後寄出。"
                return f"❌ n8n 回傳錯誤：{resp.status} - {text}"
        except Exception as exc:
            return f"❌ 無法連線至 n8n：{exc}"

    async def process_email_quotation(
        self, body: str, sender: str = "", recipient_email: str = ""
    ) -> dict:
        """
        核心自動化報價流程：解析信件 Body → 查詢零件報價 → 組 Markdown 報價單 → 透過 n8n 產生 PDF 寄出。

        :param body: 郵件的內文內容。
        :param sender: 詢價客戶名稱（選填）。
        :param recipient_email: 報價單 PDF 的收件人信箱。若未填寫，則預設寄給 wendy_wang@dimension.com.tw。
        """
        return await self._process_email_quotation(
            body, sender, unid="", recipient_email=recipient_email
        )

    async def _process_email_quotation(
        self,
        body: str,
        sender: str = "",
        unid: str = "",
        recipient_email: str = "",
    ) -> dict:
        """（內部使用）核心自動化報價流程，含 Notes UNID 移動資料夾與動態收件人處理。"""
        model_match = re.search(r"設備型號:\s*(.*)", body)
        if not model_match:
            logger.warning("無法從郵件中辨識設備型號 (UNID: %s)", unid)
            return {
                "success": False,
                "skipped": False,
                "model": "-",
                "parts": 0,
                "total": 0,
                "unid": unid,
                "msg": "無法辨識設備型號",
            }

        model = model_match.group(1).strip()
        matches = re.findall(r"Maintenance/\s*([^/\s]+)\s*/\s*([^/\s]+)\s*/-", body)
        query_tasks = list(dict.fromkeys(f"{m[0]}/{m[1]}" for m in matches))
        logger.info(
            "型號: %s，待查零件: %d 項 (UNID: %s)", model, len(query_tasks), unid
        )

        # ── 批次查詢零件報價，逐筆驗證 Cost ───────────────────────
        final_report: List[Dict[str, Any]] = []
        invalid_parts: List[str] = []

        for part in query_tasks:
            result = await self._parts_pu_query(model, part)
            items = result.get("data", [])

            if not items:
                logger.warning("零件查無資料: %s (UNID: %s)", part, unid)
                invalid_parts.append(f"{part}（查無資料）")
                final_report.append(
                    {
                        "search_key": part,
                        "pn": "-",
                        "name": "查無報價資料",
                        "cost_val": 0,
                    }
                )
                continue

            for item in items:
                cost_raw = (
                    str(item.get("productcost", ""))
                    .replace(",", "")
                    .replace("$", "")
                    .strip()
                )
                try:
                    cost_val = int(float(cost_raw))
                except (ValueError, TypeError):
                    cost_val = 0

                if cost_val <= 0:
                    logger.warning(
                        "零件 Cost 無效: %s = '%s' (UNID: %s)",
                        part,
                        cost_raw,
                        unid,
                    )
                    invalid_parts.append(f"{part}（Cost: {cost_raw or '空值'}）")

                final_report.append(
                    {
                        "search_key": part,
                        "pn": item.get("pn", "-"),
                        "name": item.get("pnname", "-"),
                        "cost_val": cost_val,
                    }
                )

        # ── 有任何零件 Cost 無效 → 不觸發 n8n ──────────────────────
        if invalid_parts:
            reason = "、".join(invalid_parts)
            logger.warning("存在無效報價，略過 n8n (UNID: %s)：%s", unid, reason)
            move_status = ""
            if unid:
                print(f"📁 [MoveFolder] Cost 無效，移動 UNID: {unid}，Type: Fail")
                move_status = await self._move_to_success_folder(
                    [unid], move_type="Fail"
                )
                print(f"📁 [MoveFolder] 移動結果: '{move_status}'")
                logger.info(
                    "信件移動結果 (UNID: %s, Type: Fail): %s", unid, move_status
                )
            return {
                "success": False,
                "skipped": True,
                "model": model,
                "parts": len(final_report),
                "total": 0,
                "unid": unid,
                "move_status": move_status,
                "msg": f"以下零件 Cost 為空或為 0，已略過 n8n：{reason}",
            }

        # ── 全部 Cost 有效，計算總金額並組 Markdown ─────────────────
        today = date.today().strftime("%Y-%m-%d")
        total = sum(row["cost_val"] for row in final_report)
        rows = [
            f"| {row['search_key']} | {row['pn']} | {row['name']} | ${row['cost_val']:,} |"
            for row in final_report
        ]
        tax = int(total * 0.05)
        total_with_tax = total + tax

        # ✅ 確定最終收件人：如果 Prompt 帶入的參數有值就用它，否則遞補成 default_email
        final_recipient = (
            recipient_email.strip() if recipient_email.strip() else self.default_email
        )

        markdown_summary = f"""# 維修報價單

**Quoted Date：** {today}
**To：** {sender or "客戶"}

---

## 設備資訊

| 項目 | 內容 |
|------|------|
| 設備型號 | {model} |

## 報價明細

| 維修路徑 | 料號 | 零件名稱 | 單價(未稅) |
|----------|------|----------|------------|
{chr(10).join(rows)}

---
**未稅總金額：** ${total:,}
**稅額(5%)：** ${tax:,}
**含稅總金額：** ${total_with_tax:,}

---
*如有任何問題請聯繫 {final_recipient}*
"""

        n8n_result = await self._generate_pdf_mail_n8n(
            summary=markdown_summary,
            subject=f"【報價單】{model} - {today}",
            email=final_recipient,
        )

        n8n_success = "✅" in n8n_result
        print(f"📨 [n8n] 回傳結果: '{n8n_result}'")
        print(f"📨 [n8n] n8n_success={n8n_success} | unid='{unid}'")

        # ── 依 n8n 結果決定 Type，呼叫 Notes Agent 移動信件 ────────
        move_status = ""
        if unid:
            move_type = "Success" if n8n_success else "Fail"
            print(f"📁 [MoveFolder] 準備移動 UNID: {unid}，Type: {move_type}")
            move_status = await self._move_to_success_folder(
                [unid], move_type=move_type
            )
            print(f"📁 [MoveFolder] 移動結果: '{move_status}'")
            logger.info(
                "信件移動結果 (UNID: %s, Type: %s): %s",
                unid,
                move_type,
                move_status,
            )
        else:
            print(f"⚠️ [MoveFolder] unid 為空，無法移動")

        return {
            "success": n8n_success,
            "skipped": False,
            "model": model,
            "parts": len(final_report),
            "total": total_with_tax,
            "unid": unid,
            "msg": n8n_result,
            "move_status": move_status,
        }

    async def run_all_quotations(
        self, quotation_email: str = "", overview_email: str = ""
    ) -> str:
        """
        批次處理所有待處理信件，每封產生報價單 PDF，最後寄出總覽報告。

        :param quotation_email: 所有個別維修報價單的收件人 Email。若未填寫，預設使用系統設定的 self.default_email。
        :param overview_email: 總覽報告的收件人 Email。若未填寫，預設使用系統設定的 self.default_email。
        """
        logger.info("=== 開始批次報價流程 ===")
        unid_list = await self._fetch_all_unids()
        print(f"📋 [run_all] _fetch_all_unids 回傳: {unid_list}")
        if not unid_list:
            return "⚠️ 查無待處理郵件"

        records = []
        for index, unid in enumerate(unid_list, start=1):
            logger.info("處理第 %d 封 (UNID: %s)", index, unid)
            mail = await self._fetch_mail_by_unid(unid)
            if not mail:
                logger.warning("UNID %s 無法取得，略過", unid)
                continue

            # ✅ 將傳入的 quotation_email 往下帶給每一封報價單
            result = await self._process_email_quotation(
                mail["body"],
                mail["sender"],
                unid=unid,
                recipient_email=quotation_email,
            )
            records.append({"index": index, "unid": unid, **mail, **result})

        today = date.today().strftime("%Y-%m-%d")
        total_count = len(records)
        success_count = sum(1 for r in records if r["success"])
        skipped_count = sum(1 for r in records if r.get("skipped"))
        grand_total = sum(r["total"] for r in records if r["success"])

        # ── 狀態標籤 ──────────────────────────────────────────────
        def _status_label(r: dict) -> str:
            if r["success"]:
                return "✅ 已報價"
            if r.get("skipped"):
                return "⚠️ 無報價略過"
            return "❌ 失敗"

        table_rows = "\n".join(
            f"| {r['index']} | {r['subject'][:25]} | {r['model']} | "
            f"{r['parts']} 項 | "
            f"{'$' + format(r['total'], ',') if r['success'] else '-'} | "
            f"{_status_label(r)} | "
            f"`{r['unid']}` |"
            for r in records
        )

        # ── 無報價略過清單（方便人工追蹤）────────────────────────
        skipped_records = [r for r in records if r.get("skipped")]
        skipped_section = ""
        if skipped_records:
            skipped_lines = "\n".join(
                f"- **{r['subject'][:40]}**（型號: {r['model']}）｜UNID: `{r['unid']}`"
                for r in skipped_records
            )
            skipped_section = f"""
---

## ⚠️ 需人工處理（報價為空或金額為 0）

{skipped_lines}
"""

        overview_md = f"""# 維修詢價報告總覽

**報告日期：** {today}
**總處理件數：** {total_count} 封
**成功報價：** {success_count} 封
**略過（無報價）：** {skipped_count} 封
**報價總金額(含稅)：** ${grand_total:,}

---

## 處理結果

| # | 信件主旨 | 設備型號 | 零件數 | 含稅總價 | 狀態 | UNID |
|---|---------|---------|--------|---------|------|------|
{table_rows}
{skipped_section}
---
*此為系統自動產生之內部總覽報告*
"""

        _overview_recipient = (
            overview_email.strip() if overview_email.strip() else self.default_email
        )
        logger.info("總覽報告收件人: %s", _overview_recipient)
        await self._generate_pdf_mail_n8n(
            summary=overview_md,
            subject=f"【總覽】維修詢價報告彙整 - {today}",
            email=_overview_recipient,
        )

        logger.info("批次完成，共處理 %d 封（略過 %d 封）", total_count, skipped_count)
        return (
            f"✅ 批次報價完成，共處理 {total_count} 封"
            f"（成功 {success_count} 封，略過 {skipped_count} 封），總覽報告已寄出。"
        )
