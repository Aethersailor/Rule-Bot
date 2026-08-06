"""Minimal authenticated HTTP ingress for MatchScope domain submissions."""

import hmac
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from aiohttp import web
from loguru import logger


@dataclass(frozen=True)
class ListenerConfig:
    name: str
    host: str
    port: int
    path: str
    source: str
    static_token: str = ""
    tls_cert_file: str = ""
    tls_key_file: str = ""


class MatchScopeAPIServer:
    """Run the private and community listeners without exposing discovery routes."""

    def __init__(self, config, handler_manager):
        self.config = config
        self.handler_manager = handler_manager
        self._runners: list[web.AppRunner] = []
        self._request_history: dict[tuple[str, str | int], list[float]] = defaultdict(list)

    async def start(self) -> None:
        listeners = []
        if self.config.MATCHSCOPE_PRIVATE_API_ENABLED:
            listeners.append(
                ListenerConfig(
                    name="private",
                    host=self.config.MATCHSCOPE_PRIVATE_API_HOST,
                    port=self.config.MATCHSCOPE_PRIVATE_API_PORT,
                    path=self.config.MATCHSCOPE_PRIVATE_API_PATH,
                    source="matchscope_private",
                    static_token=self.config.MATCHSCOPE_PRIVATE_API_TOKEN,
                    tls_cert_file=self.config.MATCHSCOPE_PRIVATE_API_TLS_CERT_FILE,
                    tls_key_file=self.config.MATCHSCOPE_PRIVATE_API_TLS_KEY_FILE,
                )
            )
        if self.config.MATCHSCOPE_PUBLIC_API_ENABLED:
            listeners.append(
                ListenerConfig(
                    name="community",
                    host=self.config.MATCHSCOPE_PUBLIC_API_HOST,
                    port=self.config.MATCHSCOPE_PUBLIC_API_PORT,
                    path=self.config.MATCHSCOPE_PUBLIC_API_PATH,
                    source="matchscope_community",
                    tls_cert_file=self.config.MATCHSCOPE_PUBLIC_API_TLS_CERT_FILE,
                    tls_key_file=self.config.MATCHSCOPE_PUBLIC_API_TLS_KEY_FILE,
                )
            )

        for listener in listeners:
            await self._start_listener(listener)

    async def _start_listener(self, listener: ListenerConfig) -> None:
        app = self._build_app(listener)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            listener.host,
            listener.port,
            ssl_context=self._ssl_context(listener),
        )
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runners.append(runner)
        scheme = "https" if listener.tls_cert_file else "http"
        logger.info(
            "MatchScope {} API 已监听 {}://{}:{}（路径已隐藏）",
            listener.name,
            scheme,
            listener.host,
            listener.port,
        )

    def _build_app(self, listener: ListenerConfig) -> web.Application:
        @web.middleware
        async def conceal_routes(request, handler):
            try:
                response = await handler(request)
            except web.HTTPRequestEntityTooLarge:
                response = self._json_response("invalid_request", 413)
            except web.HTTPException as error:
                if error.status in (404, 405):
                    response = web.Response(status=404, text="Not Found")
                else:
                    raise
            except Exception as error:
                logger.warning(
                    "MatchScope API 请求处理失败: {}（异常正文不写入日志）",
                    type(error).__name__,
                )
                response = self._json_response("temporary_error", 503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Server"] = ""
            return response

        app = web.Application(client_max_size=1024, middlewares=[conceal_routes])

        async def submit(request: web.Request) -> web.Response:
            return await self._handle_submission(request, listener)

        app.router.add_post(listener.path, submit)
        return app

    @staticmethod
    def _ssl_context(listener: ListenerConfig) -> Optional[ssl.SSLContext]:
        if not listener.tls_cert_file:
            return None
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(listener.tls_cert_file, listener.tls_key_file)
        return context

    async def stop(self) -> None:
        for runner in reversed(self._runners):
            await runner.cleanup()
        self._runners.clear()

    @staticmethod
    def _bearer_token(request: web.Request) -> str:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return ""
        return token.strip()

    async def _authenticate(
        self, request: web.Request, listener: ListenerConfig
    ) -> Optional[str | int]:
        token = self._bearer_token(request)
        if not token:
            return None
        if listener.source == "matchscope_private":
            return 0 if hmac.compare_digest(token, listener.static_token) else None
        token_service = self.handler_manager.matchscope_token_service
        return await token_service.verify(token) if token_service else None

    async def _handle_submission(
        self, request: web.Request, listener: ListenerConfig
    ) -> web.Response:
        subject = await self._authenticate(request, listener)
        if subject is None:
            return self._json_response("unauthorized", 401)

        max_requests = (
            self.config.MATCHSCOPE_PRIVATE_RATE_LIMIT_PER_HOUR
            if listener.source == "matchscope_private"
            else self.config.MATCHSCOPE_PUBLIC_RATE_LIMIT_PER_HOUR
        )
        if not self._consume_request_slot(listener.source, subject, max_requests):
            return self._json_response("rate_limited", 429)

        if request.content_type != "application/json":
            return self._json_response("invalid_request", 400)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return self._json_response("invalid_request", 400)
        if (
            not isinstance(body, dict)
            or set(body) != {"version", "domain"}
            or body.get("version") != 1
            or not isinstance(body.get("domain"), str)
            or len(body["domain"]) > 253
        ):
            return self._json_response("invalid_request", 400)

        result = await self.handler_manager.submit_matchscope_domain(
            body["domain"],
            source=listener.source,
            rate_key=(f"{listener.source}:adds", subject),
            max_adds=max_requests,
        )
        status = result.get("status", "temporary_error")
        http_status = {
            "added": 201,
            "exists_rules": 200,
            "exists_geosite": 200,
            "ignored_cn": 200,
            "rejected_policy": 200,
            "invalid_domain": 400,
            "rate_limited": 429,
            "temporary_error": 503,
        }.get(status, 503)
        payload = {
            "version": 1,
            "status": status,
        }
        if result.get("domain"):
            payload["domain"] = result["domain"]
        if result.get("commit_url"):
            payload["commit_url"] = result["commit_url"]
        return web.json_response(payload, status=http_status)

    def _consume_request_slot(
        self, source: str, subject: str | int, limit: int
    ) -> bool:
        key = (source, subject)
        now = time.time()
        cutoff = now - 3600
        history = [timestamp for timestamp in self._request_history[key] if timestamp > cutoff]
        if len(history) >= limit:
            self._request_history[key] = history
            return False
        history.append(now)
        self._request_history[key] = history
        return True

    @staticmethod
    def _json_response(status: str, http_status: int) -> web.Response:
        return web.json_response(
            {"version": 1, "status": status},
            status=http_status,
            headers={"Cache-Control": "no-store"},
        )
