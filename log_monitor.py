"""
Log Monitor Agent — runs as a background process alongside your application.
Tails target logs or Docker container streams via Docker socket, detects tracebacks,
and forwards structured crash alerts to Sentinel's webhook ingestion endpoint.
"""

import json
import os
import re
import threading
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
ALERT_COOLDOWN_LOCK = threading.Lock()


def send_crash_alert(error, traceback, file_path, github_repo=None, project_id=None, github_app_id=None, github_installation_id=None, service_name=None):
    """Post structured alert payload to Sentinel webhook endpoint with 60s deduplication rate-limiting."""
    now = time.time()
    err_slug = error[:80].strip()
    cache_key = (file_path, err_slug)

    with ALERT_COOLDOWN_LOCK:
        last_sent = ALERT_COOLDOWN_CACHE.get(cache_key, 0)
        if now - last_sent < 60:
            print(f"[COOLDOWN] Suppressing duplicate crash alert for '{file_path}' ({err_slug}) to prevent spam.")
            return
        ALERT_COOLDOWN_CACHE[cache_key] = now

    payload = {
        "error": error,
        "file": file_path,
        "traceback": traceback,
        "service": service_name or SERVICE_NAME,
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


def resolve_container_repo(container):
    """
    Generalized Target Repository Resolver.
    Checks:
    1. Container labels: com.sentinel.repo or com.github.repo
    2. Container environment variables: SENTINEL_REPO, GITHUB_REPO, or TARGET_REPO
    3. Global plugin container fallback: SENTINEL_DEFAULT_REPO or GITHUB_REPO
    """
    labels = container.labels or {}
    if labels.get("com.sentinel.repo"):
        return labels.get("com.sentinel.repo").strip()
    if labels.get("com.github.repo"):
        return labels.get("com.github.repo").strip()

    try:
        env_list = container.attrs.get("Config", {}).get("Env", [])
        for env_item in env_list:
            if "=" in env_item:
                k, v = env_item.split("=", 1)
                if k in ("SENTINEL_REPO", "GITHUB_REPO", "TARGET_REPO") and v.strip():
                    return v.strip()
    except Exception:
        pass

    return os.getenv("SENTINEL_DEFAULT_REPO") or os.getenv("GITHUB_REPO")


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

    # Filter out Sentinel's own container logs to avoid self-monitoring feedback loops
    exclude_keywords = ["sentinel-api", "sentinel-worker", "sentinel-log-monitor", "redis"]

    def stream_container_logs(container):
        # Local per-container state variables to prevent cross-thread log contamination
        in_traceback = False
        current_traceback = []

        c_name = container.name
        if any(kw in c_name for kw in exclude_keywords):
            return

        container_labels = container.labels or {}
        repo_from_label = resolve_container_repo(container)
        project_id_from_label = container_labels.get("com.sentinel.project_id") or os.getenv("SENTINEL_PROJECT_ID")
        app_id_from_label = container_labels.get("com.sentinel.app_id") or os.getenv("GITHUB_APP_ID")
        inst_id_from_label = container_labels.get("com.sentinel.installation_id") or os.getenv("GITHUB_INSTALLATION_ID")

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
                                service_name=c_name,
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
                            service_name=c_name,
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


def monitor_logs():
    """Tail log file for local non-docker deployments."""
    if not os.path.exists(LOG_FILE_PATH):
        print(f"[WARN] Log file {LOG_FILE_PATH} not found. Retrying ...")
        return

    print(f"[INFO] Monitoring log file: {LOG_FILE_PATH}")
    with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, 2)
        in_tb = False
        tb_lines = []
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue

            if TRACEBACK_START.search(line) or ERROR_LINE_PATTERN.search(line):
                if not in_tb:
                    in_tb = True
                    tb_lines = [line]
                    continue

            if in_tb:
                if STACK_FRAME_PATTERN.search(line) or ERROR_LINE_PATTERN.search(line) or "Traceback" in line:
                    tb_lines.append(line)
                else:
                    tb_text = "".join(tb_lines)
                    err_t, err_d = "Error", "Application Crash"
                    for l in tb_lines:
                        m = ERROR_LINE_PATTERN.search(l.strip())
                        if m:
                            err_t, err_d = m.group(1), m.group(2)
                            break
                    send_crash_alert(
                        error=f"{err_t}: {err_d}",
                        traceback=tb_text,
                        file_path=extract_crashing_file(tb_text) or "server.py",
                        github_repo=os.getenv("SENTINEL_DEFAULT_REPO") or os.getenv("GITHUB_REPO"),
                    )
                    in_tb = False
                    tb_lines = []


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

