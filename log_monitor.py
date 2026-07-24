"""
Log Monitor Agent — runs as a background process alongside your application.
Tails a target log file (like server.log), detects Python traceback blocks,
and forwards them to Sentinel's webhook ingestion endpoint.
"""

import json
import os
import re
import time
import requests

# ── Configuration (Configurable via Environment Variables) ───────────────────
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "server.log")
SENTINEL_WEBHOOK = os.getenv("SENTINEL_WEBHOOK") or os.getenv("SENTINEL_API_URL") or "http://127.0.0.1:8000/webhook/crash"
SERVICE_NAME = os.getenv("SERVICE_NAME", "app-service")

# Regex patterns to detect multiline Python and Node.js/JS tracebacks
TRACEBACK_START = re.compile(r"(Traceback \(most recent call last\):|^\w*(?:Error|Exception|Panic|Throwable):)")
ERROR_LINE_PATTERN = re.compile(r"^(\w*Error|\w*Exception|\w*Panic): (.*)$")
STACK_FRAME_PATTERN = re.compile(r"^\s+(?:at|File)\s+")


def extract_crashing_file(traceback_text):
    """
    Extract the most recent file in Python or Node.js/JS traceback call stack.
    Dynamically converts absolute paths (Windows/Linux) to repository-relative paths.
    """
    from pathlib import Path
    matches = re.findall(r'File "([^"]+)", line \d+', traceback_text)
    if not matches:
        matches = re.findall(r'at (?:[^\n()]+\s+\()?([^()\n:]+):\d+:\d+\)?', traceback_text)
        matches = [m for m in matches if "node_modules" not in m and not m.startswith("node:")]

    if not matches:
        return None

    clean_path = matches[-1].replace("\\", "/")
    cwd = Path.cwd()
    # Dynamic suffix matching: split by forward slashes and filter out drive letters (e.g. C:)
    parts = [pt for pt in clean_path.split("/") if pt and not (len(pt) == 2 and pt[1] == ":")]
    for i in range(len(parts)):
        sub_str = "/".join(parts[i:])
        sub_path = Path(sub_str)
        if (cwd / sub_path).exists():
            return sub_path.as_posix()
        elif sub_path.exists():
            return sub_path.as_posix()

    return Path(clean_path).name


ALERT_COOLDOWN_CACHE = {}


def send_crash_alert(error, traceback, file_path, github_repo=None, project_id=None, github_app_id=None, github_installation_id=None):
    """Post structured alert payload to Sentinel webhook endpoint with 60s deduplication rate-limiting."""
    now = time.time()
    err_slug = error.split(":")[0].strip() if ":" in error else error[:20]
    cache_key = (file_path, err_slug)

    last_sent = ALERT_COOLDOWN_CACHE.get(cache_key, 0)
    if now - last_sent < 60:
        print(f"[COOLDOWN] Suppressing duplicate crash alert for '{file_path}' ({err_slug}) to prevent spam.")
        return

    ALERT_COOLDOWN_CACHE[cache_key] = now

    payload = {
        "error": error,
        "file": file_path,
        "traceback": traceback,
        "service": SERVICE_NAME,
        "github_repo": github_repo,
        "project_id": project_id,
        "github_app_id": github_app_id,
        "github_installation_id": github_installation_id,
    }

    print(f"[CRASH] Crash detected in log: {error}")
    print(f"[SEND] Sending webhook payload to {SENTINEL_WEBHOOK} ...")

    try:
        resp = requests.post(SENTINEL_WEBHOOK, json=payload, timeout=5)
        if resp.status_code == 202:
            data = resp.json()
            print(f"[SUCCESS] Sent successfully. Event ID: {data.get('event_id', 'N/A')}")
        else:
            print(f"[WARN] Webhook returned status code: {resp.status_code}")
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to reach Sentinel: {exc}")


def monitor_docker_socket():
    """
    UNIVERSAL DOCKER SOCKET MONITORING MODE
    Connects to /var/run/docker.sock and streams live container logs across all containers.
    Zero file mounts, zero code edits, zero Dockerfile edits required!
    """
    import docker

    print("=" * 60)
    print("  [MONITOR] SENTINEL DOCKER SOCKET AGENT (UNIVERSAL PLUGIN)")
    print("  Mode:       Live Docker Socket Stream (/var/run/docker.sock)")
    print(f"  Webhook:    {SENTINEL_WEBHOOK}")
    print("=" * 60)

    try:
        client = docker.from_env()
    except Exception as exc:
        print(f"[ERROR] Could not connect to Docker socket: {exc}. Falling back to file tailing.")
        monitor_logs()
        return

    print("[INFO] Connected to Docker daemon. Monitoring logs of all running containers...")
    in_traceback = False
    current_traceback = []

    # Filter out Sentinel's own container logs to avoid self-monitoring feedback loops
    exclude_keywords = ["sentinel-api", "sentinel-worker", "sentinel-log-monitor", "redis"]

    import threading

    def stream_container_logs(container):
        nonlocal in_traceback, current_traceback
        c_name = container.name
        if any(kw in c_name for kw in exclude_keywords):
            return

        # Dynamically read target repo, app_id, installation_id & project_id from Docker container labels
        container_labels = container.labels or {}
        repo_from_label = container_labels.get("com.sentinel.repo")
        project_id_from_label = container_labels.get("com.sentinel.project_id")
        app_id_from_label = container_labels.get("com.sentinel.app_id")
        inst_id_from_label = container_labels.get("com.sentinel.installation_id")

        print(f"[INFO] Tailing live logs for container: '{c_name}' (Target Repo Label: {repo_from_label or 'Default'}) ...")
        try:
            for log_line in container.logs(stream=True, follow=True, tail=0):
                line = log_line.decode("utf-8", errors="replace")
                s_line = line.strip()

                # 0. Check for JSON structured log (Winston, Pino, Bunyan, Morgan, etc.)
                if s_line.startswith("{") and s_line.endswith("}"):
                    try:
                        data = json.loads(s_line)
                        err_obj = data.get("error") or data
                        stack = None
                        msg = None
                        err_name = "Error"
                        if isinstance(err_obj, dict):
                            stack = err_obj.get("stack") or data.get("stack")
                            msg = err_obj.get("message") or data.get("message") or data.get("error")
                            err_name = err_obj.get("name") or "Error"
                        elif isinstance(err_obj, str):
                            stack = data.get("stack") or err_obj
                            msg = data.get("message") or err_obj

                        is_err = data.get("level") in ("ERROR", "FATAL", "CRITICAL", "error", 50, 60) or "failed" in str(data.get("message", "")).lower()

                        if stack or (is_err and msg):
                            traceback_text = str(stack) if stack else f"{err_name}: {msg}"
                            crashing_file = extract_crashing_file(traceback_text) or "app.js"
                            send_crash_alert(
                                error=f"{err_name}: {msg}",
                                traceback=traceback_text,
                                file_path=crashing_file,
                                github_repo=repo_from_label,
                                project_id=project_id_from_label,
                                github_app_id=app_id_from_label,
                                github_installation_id=inst_id_from_label,
                            )
                            continue
                    except Exception:
                        pass

                # Check for start of Python traceback or JS/Generic error header
                if TRACEBACK_START.search(line) or ERROR_LINE_PATTERN.search(line):
                    if not in_traceback:
                        in_traceback = True
                        current_traceback = [line]
                        continue

                if in_traceback:
                    # If line is part of a stack trace or an error message:
                    if STACK_FRAME_PATTERN.search(line) or ERROR_LINE_PATTERN.search(line) or "Traceback" in line:
                        current_traceback.append(line)
                    else:
                        # End of traceback block detected!
                        traceback_text = "".join(current_traceback)

                        error_type = "Error"
                        error_desc = "Application Crash"
                        for tb_line in current_traceback:
                            match = ERROR_LINE_PATTERN.search(tb_line.strip())
                            if match:
                                error_type = match.group(1)
                                error_desc = match.group(2)
                                break

                        crashing_file = extract_crashing_file(traceback_text) or "app.js"

                        send_crash_alert(
                            error=f"{error_type}: {error_desc}",
                            traceback=traceback_text,
                            file_path=crashing_file,
                            github_repo=repo_from_label,
                            project_id=project_id_from_label,
                            github_app_id=app_id_from_label,
                            github_installation_id=inst_id_from_label,
                        )
                        in_traceback = False
                        current_traceback = []
        except Exception:
            pass

    monitored_containers = {}
    try:
        while True:
            for container in client.containers.list():
                c_id = container.id
                if c_id not in monitored_containers or not monitored_containers[c_id].is_alive():
                    t = threading.Thread(target=stream_container_logs, args=(container,), daemon=True)
                    t.start()
                    monitored_containers[c_id] = t
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[STOP] Container monitoring stopped.")


if __name__ == "__main__":
    try:
        use_docker_sock = os.getenv("USE_DOCKER_SOCKET", "false").lower() == "true" or (
            os.path.exists("/var/run/docker.sock") and not os.path.exists(LOG_FILE_PATH)
        )
        if use_docker_sock:
            monitor_docker_socket()
        else:
            monitor_logs()
    except KeyboardInterrupt:
        print("\n[STOP] Monitoring stopped.")
