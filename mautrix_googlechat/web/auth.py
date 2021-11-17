# mautrix-googlechat - A Matrix-Google Chat puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Optional, Dict, Any
from time import time
import pkg_resources
import urllib.parse
import asyncio
import logging
import string
import random

from aiohttp import web

from hangups.auth import OAUTH2_CLIENT_ID, OAUTH2_SCOPES, TokenManager, GoogleAuthError
from mautrix.types import UserID
from mautrix.util.signed_token import sign_token, verify_token

from .. import user as u


class ErrorResponse(Exception):
    def __init__(self, status_code: int, error: str, errcode: str,
                 extra_data: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.message = error
        self.error = error
        self.errcode = errcode
        self.payload = {
            **(extra_data or {}),
            "error": self.error,
            "errcode": self.errcode
        }


@web.middleware
async def error_middleware(request: web.Request, handler) -> web.Response:
    try:
        return await handler(request)
    except ErrorResponse as e:
        return web.json_response(status=e.status_code, data=e.payload)


log = logging.getLogger("mau.gc.auth")

LOGIN_TIMEOUT = 10 * 60


def make_login_url(device_name: str) -> str:
    query = urllib.parse.urlencode({
        "scope": "+".join(OAUTH2_SCOPES),
        "client_id": OAUTH2_CLIENT_ID,
        "device_name": device_name,
    }, safe='+')
    return f"https://accounts.google.com/o/oauth2/programmatic_auth?{query}"


class GoogleChatAuthServer:
    app: web.Application
    shared_secret: Optional[str]
    secret_key: str
    device_name: str

    def __init__(self, shared_secret: Optional[str], device_name: str,
                 loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        loop = loop or asyncio.get_event_loop()
        self.app = web.Application(loop=loop, middlewares=[error_middleware])
        self.ongoing = {}
        self.device_name = device_name
        self.shared_secret = shared_secret
        self.secret_key = "".join(random.choices(string.ascii_lowercase + string.digits, k=64))
        self.app.router.add_post("/api/verify", self.verify)
        self.app.router.add_post("/api/start", self.start_login)
        self.app.router.add_post("/api/logout", self.logout)
        self.app.router.add_post("/api/authorization", self.do_login)
        self.app.router.add_get("/api/whoami", self.whoami)
        self.app.router.add_get("", self.redirect_index)
        self.app.router.add_get("/", self.get_index)
        self.app.router.add_static("/", pkg_resources.resource_filename("mautrix_googlechat",
                                                                        "web/static/"))

    @staticmethod
    async def redirect_index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(pkg_resources.resource_filename("mautrix_googlechat",
                                                                "web/static/login-redirect.html"))

    @staticmethod
    async def get_index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(pkg_resources.resource_filename("mautrix_googlechat",
                                                                "web/static/login.html"))

    def make_token(self, user_id: UserID) -> str:
        return sign_token(self.secret_key, {
            "user_id": user_id,
            "expiry": int(time()) + LOGIN_TIMEOUT,
        })

    def verify_token(self, request: web.Request, allow_expired: bool = False) -> Optional[UserID]:
        try:
            token = request.headers["Authorization"]
        except KeyError:
            raise ErrorResponse(401, "Missing access token", "M_MISSING_TOKEN")
        if not token.startswith("Bearer "):
            raise ErrorResponse(401, "Invalid authorization header content", "M_MISSING_TOKEN")
        token = token[len("Bearer "):]
        if self.shared_secret and token == self.shared_secret:
            try:
                return UserID(request.query["user_id"])
            except KeyError:
                raise ErrorResponse(400, "Missing user_id query parameter", "M_MISSING_PARAM")
        data = verify_token(self.secret_key, token)
        if not data:
            raise ErrorResponse(401, "Invalid access token", "M_UNKNOWN_TOKEN")
        elif not allow_expired and data["expiry"] < int(time()):
            raise ErrorResponse(401, "Access token expired", "M_EXPIRED_TOKEN")
        return data["user_id"]

    async def verify(self, request: web.Request) -> web.Response:
        return web.json_response({
            "user_id": self.verify_token(request),
        })

    async def logout(self, request: web.Request) -> web.Response:
        user_id = self.verify_token(request)
        user = await u.User.get_by_mxid(user_id)
        if not await user.is_logged_in():
            raise ErrorResponse(400, "You're not logged in", "M_FORBIDDEN")
        await user.logout()
        return web.json_response({})

    async def whoami(self, request: web.Request) -> web.Response:
        user_id = self.verify_token(request)
        user = await u.User.get_by_mxid(user_id)
        return web.json_response({
            "permissions": user.level,
            "mxid": user.mxid,
            "googlechat": {
                "name": user.name,
                "id": user.gcid,
                "connected": user.connected,
            } if user.client else None,
        })

    async def start_login(self, request: web.Request) -> web.Response:
        user_id = self.verify_token(request)
        user = await u.User.get_by_mxid(user_id)
        if user.client:
            return web.json_response({
                "status": "success",
                "name": await user.name_future,
            })
        return web.json_response({
            "next_step": "authorization",
            "manual_auth_url": make_login_url(self.device_name),
        })

    async def do_login(self, request: web.Request) -> web.Response:
        user_id = self.verify_token(request, allow_expired=True)
        user = await u.User.get_by_mxid(user_id)
        if user.client:
            return web.json_response({
                "status": "success",
                "name": await user.name_future,
            })
        data = await request.json()
        if not data:
            raise ErrorResponse(400, "Body is not JSON", "M_NOT_JSON")
        try:
            auth = data["authorization"]
        except KeyError:
            raise ErrorResponse(400, "Request body did not contain authorization field", "M_BAD_REQUEST")

        try:
            token_mgr = await TokenManager.from_authorization_code(
                auth, u.UserRefreshTokenCache(user))
        except GoogleAuthError as e:
            log.exception(f"Login for {user.mxid} failed")
            return web.json_response({
                "status": "fail",
                "error": str(e),
            })
        except Exception:
            log.exception(f"Login for {user.mxid} errored")
            return web.json_response({
                "status": "fail",
                "error": "internal error",
            }, status=500)
        else:
            await user.login_complete(token_mgr)
            return web.json_response({
                "status": "success",
                "name": await user.name_future,
            })