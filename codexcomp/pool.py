"""Shared upstream httpx client lifecycle: leases, rotation, diagnostics.

One `httpx.AsyncClient` (one connection pool) serves all upstream requests.
A transient proxy failure can leave each failed handshake pinned as an ACTIVE
httpcore connection; enough of them exhaust the pool and every later request
fails with PoolTimeout until the client is replaced (#12). This module owns
the recovery:

  * every user of the client takes a LEASE (`lease_upstream_client`) and
    releases it when done — an UpstreamRounds leases its pinned client from
    construction through the complete fold, continuations included;
  * on PoolTimeout the client is rotated to a fresh generation under a lock
    (concurrent failures rotate a given generation only once); on ConnectError
    it is rotated only when no other request holds a lease on it;
  * a retired generation is never reused and is closed only when its own lease
    count reaches zero — recovery never aborts a live stream or fold;
  * the failed request itself is never replayed (no-double-generation stance).

State lives on the Starlette `app.state` (client / client_factory /
client_reset_lock / client_generation / client_leases / retired_clients /
upstream_active), wired up in `server.build_app`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("codexcomp.pool")

POOL_MAX_CONNECTIONS = 100


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=True,
        http2=False,
        limits=httpx.Limits(
            max_connections=POOL_MAX_CONNECTIONS,
            max_keepalive_connections=20,
            keepalive_expiry=5,
        ),
    )


def proxy_summary() -> str:
    """Describe proxy routing without ever logging credentials."""
    configured = []
    for key in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "all_proxy", "https_proxy", "http_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parsed = urlsplit(raw)
        configured.append(
            f"{key}={parsed.scheme}://{parsed.hostname}:{parsed.port}")
    return ",".join(configured) or "direct"


def pool_snapshot(client: httpx.AsyncClient) -> str:
    """Best-effort httpcore snapshot used only for failure diagnostics."""
    transports = [getattr(client, "_transport", None)]
    transports.extend(getattr(client, "_mounts", {}).values())
    seen: set[int] = set()
    pools = []
    for transport in transports:
        if transport is None or id(transport) in seen:
            continue
        seen.add(id(transport))
        pool = getattr(transport, "_pool", None)
        connections = list(getattr(pool, "connections", ()) or ())
        if pool is None:
            continue
        try:
            idle = sum(conn.is_idle() for conn in connections)
            available = sum(conn.is_available() for conn in connections)
        except (AttributeError, TypeError):
            pools.append(f"total={len(connections)}")
            continue
        active = len(connections) - idle
        pools.append(
            f"total={len(connections)} active={active} idle={idle} "
            f"available={available}")
    return ";".join(pools) or "unavailable"


async def recover_upstream_client(
        state: Any, failed_client: httpx.AsyncClient, context: str, reason: str,
        *, require_no_other_requests: bool = False,
        current_request_counted: bool = False) -> None:
    """Rotate a broken pool once; later retries use a clean client immediately."""
    async with state.client_reset_lock:
        if state.client is not failed_client:
            return
        client_requests = state.client_leases.get(failed_client, 0)
        other_requests = client_requests - int(current_request_counted)
        if require_no_other_requests and other_requests > 0:
            log.warning(
                "upstream client rotation deferred context=%s reason=%s "
                "other_active_requests=%d",
                context, reason, other_requests,
            )
            return
        old_snapshot = pool_snapshot(failed_client)
        state.client = state.client_factory()
        state.client_generation += 1
        log.error(
            "rotated upstream client generation=%d context=%s reason=%s "
            "active_requests=%d other_active_requests=%d old_pool=[%s] proxy=%s",
            state.client_generation, context, reason,
            state.upstream_active, other_requests, old_snapshot, proxy_summary(),
        )
        # A retired generation is never reused. Its own final lease closes it;
        # unrelated requests on newer generations do not delay that cleanup.
        state.retired_clients.add(failed_client)
        await close_unleased_retired_clients(state)


async def close_unleased_retired_clients(state: Any) -> None:
    retired = tuple(
        client for client in state.retired_clients
        if state.client_leases.get(client, 0) == 0
    )
    if not retired:
        return
    state.retired_clients.difference_update(retired)
    await asyncio.gather(*(client.aclose() for client in retired),
                         return_exceptions=True)
    log.info("closed %d drained upstream client generation(s)", len(retired))


def lease_upstream_client(state: Any, client: httpx.AsyncClient) -> None:
    state.client_leases[client] = state.client_leases.get(client, 0) + 1
    state.upstream_active += 1


async def release_upstream_client(state: Any, client: httpx.AsyncClient) -> None:
    leases = state.client_leases.get(client, 0)
    if leases <= 1:
        state.client_leases.pop(client, None)
    else:
        state.client_leases[client] = leases - 1
    state.upstream_active -= 1
    await close_unleased_retired_clients(state)
