"""
Claude model ids, in one place. Model ids retire (that has caused 404s here
before), so override them in .env rather than editing code:

  CLAUDE_MODEL       main model (invoice OCR, Jarvis)
  CLAUDE_FAST_MODEL  small, fast model for person-triggered lookups
                     (the transfer/waste product fallback)
"""
import os

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_FAST_MODEL = os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-5-5")
