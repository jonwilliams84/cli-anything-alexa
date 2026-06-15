"""Core modules for cli-anything-alexa.

Each module owns one Alexa API surface (devices, groups, routines,
notifications, announce, dnd). The pure-logic helpers (appliance-id
parsing, whitelist filtering, table formatting) live in `appliances.py`
and `formatting.py` and are deliberately dependency-free so they can be
unit-tested without `alexapy` or a live account.
"""
