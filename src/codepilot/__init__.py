# src/codepilot/__init__.py
"""CodePilot+: turns GitHub issues into approved pull requests."""
import os

# A3's guardrail package loads a .env at import unless told not to; CodePilot+
# manages its own configuration and must never read a file it did not choose.
os.environ.setdefault("GEP_LOAD_DOTENV", "0")
