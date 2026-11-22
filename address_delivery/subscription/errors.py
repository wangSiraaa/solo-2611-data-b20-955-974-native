"""Exceptions raised by the subscription ledger and scheduler."""
from __future__ import annotations


class SubscriptionError(RuntimeError):
    """Base class for subscription-ledger invariant violations."""


class AnchorError(SubscriptionError):
    """A subscription root cannot be anchored (no successful parcel, etc.)."""


class SplitError(SubscriptionError):
    """A split batch is structurally or algebraically invalid."""


class CalendarError(SubscriptionError):
    """A box calendar / closure registration is inconsistent."""


class SchedulingError(SubscriptionError):
    """An issue cannot be scheduled (e.g. nobody eligible to receive)."""
