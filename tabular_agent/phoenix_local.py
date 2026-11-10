"""Launch Phoenix with local HTTP only; its default gRPC listener binds all interfaces."""
import os
import runpy

# Ignore remote database and collector settings for this local-only server.
for key in tuple(os.environ):
    if key.startswith('PHOENIX_POSTGRES_') or key in {
        'PHOENIX_SQL_DATABASE_URL', 'PHOENIX_SQL_DATABASE_READ_REPLICA_URL',
        'PHOENIX_SERVER_INSTRUMENTATION_OTLP_TRACE_COLLECTOR_HTTP_ENDPOINT',
        'PHOENIX_SERVER_INSTRUMENTATION_OTLP_TRACE_COLLECTOR_GRPC_ENDPOINT',
    }:
        os.environ.pop(key, None)
os.environ['PHOENIX_HOST'] = '127.0.0.1'
os.environ['PHOENIX_PORT'] = '6006'

from phoenix.server.grpc_server import GrpcServer

# Phoenix 14.6.0 does not expose a host setting for its gRPC listener. The agent
# exports over HTTP, so disable that listener rather than exposing it on the LAN.
_original_enter = GrpcServer.__aenter__


async def _http_only(self):
    """Enter Phoenix's server context with its gRPC listener disabled.

    Args:
        self: GrpcServer instance whose _disabled flag is set before entry.

    Returns:
        The awaited result of the original GrpcServer.__aenter__ method.
    """
    self._disabled = True
    return await _original_enter(self)


def main():
    """Start Phoenix using process arguments and this module's local configuration.

    Returns:
        None if Phoenix exits normally. Patches GrpcServer.__aenter__ for this
        process and runs Phoenix's main module; HTTP uses 127.0.0.1:6006 and
        the gRPC listener is disabled. Server errors or SystemExit propagate.
    """
    GrpcServer.__aenter__ = _http_only
    print('Phoenix local mode: HTTP on 127.0.0.1:6006; gRPC disabled.', flush=True)
    runpy.run_module('phoenix.server.main', run_name='__main__')


if __name__ == '__main__':
    main()
