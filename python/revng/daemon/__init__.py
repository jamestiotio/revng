#
# This file is distributed under the MIT License. See LICENSE.md for details.
#

import asyncio
import logging
import os
import signal
from datetime import timedelta
from importlib import import_module
from typing import List

from starlette.applications import Starlette
from starlette.config import Config
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLHTTPHandler, GraphQLTransportWSHandler
from ariadne.contrib.tracing.apollotracing import ApolloTracingExtension

from revng.api import Manager
from revng.api._capi import initialize as capi_initialize
from revng.api._capi import shutdown as capi_shutdown

from .event_manager import EventManager
from .graphql import get_schema
from .util import project_workdir

config = Config()
DEBUG = config("STARLETTE_DEBUG", cast=bool, default=False)


class ManagerCredentialsMiddleware:
    def __init__(self, app: ASGIApp, event_manager: EventManager):
        self.app = app
        self.event_manager = event_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        credentials = Headers(scope=scope).get("x-revng-set-credentials")
        if credentials is not None:
            self.event_manager.set_credentials(credentials)

        return await self.app(scope, receive, send)


def get_middlewares(event_manager: EventManager) -> List[Middleware]:
    extra_middlewares_early = parse_middleware_env(os.environ.get("STARLETTE_MIDDLEWARES_EARLY"))
    extra_middlewares_late = parse_middleware_env(os.environ.get("STARLETTE_MIDDLEWARES_LATE"))

    origins: List[str] = []
    if "REVNG_ORIGINS" in os.environ:
        origins = os.environ["REVNG_ORIGINS"].split(",")

    expose_headers: List[str] = []
    if "REVNG_EXPOSE_HEADERS" in os.environ:
        expose_headers = os.environ["REVNG_EXPOSE_HEADERS"].split(",")

    return [
        *extra_middlewares_early,
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            expose_headers=expose_headers,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(GZipMiddleware, minimum_size=1024),
        *extra_middlewares_late,
        Middleware(ManagerCredentialsMiddleware, event_manager=event_manager),
    ]


def parse_middleware_env(string: str | None) -> List[Middleware]:
    if string is None:
        return []

    result = []
    for element in string.split(","):
        module_path, attribute_name = element.rsplit(".", 1)
        module = import_module(module_path)
        if not hasattr(module, attribute_name):
            raise ValueError(f"Middleware not found: {element}")
        result.append(getattr(module, attribute_name))

    return result


def make_startlette() -> Starlette:
    capi_initialize(
        signals_to_preserve=(
            # Common terminal signals
            signal.SIGINT,
            signal.SIGTERM,
            signal.SIGHUP,
            signal.SIGCHLD,
            # Issued by writing in closed sockets
            signal.SIGPIPE,
            # Used by uvicorn workers
            signal.SIGUSR1,
            signal.SIGUSR2,
            signal.SIGQUIT,
        )
    )
    manager = Manager(project_workdir())
    event_manager = EventManager(manager)
    event_manager.start()
    startup_done = False

    if DEBUG:
        print(f"Manager workdir is: {manager.workdir}")

    async def index_page(request):
        return PlainTextResponse("")

    async def status(request):
        if startup_done:
            return PlainTextResponse("OK")
        else:
            return PlainTextResponse("KO", 503)

    def generate_context(request: Request):
        return {
            "manager": manager,
            "event_manager": event_manager,
            # Lock for operations that have an `index` parameter. This is
            # needed because otherwise there's a TOCTOU between when the index
            # is checked and when the analysis actually bumps the index.
            "index_lock": asyncio.Lock(),
            "headers": request.headers,
        }

    routes = [
        Route("/", index_page, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Mount(
            "/graphql",
            GraphQL(
                get_schema(),
                context_value=generate_context,
                http_handler=GraphQLHTTPHandler(extensions=[ApolloTracingExtension]),
                websocket_handler=GraphQLTransportWSHandler(
                    connection_init_wait_timeout=timedelta(seconds=5)
                ),
                debug=DEBUG,
            ),
        ),
    ]

    def startup():
        nonlocal startup_done
        startup_done = True

    def shutdown():
        event_manager.running = False
        store_result = manager.save()
        if not store_result:
            logging.warning("Failed to store manager's containers")
        manager._manager = None
        capi_shutdown()

    return Starlette(
        debug=DEBUG,
        middleware=get_middlewares(event_manager),
        routes=routes,
        on_startup=[startup],
        on_shutdown=[shutdown],
    )


app = make_startlette()
