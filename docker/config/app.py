import os
import logging

from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NAMESPACE = os.environ.get("NAMESPACE", "default")
LAUNCHER_POD_NAME = os.environ.get("LAUNCHER_POD_NAME", "containerssh-launcher")

# When the user's notebook pod is already running we exec straight into it, so
# that shell, exec (scp/rsync/VS Code Remote-SSH) and subsystem (sftp) requests
# all land on the user's own filesystem. The launcher is only used to start a
# stopped server, because that needs the interactive image picker.
USER_POD_ENABLED = os.environ.get("USER_POD_ENABLED", "true").lower() == "true"
USER_NAMESPACE = os.environ.get("USER_NAMESPACE", NAMESPACE)
USER_POD_SELECTOR = os.environ.get("USER_POD_SELECTOR", "component=singleuser-server")
# KubeSpawner stores the raw (unescaped) username here; the same-named *label*
# holds the escaped form, so match on the annotation.
USERNAME_ANNOTATION = os.environ.get(
    "USERNAME_ANNOTATION", "hub.jupyter.org/username"
)
NOTEBOOK_CONTAINER = os.environ.get("NOTEBOOK_CONTAINER", "notebook")
# Delivered by an initContainer on the singleuser pod (see README); empty
# disables the guest agent instead of pointing at a path that isn't there.
AGENT_PATH = os.environ.get("AGENT_PATH", "/opt/containerssh/containerssh-agent")
SHELL_COMMAND = os.environ.get("SHELL_COMMAND", "/bin/bash -l").split()
# Optional "name=/path" pairs, e.g. "sftp=/usr/libexec/openssh/sftp-server".
# Only set these once the executable actually exists in the singleuser image;
# a missing subsystem binary makes the client's request fail.
SUBSYSTEMS = os.environ.get("SUBSYSTEMS", "")


def _parse_subsystems(raw: str) -> dict:
    out = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, path = item.partition("=")
        name, path = name.strip(), path.strip()
        if name and path:
            out[name] = path
    return out


def find_user_pod(username: str):
    """Return (pod_name, console_container_number) for a ready notebook pod.

    Returns (None, 0) when the user has no running server, or when the lookup
    fails for any reason — the caller then falls back to the launcher, so a
    broken lookup degrades to today's behaviour instead of refusing the
    connection.
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        core = k8s_client.CoreV1Api()
        pods = core.list_namespaced_pod(
            USER_NAMESPACE, label_selector=USER_POD_SELECTOR
        )
    except Exception as exc:
        logger.warning("User pod lookup failed for %s: %r", username, exc)
        return None, 0

    for pod in pods.items:
        annotations = pod.metadata.annotations or {}
        if annotations.get(USERNAME_ANNOTATION) != username:
            continue
        if pod.status.phase != "Running":
            continue

        # consoleContainerNumber indexes spec.containers, whose order does not
        # match status.containerStatuses — resolve the index from the spec and
        # the readiness from the status, both by container name.
        names = [c.name for c in (pod.spec.containers or [])]
        if NOTEBOOK_CONTAINER in names:
            index = names.index(NOTEBOOK_CONTAINER)
        else:
            logger.warning(
                "Pod %s has no %r container (has %s); using index 0",
                pod.metadata.name,
                NOTEBOOK_CONTAINER,
                names,
            )
            index = 0

        ready = False
        for status in pod.status.container_statuses or []:
            if status.name == names[index]:
                ready = bool(status.ready)
                break
        if not ready:
            logger.info(
                "Pod %s found for %s but container %s is not ready",
                pod.metadata.name,
                username,
                names[index],
            )
            continue

        return pod.metadata.name, index

    return None, 0


def launcher_config(username: str) -> dict:
    """Exec the interactive launcher, which starts the user's server."""
    return {
        "backend": "kubernetes",
        "kubernetes": {
            "pod": {
                "metadata": {
                    "name": LAUNCHER_POD_NAME,
                    "namespace": NAMESPACE,
                },
                "mode": "persistent",
                "shellCommand": ["/app/launcher.py", username],
            },
        },
    }


def user_pod_config(pod_name: str, container_number: int) -> dict:
    """Exec directly into the user's running notebook pod."""
    pod = {
        "metadata": {
            "name": pod_name,
            "namespace": USER_NAMESPACE,
        },
        "mode": "persistent",
        "consoleContainerNumber": container_number,
        "shellCommand": SHELL_COMMAND,
    }
    if AGENT_PATH:
        pod["agentPath"] = AGENT_PATH
    else:
        pod["disableAgent"] = True
    subsystems = _parse_subsystems(SUBSYSTEMS)
    if subsystems:
        pod["subsystems"] = subsystems
    return {"backend": "kubernetes", "kubernetes": {"pod": pod}}


@app.route("/", methods=["POST"])
@app.route("/config", methods=["POST"])
def config():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("authenticatedUsername") or data.get("username", "")
    remote_address = data.get("remoteAddress", "")
    connection_id = data.get("connectionId", "")

    logger.info(
        "Config request: username=%s remoteAddress=%s connectionId=%s",
        username,
        remote_address,
        connection_id,
    )

    if USER_POD_ENABLED and username:
        pod_name, container_number = find_user_pod(username)
        if pod_name:
            logger.info(
                "Routing %s to notebook pod %s (container %s)",
                username,
                pod_name,
                container_number,
            )
            return jsonify({"config": user_pod_config(pod_name, container_number)})
        logger.info(
            "No ready notebook pod for %s; routing to launcher %s",
            username,
            LAUNCHER_POD_NAME,
        )

    return jsonify({"config": launcher_config(username)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
