from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from work_retrieval_core import (
    SearchAuditTrace,
    SearchEngine,
    SearchQuery,
    SearchResult,
    SearchUnavailableError,
)

from work_retrieval_api.models import (
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    JobId,
    JobResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    validation_details,
)
from work_retrieval_api.runtime import RetrievalRuntime, RuntimeFactory

MAX_BODY_BYTES = 16 * 1024
MAX_RESULTS = 10
MAX_AUDIT_HEADER_BYTES = 7 * 1024
SEARCH_PATH = "/api/v1/jobs/search"
SEARCH_AUDIT_HEADER = "X-Search-Audit"
logger = logging.getLogger("work_retrieval.access")
internal_logger = logging.getLogger("work_retrieval.internal")


class EngineContractError(RuntimeError):
    pass


def _request_id(request: Request) -> str:
    return cast(str, request.state.request_id)


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        request_id=_request_id(request),
        error=ErrorBody(code=code, message=message, details=details or []),
    )
    return JSONResponse(status_code=status, content=payload.model_dump())


def _validate_engine_output(result: object) -> tuple[tuple[str, ...], str]:
    if not isinstance(result, SearchResult):
        raise EngineContractError("engine result must be SearchResult")
    job_ids = result.job_ids
    if not isinstance(job_ids, tuple):
        raise EngineContractError("engine job_ids must be a tuple")
    if len(job_ids) > MAX_RESULTS:
        raise EngineContractError("engine returned too many jobs")
    if any(
        not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdecimal()
        for job_id in job_ids
    ):
        raise EngineContractError("engine returned an invalid job_id")
    if len(job_ids) != len(set(job_ids)):
        raise EngineContractError("engine returned duplicate job_id")
    if not isinstance(result.trace, SearchAuditTrace):
        raise EngineContractError("engine returned an invalid audit trace")
    if tuple(item.job_id for item in result.trace.results) != job_ids:
        raise EngineContractError("audit result order does not match engine job_ids")
    audit_json = result.trace.to_json()
    if len(audit_json.encode("ascii")) > MAX_AUDIT_HEADER_BYTES:
        raise EngineContractError("audit trace exceeds the response header budget")
    return job_ids, audit_json


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = cast(dict[str, Any], scope.setdefault("state", {}))
        state.update(
            request_id=f"req_{uuid.uuid4().hex}",
            query_len=0,
            location_code_count=0,
            duty_code_count=0,
            result_count=0,
        )
        path = str(scope["path"])
        method = str(scope["method"])
        status = 500
        started = time.perf_counter()

        async def send_with_context(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message).append("X-Request-Id", str(state["request_id"]))
            await send(message)

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        response: Response | None = None
        declared_length: int | None = None
        if (length := headers.get("content-length")) is not None:
            if not length.isdecimal() or int(length) > MAX_BODY_BYTES:
                response = _error(
                    Request(scope),
                    413,
                    "payload_too_large",
                    f"Request body must not exceed {MAX_BODY_BYTES} bytes.",
                )
            else:
                declared_length = int(length)

        requires_json = method == "POST" and path == SEARCH_PATH
        if (
            response is None
            and (requires_json or (declared_length or 0) > 0)
            and content_type != "application/json"
        ):
            response = _error(
                Request(scope),
                415,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )

        buffered: deque[Message] = deque()
        consumed = 0
        if response is None:
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] != "http.request":
                    break
                consumed += len(message.get("body", b""))
                if consumed > MAX_BODY_BYTES:
                    response = _error(
                        Request(scope),
                        413,
                        "payload_too_large",
                        f"Request body must not exceed {MAX_BODY_BYTES} bytes.",
                    )
                    buffered.clear()
                    break
                if not message.get("more_body", False):
                    break

        if response is None and consumed > 0 and content_type != "application/json":
            response = _error(
                Request(scope),
                415,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )
            buffered.clear()

        async def replay_receive() -> Message:
            return buffered.popleft() if buffered else await receive()

        try:
            if response is None:
                await self.app(scope, replay_receive, send_with_context)
            else:
                await response(scope, receive, send_with_context)
        finally:
            logger.info(
                json.dumps(
                    {
                        "request_id": state["request_id"],
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "query_len": state["query_len"],
                        "location_code_count": state["location_code_count"],
                        "duty_code_count": state["duty_code_count"],
                        "result_count": state["result_count"],
                    },
                    separators=(",", ":"),
                )
            )


def create_app(runtime_factory: RuntimeFactory) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = runtime_factory()
        if not isinstance(engine, RetrievalRuntime):
            raise TypeError("runtime_factory must return a RetrievalRuntime")
        app.state.engine = engine
        try:
            yield
        finally:
            engine.close()

    app = FastAPI(title="1111 Work Retrieval API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error(
            request,
            422,
            "validation_error",
            "Request does not satisfy the API contract.",
            validation_details(error.errors()),
        )

    @app.exception_handler(SearchUnavailableError)
    async def unavailable(request: Request, _error_value: SearchUnavailableError) -> JSONResponse:
        return _error(
            request,
            503,
            "search_unavailable",
            "The search engine is temporarily unavailable.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "method_not_allowed"
        return _error(request, error.status_code, code, str(error.detail))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _error_value: Exception) -> JSONResponse:
        internal_logger.error(
            json.dumps(
                {"request_id": _request_id(request), "code": "internal_error"},
                separators=(",", ":"),
            )
        )
        return _error(request, 500, "internal_error", "The request could not be completed.")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> dict[str, str]:
        if not hasattr(request.app.state, "engine"):
            raise SearchUnavailableError("engine was not initialized")
        return {"status": "ready"}

    @app.post(
        SEARCH_PATH,
        response_model=SearchResponse,
        responses={
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def search(
        payload: SearchRequest, request: Request, response: Response
    ) -> SearchResponse:
        request.state.query_len = len(payload.query)
        request.state.location_code_count = len(payload.location_code)
        request.state.duty_code_count = len(payload.duty_code)
        query = SearchQuery(
            text=payload.query,
            search_date=payload.search_date,
            location_codes=tuple(payload.location_code),
            duty_codes=tuple(payload.duty_code),
        )
        engine = cast(SearchEngine, request.app.state.engine)
        raw_result = await run_in_threadpool(engine.search, query, limit=MAX_RESULTS)
        job_ids, audit_json = _validate_engine_output(raw_result)
        response.headers[SEARCH_AUDIT_HEADER] = audit_json
        request.state.result_count = len(job_ids)
        return SearchResponse(
            request_id=_request_id(request),
            result=[
                SearchResultItem(job_id=job_id, rank=rank) for rank, job_id in enumerate(job_ids, 1)
            ],
        )

    @app.get(
        "/api/v1/job-details/{job_id}",
        response_model=JobResponse,
        responses={
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def job_detail(job_id: JobId, request: Request) -> JobResponse | JSONResponse:
        runtime = cast(RetrievalRuntime, request.app.state.engine)
        details = await run_in_threadpool(runtime.job_details, job_id)
        if details is None:
            return _error(
                request,
                404,
                "job_not_found",
                "The requested job was not found.",
            )
        return JobResponse(job_id=job_id, details=details)

    return app
