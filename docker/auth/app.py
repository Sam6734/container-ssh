import os
import re
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

JHUB_URL = os.environ.get("JHUB_URL", "http://hub:8081")
JHUB_ADMIN_TOKEN = os.environ.get("JHUB_ADMIN_TOKEN", "")
TOKEN_MAX_AGE_DAYS = int(os.environ.get("TOKEN_MAX_AGE_DAYS", "7"))

# Matches emails-as-usernames like sam-albin-unl-edu (no bots like root, admin)
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[a-z0-9][a-z0-9-]*$")


def normalize_username(email: str) -> str:
    """Normalize a JupyterHub email username to SSH-safe form.

    Replaces '@' and '.' with '-', e.g. sam.albin@unl.edu -> sam-albin-unl-edu.
    """
    return email.replace("@", "-").replace(".", "-")


def get_user_info(username: str, admin_token: str, jhub_url: str) -> dict | None:
    """Fetch JupyterHub user record for *username* using the admin token."""
    enc = quote(username, safe="")
    url = f"{jhub_url}/hub/api/users/{enc}"
    headers = {"Authorization": f"token {admin_token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("get_user_info %s: HTTP %s", username, resp.status_code)
    except requests.RequestException as exc:
        logger.error("get_user_info request failed: %s", exc)
    return None


def has_recent_token(
    username: str, admin_token: str, jhub_url: str, max_age_days: int
) -> bool:
    """Return True if the user has at least one token created within *max_age_days* days."""
    enc = quote(username, safe="")
    url = f"{jhub_url}/hub/api/users/{enc}/tokens"
    headers = {"Authorization": f"token {admin_token}"}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("has_recent_token %s: HTTP %s", username, resp.status_code)
            return False
        tokens = resp.json()
        for token in tokens:
            created_str = token.get("created")
            if not created_str:
                continue
            # JupyterHub returns ISO 8601, e.g. "2024-01-15T12:00:00.000000Z"
            try:
                created = datetime.fromisoformat(
                    created_str.replace("Z", "+00:00")
                )
                if created >= cutoff:
                    return True
            except ValueError:
                logger.warning("Unparseable token created time: %s", created_str)
    except requests.RequestException as exc:
        logger.error("has_recent_token request failed: %s", exc)
    return False


@app.route("/password", methods=["POST"])
def password():
    data = request.get_json(force=True, silent=True) or {}
    ssh_username = data.get("username", "")
    token = data.get("password", "")

    logger.info("Auth attempt for SSH username: %s", ssh_username)

    # 1. Validate username format — reject bots, root, admin, etc.
    if not USERNAME_RE.match(ssh_username):
        logger.warning("Rejected username (bad format): %s", ssh_username)
        return jsonify({"success": False})

    # 2. Validate the token against JupyterHub and retrieve the JupyterHub username.
    token_url = f"{JHUB_URL}/hub/api/authorizations/token/{quote(token, safe='')}"
    headers = {"Authorization": f"token {JHUB_ADMIN_TOKEN}"}
    try:
        token_resp = requests.get(token_url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        logger.error("Token validation request failed: %s", exc)
        return jsonify({"success": False})

    if token_resp.status_code != 200:
        logger.warning(
            "Token validation failed for %s: HTTP %s",
            ssh_username,
            token_resp.status_code,
        )
        return jsonify({"success": False})

    token_data = token_resp.json()
    jhub_username = token_data.get("name", "")
    if not jhub_username:
        logger.warning("Token response missing 'name' field")
        return jsonify({"success": False})

    # 3. Confirm the token belongs to the SSH user who is logging in.
    if normalize_username(jhub_username) != ssh_username:
        logger.warning(
            "Username mismatch: SSH=%s JHub=%s (normalized=%s)",
            ssh_username,
            jhub_username,
            normalize_username(jhub_username),
        )
        return jsonify({"success": False})

    # 4. Fetch full user info to check admin status.
    user_info = get_user_info(jhub_username, JHUB_ADMIN_TOKEN, JHUB_URL)
    is_admin = bool(user_info and user_info.get("admin", False))

    # 5. Non-admins must have a token created within TOKEN_MAX_AGE_DAYS.
    if not is_admin:
        if not has_recent_token(
            jhub_username, JHUB_ADMIN_TOKEN, JHUB_URL, TOKEN_MAX_AGE_DAYS
        ):
            logger.warning(
                "No recent token for non-admin user %s (max_age=%s days)",
                jhub_username,
                TOKEN_MAX_AGE_DAYS,
            )
            return jsonify({"success": False})

    logger.info("Auth success for %s (admin=%s)", jhub_username, is_admin)
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
