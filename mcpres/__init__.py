"""mcp-resource-heavy: a from-scratch, stdlib-only Model Context Protocol server.

No third-party imports, no MCP SDK. See ORIGINALITY.md and AUDIT.md.
"""

__version__ = "1.0.0"

# Protocol revisions this server will negotiate. The first entry is the
# server's preferred ("latest it speaks natively") revision.
PREFERRED_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05", "2025-06-18")
