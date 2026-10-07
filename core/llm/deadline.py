"""Own cancellable deadline requests on one loop, without changing ordinary synchronous calls."""

import asyncio
import threading
import time
from contextvars import ContextVar

import httpx

_ACTIVE = ContextVar("model_deadline_call", default=None)


class DeadlineExpired(TimeoutError):
    def __init__(self, possibly_dispatched=False, auth_unresolved=False, cleanup_failed=False):
        super().__init__("model timeout followed by request cleanup failure" if cleanup_failed
                         else "model dispatch deadline expired")
        self.possibly_dispatched = possibly_dispatched
        self.auth_unresolved = auth_unresolved
        self.reserve_model_estimate = possibly_dispatched
        self.known_pre_dispatch = not possibly_dispatched
        self.request_cleanup_failed = cleanup_failed


def _remaining(state):
    return min(state["expires"] - time.monotonic(), state["allowance"]())


async def dispatch_guard(request):
    state = _ACTIVE.get()
    if state is None:
        raise RuntimeError("deadline request has no active owner")
    if state["owner"].retired:
        raise RuntimeError("deadline owner retired after unresolved authentication")
    remaining = _remaining(state)
    if remaining < 0.001:
        raise DeadlineExpired(state["dispatched"])
    limits = request.extensions.get("timeout", {})
    request.extensions["timeout"] = {
        phase: min(limit, remaining) if limit is not None else remaining for phase, limit in limits.items()
    }
    state["dispatched"] = True


class DeadlineRunner:
    def __init__(self, open_client):
        self.open_client = open_client
        self.retired = False
        self._lock = threading.Lock()
        self._loop = self._thread = self._sdk = self._http = None
        self._futures = set()
        self._closing = False

    def _serve(self, ready):
        asyncio.set_event_loop(self._loop)
        ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def generate(self, request, expires, allowance):
        with self._lock:
            if self._closing or self.retired:
                raise RuntimeError("deadline owner retired or closing")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                ready = threading.Event()
                self._thread = threading.Thread(target=self._serve, args=(ready,), name="model-deadline")
                try:
                    self._thread.start()
                except Exception:
                    self.retired = True
                    self._loop.close()
                    self._loop = None
                    raise
                ready.wait()
            future = asyncio.run_coroutine_threadsafe(self._generate(request, expires, allowance), self._loop)
            self._futures.add(future)
        try:
            return future.result()
        finally:
            with self._lock:
                self._futures.discard(future)

    async def _generate(self, request, expires, allowance):
        state = {"expires": expires, "allowance": allowance, "dispatched": False, "owner": self}
        token = _ACTIVE.set(state)
        sdk_started = False
        deadline = asyncio.timeout(max(0, expires - time.monotonic()))
        try:
            async with deadline:
                if _remaining(state) < 0.001:
                    raise DeadlineExpired()
                if self._sdk is None:
                    self._sdk, self._http = await self.open_client(dispatch_guard)
                if _remaining(state) < 0.001:
                    raise DeadlineExpired()
                if self.retired:
                    raise RuntimeError("deadline owner retired after unresolved authentication")
                sdk_started = True
                return await self._sdk.aio.models.generate_content(**request)
        except DeadlineExpired:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            unresolved = sdk_started and not state["dispatched"]
            if unresolved:
                self.retired = True
            raise DeadlineExpired(state["dispatched"], unresolved) from exc
        except Exception as exc:
            masked_timeout = deadline.expired()
            pending, seen = [exc.__cause__, exc.__context__], set()
            while pending:
                cause = pending.pop()
                if cause is None or id(cause) in seen:
                    continue
                seen.add(id(cause))
                if isinstance(cause, (TimeoutError, httpx.TimeoutException, asyncio.CancelledError)):
                    masked_timeout = True
                    break
                pending.extend((cause.__cause__, cause.__context__))
            if masked_timeout:
                self.retired = True
                raise DeadlineExpired(state["dispatched"], sdk_started and not state["dispatched"], True) from exc
            if self._sdk is None:
                self.retired = True
            raise
        finally:
            _ACTIVE.reset(token)

    async def _shutdown(self):
        try:
            try:
                if self._sdk is not None:
                    await self._sdk.aio.aclose()
            finally:
                try:
                    if self._http is not None:
                        await self._http.aclose()
                finally:
                    if self._sdk is not None:
                        self._sdk.close()
        finally:
            await self._loop.shutdown_asyncgens()
            await self._loop.shutdown_default_executor()

    def close(self):
        with self._lock:
            self._closing = True
            futures = list(self._futures)
        for future in futures:
            try:
                future.result()
            except Exception:
                pass
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result()
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
