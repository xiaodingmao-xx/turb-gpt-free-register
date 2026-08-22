# -*- coding: utf-8 -*-
"""Offline OAICS/Stripe Checkout and payment-method detector.

This module only parses already-returned JSON objects. It never performs HTTP
requests, creates a checkout session, or submits a payment method.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

SUPPORTED_SESSION_PREFIXES = ("cs_", "oaics_")


@dataclass(frozen=True)
class CheckoutSessionInfo:
    checkout_session_id: str
    session_kind: str
    processor_entity: str
    publishable_key: str


@dataclass(frozen=True)
class CapabilityEvidence:
    amount_minor: int | None
    currency: str
    payment_method_types: tuple[str, ...]
    ordered_payment_method_types: tuple[str, ...]
    custom_payment_methods: tuple[str, ...]
    offer_state: str


def normalize_payment_method_token(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "kakao": "kakao_pay",
        "card_payment": "card",
        "direct_card": "card",
        "external_gcash": "gcash",
        "external_momo": "momo",
        "go_pay": "gopay",
        "grab_pay": "grabpay",
    }
    return aliases.get(token, token)


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _values_for_key(value: Any, target: str, *, depth: int = 0) -> Iterable[Any]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == target.lower():
                yield item
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)


def _walk_dicts(value: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 10:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _walk_dicts(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _walk_dicts(item, depth=depth + 1)


def _collect_method_group(payload: Any, key: str) -> list[str]:
    values: list[str] = []
    for group in _values_for_key(payload, key):
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                token = normalize_payment_method_token(item)
                if token:
                    values.append(token)
            elif isinstance(item, dict):
                for candidate_key in ("type", "payment_method_type", "name", "id"):
                    token = normalize_payment_method_token(item.get(candidate_key))
                    if token:
                        values.append(token)
                        break
    return _dedupe(values)


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    if isinstance(value, dict):
        for key in ("amount", "value", "unit_amount", "amount_minor"):
            if key in value:
                parsed = _minor_units(value[key])
                if parsed is not None:
                    return parsed
    return None


def _extract_amount_minor(payload: dict[str, Any]) -> int | None:
    paths = (
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("elements_options", "amount"),
        ("payment_intent", "amount"),
        ("amount_due",),
    )
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        parsed = _minor_units(value)
        if parsed is not None:
            return parsed
    return None


def _extract_currency(payload: Any) -> str:
    for key in ("currency", "currency_code"):
        for value in _values_for_key(payload, key):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
    return ""


def parse_checkout_session(
    payload: Any,
    *,
    billing_country: str,
    fallback_publishable_key: str = "",
) -> CheckoutSessionInfo:
    if not isinstance(payload, dict):
        raise ValueError("checkout response must be a JSON object")
    session_id = str(
        payload.get("checkout_session_id")
        or payload.get("session_id")
        or payload.get("id")
        or ""
    ).strip()
    if not session_id.startswith(SUPPORTED_SESSION_PREFIXES):
        raise ValueError("checkout response did not contain a supported cs_/oaics_ session id")
    processor = str(payload.get("processor_entity") or "").strip() or (
        "openai_llc" if str(billing_country or "").upper() == "US" else "openai_ie"
    )
    publishable_key = str(
        payload.get("publishable_key") or fallback_publishable_key or ""
    ).strip()
    kind = "oaics" if session_id.startswith("oaics_") else "stripe_cs"
    return CheckoutSessionInfo(session_id, kind, processor, publishable_key)


def parse_capability_evidence(
    stripe_init_payload: Any,
    *,
    fallback_currency: str = "",
) -> CapabilityEvidence:
    if not isinstance(stripe_init_payload, dict):
        raise ValueError("Stripe init response must be a JSON object")
    standard = _collect_method_group(stripe_init_payload, "payment_method_types")
    ordered = _collect_method_group(stripe_init_payload, "ordered_payment_method_types")
    custom = _collect_method_group(stripe_init_payload, "custom_payment_methods")
    methods = tuple(_dedupe((*standard, *ordered, *custom)))
    amount = _extract_amount_minor(stripe_init_payload)
    currency = _extract_currency(stripe_init_payload) or str(fallback_currency or "").upper()
    offer_state = "zero_due" if amount == 0 else "nonzero_due" if amount is not None else "unknown_amount"
    return CapabilityEvidence(amount, currency, methods, tuple(ordered), tuple(custom), offer_state)


def classify_payment_method(
    evidence: CapabilityEvidence,
    expected_method: str,
) -> tuple[str, bool | None]:
    expected = normalize_payment_method_token(expected_method)
    if not expected:
        return "unknown", None
    if expected in evidence.payment_method_types:
        return "available", True
    if evidence.payment_method_types:
        return "unavailable", False
    return "unknown", None


def detect_oaics(
    checkout_payload: Any,
    stripe_init_payload: Any | None = None,
    *,
    billing_country: str,
    fallback_currency: str = "",
    expected_method: str = "paypal",
) -> dict[str, Any]:
    checkout = parse_checkout_session(checkout_payload, billing_country=billing_country)
    result: dict[str, Any] = {
        "detected": True,
        "checkout_session_id": checkout.checkout_session_id,
        "session_kind": checkout.session_kind,
        "is_oaics": checkout.session_kind == "oaics",
        "processor_entity": checkout.processor_entity,
        "stripe_init_present": stripe_init_payload is not None,
        "expected_method": normalize_payment_method_token(expected_method),
        "method_status": "unknown",
        "method_available": None,
    }
    if stripe_init_payload is None:
        return result
    evidence = parse_capability_evidence(stripe_init_payload, fallback_currency=fallback_currency)
    status, available = classify_payment_method(evidence, expected_method)
    result.update(
        {
            "currency": evidence.currency,
            "amount_minor": evidence.amount_minor,
            "offer_state": evidence.offer_state,
            "payment_method_types": list(evidence.payment_method_types),
            "ordered_payment_method_types": list(evidence.ordered_payment_method_types),
            "custom_payment_methods": list(evidence.custom_payment_methods),
            "method_status": status,
            "method_available": available,
        }
    )
    return result


def _empty_detection(expected_method: str = "") -> dict[str, Any]:
    return {
        "detected": False,
        "checkout_session_id": None,
        "session_kind": "unknown",
        "is_oaics": False,
        "processor_entity": None,
        "stripe_init_present": False,
        "expected_method": normalize_payment_method_token(expected_method),
        "method_status": "unknown",
        "method_available": None,
    }


def _find_checkout_payload(payload: Any, *, billing_country: str) -> dict[str, Any] | None:
    for candidate in _walk_dicts(payload):
        try:
            parse_checkout_session(candidate, billing_country=billing_country)
        except (TypeError, ValueError):
            continue
        return candidate
    return None


def _find_stripe_init_payload(payload: Any) -> dict[str, Any] | None:
    capability_keys = {
        "payment_method_types",
        "ordered_payment_method_types",
        "custom_payment_methods",
        "elements_options",
        "payment_intent",
        "total_summary",
        "invoice",
    }
    for candidate in _walk_dicts(payload):
        if any(key in candidate for key in capability_keys):
            return candidate
    return None


def detect_extract_payment_session(
    extract_payload: Any,
    *,
    billing_country: str = "",
    fallback_currency: str = "",
    expected_method: str = "",
) -> dict[str, Any]:
    """Detect a supported checkout session in an already-returned extract payload."""
    if not expected_method and isinstance(extract_payload, dict):
        expected_method = str(
            extract_payload.get("payment_method")
            or extract_payload.get("payment_link_type")
            or ""
        )
    checkout_payload = _find_checkout_payload(extract_payload, billing_country=billing_country)
    if checkout_payload is None:
        return _empty_detection(expected_method)
    stripe_payload = _find_stripe_init_payload(extract_payload)
    try:
        return detect_oaics(
            checkout_payload,
            stripe_payload,
            billing_country=billing_country,
            fallback_currency=fallback_currency,
            expected_method=expected_method,
        )
    except (TypeError, ValueError):
        return _empty_detection(expected_method)
