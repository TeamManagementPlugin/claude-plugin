"""
team-management framework
Enforced methodology for AI pair programming
"""

from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PkgNotFound

try:
    __version__ = _pkg_version("team-management")
except _PkgNotFound:  # running from a source tree without an installed dist
    __version__ = "0.0.0+source"
__author__ = "toast"