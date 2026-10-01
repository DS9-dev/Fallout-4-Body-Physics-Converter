"""Fallout 4 Body & Physics Converter (internal package name ``cbbe3bbb``, kept for compatibility).

Product name 0.6.0+: "Fallout 4 Body & Physics Converter". Internal identities stay unchanged on purpose: the Python
package ``cbbe3bbb``, the folder ``CBBE3BBBConverter``, ``run_batch.bat``, the manifest ``tool`` id
"CBBE3BBBConverter", the output mod "CBBE 3BBB Auto Conversions" and its BodySlide groups.
"""

__version__ = "0.6.1"  # the ONE version source (pyproject.toml reads it: dynamic version)
PRODUCT_NAME = "Fallout 4 Body & Physics Converter"
MILESTONE = "Body Architecture Foundation (0.6.0); stabilization (0.6.1)"
APP_NAME = PRODUCT_NAME  # user-facing label (was "CBBE 3BBB Outfit Converter")
TOOL_ID = "CBBE3BBBConverter"  # serialised identity (manifests, reports, settings): never renamed
PHASE = 2


def product_title(suffix: str = "") -> str:
    """The window title: product + version, once (0.6.1: no Qt application display name is appended)."""
    return f"{PRODUCT_NAME} {__version__}" + (f" — {suffix}" if suffix else "")
