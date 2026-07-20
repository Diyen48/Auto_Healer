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
SENTINEL_WEBHOOK = os.getenv("SENTINEL_WEBHOOK", "http://127.0.0.1:8000/webhook/crash")
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
    Converts absolute paths to repository-relative paths if possible.
    """
    from pathlib import Path
    # Matches lines like: File "buggy_app.py", line 32, in process_data
    matches = re.findall(r'File "([^"]+)", line \d+', traceback_text)
    if not matches:
        return None
        
    raw_path = matches[-1]
    try:
        abs_path = Path(raw_path).resolve()
        cwd_path = Path.cwd().resolve()
        if abs_path.is_relative_to(cwd_path):
            return str(abs_path.relative_to(cwd_path).as_posix())
    except Exception:
        pass
        
    # Fallback to filename
    return str(Path(raw_path).name)


def send_crash_alert(error, traceback, file_path):
    """Post structured alert payload to Sentinel webhook endpoint."""
    payload = {
        "error": error,
        "file": file_path,
        "traceback": traceback,
        "service": SERVICE_NAME,
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


if __name__ == "__main__":
    try:
        monitor_logs()
    except KeyboardInterrupt:
        print("\n[STOP] Monitoring stopped.")
