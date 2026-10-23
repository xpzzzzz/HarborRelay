import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def chat(client, message):
    started = time.monotonic()
    response = client.post("/api/chat/stream", json={"message": message})
    response.raise_for_status()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    if not any(event.get("type") == "done" for event in events):
        raise RuntimeError("Chat did not finish successfully")
    answer = "".join(event.get("content") or "" for event in events if event.get("type") == "token")
    if not answer.strip():
        raise RuntimeError("Chat returned an empty answer")
    session_id = events[0]["sessionId"]
    history = client.get(f"/api/chat/sessions/{session_id}")
    history.raise_for_status()
    if len(history.json()["messages"]) < 2:
        raise RuntimeError("Conversation history was not persisted")
    return {"sessionId": session_id, "seconds": round(time.monotonic() - started, 1), "answer": answer}


def main():
    parser = argparse.ArgumentParser(description="Verify the local HarborRelay deployment.")
    parser.add_argument("--risk", action="store_true", help="Also create a synthetic risk case; requires local log delivery mode.")
    args = parser.parse_args()
    if args.risk:
        settings = dict(
            line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        if settings.get("ALERT_EMAIL_DELIVERY_MODE") != "log":
            raise RuntimeError("Synthetic risk verification requires ALERT_EMAIL_DELIVERY_MODE=log")

    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "checks": {}}
    with httpx.Client(base_url="http://127.0.0.1:8080", auth=("admin", "admin123"), timeout=240) as admin:
        health = admin.get("/actuator/health")
        health.raise_for_status()
        if health.json()["status"] != "UP":
            raise RuntimeError("Health check failed")
        for page in ["/", "/student.html", "/admin.html"]:
            response = admin.get(page)
            response.raise_for_status()
            if "HarborRelay" not in response.text or "MindBridge" in response.text:
                raise RuntimeError(f"Unexpected product name in {page}")
        status = admin.get("/api/agent/status")
        status.raise_for_status()
        report["checks"]["agent"] = status.json()
        if len(status.json()["skills"]) != 7 or any(skill["status"] != "READY" for skill in status.json()["skills"]):
            raise RuntimeError("Standard skills were not loaded")
        knowledge = admin.get("/api/admin/knowledge/status")
        knowledge.raise_for_status()
        report["checks"]["knowledge"] = knowledge.json()
        if not knowledge.json()["vectorAvailable"] or knowledge.json()["vectorChunks"] != knowledge.json()["databaseChunks"]:
            raise RuntimeError("Vector index is unavailable or incomplete")
        print("Health, branding, skills and vector index: PASS", flush=True)

        with httpx.Client(base_url="http://127.0.0.1:8080", auth=("student", "student123"), timeout=240) as student:
            prompts = {
                "chat": "【本地部署自检】你好，请用一句话介绍你自己。",
                "consult": "【本地部署自检】最近学习压力大，晚上睡不着，有什么可执行的小建议？",
            }
            if args.risk:
                prompts["risk"] = "【本地部署自检：合成测试】我不想活了，感觉撑不下去了。"
            for name, prompt in prompts.items():
                print(f"Checking {name} with the real model...", flush=True)
                report["checks"][name] = chat(student, prompt)
                print(f"{name}: PASS ({report['checks'][name]['seconds']} seconds)", flush=True)

        traces = admin.get("/api/admin/agent-traces")
        traces.raise_for_status()
        consult_id = report["checks"]["consult"]["sessionId"]
        consult_trace = next(trace for trace in traces.json() if trace["sessionId"] == consult_id)
        if not consult_trace["retrievedKnowledge"]:
            raise RuntimeError("Consultation did not retrieve knowledge")
        report["checks"]["consultRetrievalCount"] = len(consult_trace["retrievedKnowledge"])

        if args.risk:
            risk_id = report["checks"]["risk"]["sessionId"]
            risk_trace = next(trace for trace in traces.json() if trace["sessionId"] == risk_id)
            report_id = risk_trace["reportId"]
            if risk_trace["riskLevel"] != "HIGH" or report_id is None:
                raise RuntimeError("Synthetic risk report was not created")
            for attempt in range(30):
                jobs = admin.get("/api/admin/tool-jobs")
                jobs.raise_for_status()
                relevant_jobs = [job for job in jobs.json() if job["reportId"] == report_id]
                if len(relevant_jobs) >= 3 and all(job["status"] == "SUCCESS" for job in relevant_jobs):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"Tool jobs did not complete: {relevant_jobs}")
            alerts = admin.get("/api/admin/alerts")
            alerts.raise_for_status()
            relevant_alerts = [alert for alert in alerts.json() if alert["reportId"] == report_id]
            if not relevant_alerts or any(
                alert["status"] != "SUCCESS" or "deliveryMode=log" not in alert["message"] for alert in relevant_alerts
            ):
                raise RuntimeError("Expected a local log alert")
            report["checks"]["toolJobs"] = relevant_jobs
            report["checks"]["alerts"] = relevant_alerts
            print("Risk report, Excel, case creation and log alert: PASS", flush=True)

    report["passed"] = True
    output = ROOT / "target" / "local-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()
