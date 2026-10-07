"""CommAI AI agents (ADR 0019): one runtime, three role profiles.

- model.py      the model interface: an OpenAI-compatible model (DeepInfra) and
                a deterministic simulated model used when no key is set
- runtime.py    role profiles, the business's AI profile, tools, usage limits
- knowledge.py  approved knowledge with sources, retrieval, knowledge gaps
- agent.py      the customer AI agent (job "ai.respond")
- copilot.py    the employee copilot (a person sends)
- memory.py     customer memory for verified identities
- language.py   language detection and the honest no-translation fallback
- voice.py      browser calls with the AI agent (voice stage 1)
"""

from . import (  # noqa: F401  # noqa: F401
    agent,
    attachments,
    copilot,
    followups,
    gaps,
    governance,
    judging,
    knowledge,
    language,
    memory,
    model,
    quality,
    runtime,
    voice,
)
from .model import Model, ModelError, ModelInput, ModelOutput, OpenAICompatibleModel, SimulatedModel  # noqa: F401
from .runtime import ROLES, UsageLimit, get_model, set_model  # noqa: F401
