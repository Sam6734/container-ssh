#!/usr/bin/env python3
import fcntl, json, os, re, select, signal, struct, sys
import termios, threading, time, tty
import urllib.error, urllib.parse, urllib.request

JHUB_URL    = os.environ.get("JHUB_URL", "http://hub:8081")
ADMIN_TOKEN = os.environ.get("JHUB_ADMIN_TOKEN", "")
NAMESPACE   = os.environ.get("NAMESPACE", "default")
USERNAME    = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JHUB_USERNAME", "")

def jhub(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"{JHUB_URL}/hub/api{path}", data=body, method=method,
        headers={"Authorization": f"token {ADMIN_TOKEN}",
                 **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            try: return r.status, json.loads(r.read())
            except: return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {}

def get_user_data():
    enc = urllib.parse.quote(USERNAME, safe="")
    status, data = jhub("GET", f"/users/{enc}")
    if status != 200:
        print(f"\r\n  Error: JHub returned {status} for user lookup.\r\n")
        sys.exit(1)
    return data

def get_profiles():
    enc = urllib.parse.quote(USERNAME, safe="")
    status, tok = jhub("POST", f"/users/{enc}/tokens",
                       {"expires_in": 60, "note": "launcher-profile-fetch"})
    if status not in (200, 201) or not isinstance(tok, dict):
        return []
    user_token = tok.get("token", "")
    token_id   = tok.get("id", "")
    try:
        req = urllib.request.Request(
            f"{JHUB_URL}/hub/spawn/{enc}",
            headers={"Authorization": f"token {user_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  Warning: could not fetch profiles ({e})\r\n")
            return []
        profiles = []
        pattern = re.compile(
            r'<input[^>]+name=["\']profile["\'][^>]+value=["\']([^"\']+)["\']'
            r'|<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']profile["\']',
            re.IGNORECASE,
        )
        for m in pattern.finditer(html):
            slug = m.group(1) or m.group(2)
            if not slug: continue
            label_m = re.search(r"<label[^>]*>(.*?)</label>", html[m.end():m.end()+600],
                                re.DOTALL | re.IGNORECASE)
            if label_m:
                text = re.sub(r"<[^>]+>", " ", label_m.group(1))
                display = re.sub(r"\s+", " ", text).strip().split("\n")[0].strip() or slug
            else:
                display = slug
            profiles.append({"slug": slug, "display": display})
        return profiles
    finally:
        if token_id:
            jhub("DELETE", f"/users/{enc}/tokens/{token_id}")

def stop_server():
    enc = urllib.parse.quote(USERNAME, safe="")
    for p in [f"/users/{enc}/servers/", f"/users/{enc}/server"]:
        status, _ = jhub("DELETE", p)
        if status in (200, 202, 204): break
    print("  Stopping server", end="", flush=True)
    for _ in range(30):
        time.sleep(2)
        _, data = jhub("GET", f"/users/{enc}")
        s = data.get("servers", {}).get("", {})
        if not s.get("pending") and not s.get("ready"):
            print(" done.\r\n"); return
        print(".", end="", flush=True)
    print(" timed out.\r\n")

def start_server(profile_slug=None):
    enc = urllib.parse.quote(USERNAME, safe="")
    body = {"profile": profile_slug} if profile_slug else {}
    status, resp = jhub("POST", f"/users/{enc}/server", body)
    if status not in (200, 201, 202):
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        if status == 403 and "auth has expired" in msg:
            print("\r\n  Your JupyterHub session has expired.\r\n")
            print("  Please log in at the JupyterHub URL in your browser,\r\n")
            print("  then reconnect via SSH.\r\n")
        else:
            print(f"\r\n  Error starting server: HTTP {status} {msg}\r\n")
        return False
    print("  Starting server", end="", flush=True)
    for _ in range(60):
        time.sleep(5)
        _, data = jhub("GET", f"/users/{enc}")
        if data.get("servers", {}).get("", {}).get("ready"):
            print(" ready!\r\n"); return True
        print(".", end="", flush=True)
    print(" timed out.\r\n")
    return False

def find_pod_name(user_data):
    # state is an admin-only field; try it first, fall back to k8s label lookup
    try:
        pod_name = user_data["servers"][""]["state"]["pod_name"]
        if pod_name: return pod_name
    except (KeyError, TypeError): pass
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try: k8s_config.load_incluster_config()
        except: k8s_config.load_kube_config()
        core = k8s_client.CoreV1Api()
        safe = re.sub(r"[^a-z0-9]", "-", USERNAME.lower())
        prefix = f"jupyter-{re.sub(r'-+', '-', safe).strip('-')}"
        pods = core.list_namespaced_pod(
            NAMESPACE, label_selector="component=singleuser-server"
        )
        for p in pods.items:
            if p.metadata.name.startswith(prefix) and p.status.phase in ("Running", "Pending"):
                return p.metadata.name
    except Exception:
        pass
    safe = re.sub(r"[^a-z0-9]", "-", USERNAME.lower())
    return f"jupyter-{re.sub(r'-+', '-', safe).strip('-')}"

def pick_profile():
    profiles = get_profiles()
    if not profiles:
        print("  No profiles found — using default.\r\n")
        return None
    print("  Available images:\r\n")
    for i, p in enumerate(profiles, 1):
        print(f"  [{i}]  {p['display']}\r\n", end="")
    print()
    while True:
        try:
            choice = input(f"  Select image [1-{len(profiles)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(profiles):
                print(); return profiles[idx]["slug"]
            print(f"  Enter a number from 1 to {len(profiles)}.\r\n")
        except (ValueError, EOFError):
            print(f"  Enter a number from 1 to {len(profiles)}.\r\n")
        except KeyboardInterrupt:
            print("\r\n  Cancelled.\r\n"); sys.exit(0)

def connect_to_pod(pod_name):
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        from kubernetes.stream import stream
    except ImportError:
        print("\r\n  Error: kubernetes package missing.\r\n"); sys.exit(1)
    try: k8s_config.load_incluster_config()
    except: k8s_config.load_kube_config()
    core = k8s_client.CoreV1Api()
    print(f"  Connecting to {pod_name}...\r\n")
    try:
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            pod_name, NAMESPACE,
            command=["/bin/bash", "-l"],
            container="notebook",
            stderr=True, stdin=True, stdout=True, tty=True,
            _preload_content=False,
        )
    except Exception as e:
        print(f"\r\n  Failed to connect to pod: {e}\r\n"); sys.exit(1)

    def _get_size():
        try:
            s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00"*8)
            rows, cols = struct.unpack("HHHH", s)[:2]
            return rows or 24, cols or 80
        except: return 24, 80

    def _send_resize():
        rows, cols = _get_size()
        try: resp.write_channel(4, json.dumps({"Width": cols, "Height": rows}))
        except: pass

    _send_resize()
    signal.signal(signal.SIGWINCH, lambda *_: _send_resize())
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setraw(fd)
    stop = threading.Event()

    def _stdin_reader():
        while not stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r and resp.is_open():
                    data = os.read(fd, 4096)
                    if data: resp.write_stdin(data.decode("utf-8", errors="replace"))
            except: break

    threading.Thread(target=_stdin_reader, daemon=True).start()
    try:
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                out = resp.read_stdout()
                if out:
                    sys.stdout.buffer.write(out if isinstance(out, bytes) else out.encode())
                    sys.stdout.buffer.flush()
            if resp.peek_stderr():
                err = resp.read_stderr()
                if err:
                    sys.stderr.buffer.write(err if isinstance(err, bytes) else err.encode())
                    sys.stderr.buffer.flush()
    finally:
        stop.set()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        resp.close()

def main():
    if not USERNAME:
        print("Error: username not provided.\r\n"); sys.exit(1)
    print("\r\n")
    print("  ╔══════════════════════════════════════════════════╗\r\n", end="")
    print("  ║          JupyterHub SSH Gateway                  ║\r\n", end="")
    print("  ╚══════════════════════════════════════════════════╝\r\n", end="")
    print(f"  User: {USERNAME}\r\n\n")
    user_data = get_user_data()
    server = user_data.get("servers", {}).get("", {})
    if server.get("ready"):
        print("  Your Jupyter server is already running.\r\n")
        print("  [1]  Connect to running server\r\n", end="")
        print("  [2]  Stop server and restart with a new image\r\n\n", end="")
        try: choice = input("  Choice [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt): choice = "1"
        if choice == "2":
            print(); stop_server()
            profile = pick_profile()
            if not start_server(profile): sys.exit(1)
            user_data = get_user_data()
    else:
        print("  Your Jupyter server is not running.\r\n")
        profile = pick_profile()
        if not start_server(profile): sys.exit(1)
        user_data = get_user_data()
    connect_to_pod(find_pod_name(user_data))

if __name__ == "__main__":
    main()
