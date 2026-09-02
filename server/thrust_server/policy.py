"""The control policy: given the current game state, decide what the rocket does.

``default_policy`` is the hook to replace with a real algorithm. It receives one
``State`` per physics tick (60 Hz, paced by the client so at most one request is
in flight) and returns the ``Move`` the client applies until the next reply.
"""

from thrust_server.models import Move, State


def default_policy(state: State) -> Move:  # noqa: ARG001 - the real policy will use it
    """Do nothing. Replace me."""
    return Move()
