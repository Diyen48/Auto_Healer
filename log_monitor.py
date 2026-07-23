"""
Log Monitor Agent — runs as a background process alongside your application.
Tails a target log file (like server.log), detects Python traceback blocks,
and forwards them to Sentinel's webhook ingestion endpoint.
"""

import os
import re
import time
import requests

# ── Configuration (Configurable via Environment Variables) ───────────────────
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "server.log")
SENTINEL_WEBHOOK = os.getenv("SENTINEL_WEBHOOK") or os.getenv("SENTINEL_API_URL") or "http://127.0.0.1:8000/webhook/crash"
SERVICE_NAME = os.getenv("SERVICE_NAME", "data-processor-silent")

# Regex patterns to detect multiline Python tracebacks
TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):")
ERROR_LINE_PATTERN = re.compile(r"^(\w+Error|\w+Exception): (.*)$")


def tail_file(filepath):
    """
    Generator that acts like 'tail -f'.
    Waits for the file to be created, goes to the end, and yields new lines as they arrive.
    """
    if not os.path.exists(filepath):
        print(f"[WAIT] Waiting for log file to be created at: {filepath} ...")
        while not os.path.exists(filepath):
            time.sleep(1)

    print(f"[INFO] Opened log file: {filepath}. Starting tailing from current end of file.")
    file = open(filepath, "r", encoding="utf-8")
    
    # Seek to end of file to only process new logs
    file.seek(0, os.SEEK_END)

    while True:
        line = file.readline()
        if not line:
            time.sleep(0.5)  # Rest briefly before checking for new lines
            continue
        yield line


def monitor_logs():
    """Main monitoring loop tailing the file and aggregating multiline tracebacks."""
    print("=" * 60)
    print("  [MONITOR] SENTINEL LOG MONITOR AGENT (PLUGIN MODE)")
    print(f"  Target Log: {LOG_FILE_PATH}")
    print(f"  Webhook:    {SENTINEL_WEBHOOK}")
    print("=" * 60)

    in_traceback = False
    current_traceback = []

    for line in tail_file(LOG_FILE_PATH):
        # 1. Detect start of a traceback block
        if TRACEBACK_START.search(line):
            in_traceback = True
            current_traceback = [line]
            continue

        if in_traceback:
            current_traceback.append(line)

            # 2. Check if this line matches the final exception statement
            match = ERROR_LINE_PATTERN.match(line.strip())
            if match:
                error_type = match.group(1)
                error_desc = match.group(2)
                traceback_text = "".join(current_traceback)

                # Find the crashing file in the traceback block
                crashing_file = extract_crashing_file(traceback_text) or "unknown.py"

                # 3. Fire the webhook to Sentinel
                send_crash_alert(
                    error=f"{error_type}: {error_desc}",
                    traceback=traceback_text,
                    file_path=crashing_file,
                )

                # Reset status
                in_traceback = False
                current_traceback = []


def extract_crashing_file(traceback_text):
    """
    Extract the most recent file in the traceback call stack.
    Dynamically converts absolute paths (Windows/Linux) to repository-relative paths.
    """
    from pathlib import Path
    matches = re.findall(r'File "([^"]+)", line \d+', traceback_text)
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


def send_crash_alert(error, traceback, file_path, github_repo=None, project_id=None, github_app_id=None, github_installation_id=None):
    """Post structured alert payload to Sentinel webhook endpoint."""
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
                if TRACEBACK_START.search(line):
                    in_traceback = True
                    current_traceback = [line]
                    continue

                if in_traceback:
                    current_traceback.append(line)
                    match = ERROR_LINE_PATTERN.match(line.strip())
                    if match:
                        error_type = match.group(1)
                        error_desc = match.group(2)
                        traceback_text = "".join(current_traceback)
                        crashing_file = extract_crashing_file(traceback_text) or "unknown.py"

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
