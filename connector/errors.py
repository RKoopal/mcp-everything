"""Error kinds that every connector shares."""
from __future__ import annotations


class ConnectorError(RuntimeError):
    pass


class AuthExpired(ConnectorError):
    """The session expired and could not be renewed by itself."""


class StateUnknown(ConnectorError):
    """A write got no clear answer: it may have been done."""


class Unverified(ConnectorError):
    """The site said it worked, but the verifier does not see the result."""
