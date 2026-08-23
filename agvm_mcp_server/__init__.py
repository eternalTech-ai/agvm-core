# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Local MCP stdio server package for AGVM."""

from .server import AgvmMcpConfig, AgvmMcpServer, load_config, main

__all__ = ["AgvmMcpConfig", "AgvmMcpServer", "load_config", "main"]
