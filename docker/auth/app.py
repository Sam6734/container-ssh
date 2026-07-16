import base64
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
# Scoped JupyterHub service token (see README "Registering the JupyterHub
# service"). Login validation uses the user's own token; this is only a
# fallback for the token-age check when the user's token can't list itself.
JHUB_SERVICE_TOKEN = os.environ.get("JHUB_ADMIN_TOKEN", "")
TOKEN_MAX_AGE_DAYS = int(os.environ.get("TOKEN_MAX_AGE_DAYS", "7"))

# Matches emails-as-usernames like sam-albin-unl-edu (no bots like root, admin)
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[a-z0-9][a-z0-9-]*$")


def normalize_username(email: str) -> str:
    """Normalize a JupyterHub email username to SSH-safe form.

    Replaces '@' and '.' with '-', e.g. sam.albin@unl.edu -> sam-albin-unl-edu.
    """
    return email.replace("@", "-").replace(".", "-")


def has_recent_token(
    username: str, auth_token: str, jhub_url: str, max_age_days: int
) -> bool | None:
    """Return True if the user has a token created within *max_age_days* days.

    Returns None (rather than False) when the token list could not be
    fetched, so the caller can distinguish "no recent token" from
    "couldn't check" and fall back to another credential.
    """
    enc = quote(username, safe="")
    url = f"{jhub_url}/hub/api/users/{enc}/tokens"
    headers = {"Authorization": f"token {auth_token}"}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("has_recent_token %s: HTTP %s", username, resp.status_code)
            return None
        data = resp.json()
        # JupyterHub >= 2 wraps the list: {"api_tokens": [...]}
        tokens = data.get("api_tokens", []) if isinstance(data, dict) else data
    except requests.RequestException as exc:
        logger.error("has_recent_token request failed: %s", exc)
        return None
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
    return False


@app.route("/password", methods=["POST"])
def password():
    data = request.get_json(force=True, silent=True) or {}
    ssh_username = data.get("username", "")
    token = data.get("password", "")
    if not token and data.get("passwordBase64"):
        password_b64 = data.get("passwordBase64", "")
        try:
            token = base64.b64decode(
                password_b64 + ("=" * (-len(password_b64) % 4))
            ).decode("utf-8").strip()
        except Exception as exc:
            logger.error("passwordBase64 decode failed for %s: %s", ssh_username, exc)
            return jsonify({"success": False})

    if not token:
        logger.warning("No password/token supplied for %s", ssh_username)
        return jsonify({"success": False})

    logger.info("Auth attempt for SSH username: %s", ssh_username)

    # Normalize: accept email form (sam.albin@unl.edu) or already-dashed form
    ssh_username = normalize_username(ssh_username)

    # 1. Validate username format: reject bots, root, admin, etc.
    if not USERNAME_RE.match(ssh_username):
        logger.warning("Rejected username (bad format): %s", ssh_username)
        return jsonify({"success": False})

    # 2. Validate the token by asking JupyterHub who owns it, authenticating
    #    as the user. A valid token returns its owner's user model (including
    #    admin status), so no admin/service credential is needed to log in.
    #    Hubs with auth_refresh_age reject user-token API calls once the
    #    owner's upstream OAuth state goes stale ("Login is required to
    #    refresh"), so fall back to resolving the token with the service
    #    token, which is not subject to the owner's auth freshness.
    user = None
    try:
        user_resp = requests.get(
            f"{JHUB_URL}/hub/api/user",
            headers={"Authorization": f"token {token}"},
            timeout=10,
        )
        if user_resp.status_code == 200:
            user = user_resp.json()
        else:
            logger.info(
                "Self-lookup failed for %s (HTTP %s), trying service lookup",
                ssh_username,
                user_resp.status_code,
            )
    except requests.RequestException as exc:
        logger.error("Token validation request failed: %s", exc)

    if user is None and JHUB_SERVICE_TOKEN:
        try:
            lookup_resp = requests.get(
                f"{JHUB_URL}/hub/api/authorizations/token/{quote(token, safe='')}",
                headers={"Authorization": f"token {JHUB_SERVICE_TOKEN}"},
                timeout=10,
            )
            if lookup_resp.status_code == 200:
                user = lookup_resp.json()
        except requests.RequestException as exc:
            logger.error("Service token lookup failed: %s", exc)

    if user is None:
        logger.warning("Token validation failed for %s", ssh_username)
        return jsonify({"success": False})
    if user.get("kind", "user") != "user":
        logger.warning(
            "Rejected non-user token for %s (kind=%s)",
            ssh_username,
            user.get("kind"),
        )
        return jsonify({"success": False})

    jhub_username = user.get("name", "")
    if not jhub_username:
        logger.warning("User response missing 'name' field")
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

    is_admin = bool(user.get("admin", False))

    # 4. Non-admins must have a token created within TOKEN_MAX_AGE_DAYS.
    #    Check with the user's own token; fall back to the service token if
    #    the user's token lacks the scope to list itself.
    if not is_admin:
        recent = has_recent_token(
            jhub_username, token, JHUB_URL, TOKEN_MAX_AGE_DAYS
        )
        if recent is None and JHUB_SERVICE_TOKEN:
            recent = has_recent_token(
                jhub_username, JHUB_SERVICE_TOKEN, JHUB_URL, TOKEN_MAX_AGE_DAYS
            )
        if not recent:
            logger.warning(
                "No recent token for non-admin user %s (max_age=%s days)",
                jhub_username,
                TOKEN_MAX_AGE_DAYS,
            )
            return jsonify({"success": False})

    logger.info("Auth success for %s (admin=%s)", jhub_username, is_admin)
    return jsonify({"success": True, "authenticatedUsername": jhub_username})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
