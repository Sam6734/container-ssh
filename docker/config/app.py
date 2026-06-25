import os
import logging

from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NAMESPACE = os.environ.get("NAMESPACE", "default")
LAUNCHER_POD_NAME = os.environ.get("LAUNCHER_POD_NAME", "containerssh-launcher")


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

    return jsonify({
        "config": {
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
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
