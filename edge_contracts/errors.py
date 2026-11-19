"""Stable error identity and normalization for provider and kernel boundaries."""
import re


CODE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}")


class Unavailable(Exception):
    """Transport or provider is temporarily unavailable."""


class Refused(Exception):
    """Unfulfillable task or terminal adapter failure."""


class OutcomeUnknown(Exception):
    """A dispatched operation lacks a trustworthy terminal receipt."""


def failure(cls, message, code, origin, details=None):
    exc=cls(str(message))
    marker=re.search(r"\[code:([a-zA-Z][\w.-]{0,95})\]\s*$",str(message))
    exc.code=marker.group(1) if marker else code
    exc.origin=origin
    exc.details=details
    return exc


def remote_failure(cls, reply, fallback):
    error=reply.get("error",{})
    if not isinstance(error,dict):
        error={}
    code=error.get("code")
    code=code if isinstance(code,str) and CODE.fullmatch(code) else fallback
    return failure(cls,error.get("message") or reply.get("detail") or fallback,code,"provider",error.get("details"))


class OperationCanceled(Exception):
    """The executor confirmed that its operation was canceled."""
