"""Executor-specific transport adapters (T051 GPT relay; T052 BYOK later).

Each adapter packages transport around the transport-neutral
``SemanticWorkRequest``/``SemanticWorkResult`` control plane and the
``EvidenceAccess`` boundary. No adapter here performs T003 workflow
mutation, compiler execution, or holds a provider SDK/API key.
"""

from __future__ import annotations
