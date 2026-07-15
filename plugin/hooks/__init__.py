"""team-management hooks package.

Hook modules import each other top-level at runtime (e.g. `import shared_state`),
loaded from the plugin install via the hook shim. This __init__.py makes
`plugin.hooks` importable as a package for the dev-tooling test suite.
"""
