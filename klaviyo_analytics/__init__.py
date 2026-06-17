"""Klaviyo reporting service shared core package.

The single owner of all Klaviyo REST interaction. Both the MCP server
(``server.py``) and the Flask REST API (``api/``) are thin adapters over this
package, so the two transports return identical data by construction (AC-2).
See the WP-0 implementation plan for the authoritative design.
"""
