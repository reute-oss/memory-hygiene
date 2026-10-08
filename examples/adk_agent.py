# Disclaimer: This code is provided "as-is" as a demonstration only to illustrate a potential solution. The code does not constitute a Google product or service of any kind, and Google offers no support, warranties, or liability of any kind with its regard. Whoever chooses to use this code accepts all responsibility related to it, including for its implementation, use, and ongoing maintenance. For the avoidance of doubt, this code is not eligible for the Google Open Source Software Vulnerability Rewards Program.

# Requirements and status, please read before running anything here:
#   This example needs google-adk, the optional "memorybank" extra
#   (google-cloud-agentplatform, Python 3.10 or newer) and real Google Cloud
#   credentials with access to a Memory Bank.
#   This file has NOT been executed in the environment where it was written.
#   There was no SDK and no network there, so nothing below has been run even
#   once. Treat it as wiring to copy, not as a verified sample.

"""Wire an ADK agent to Memory Bank, and audit the same bank with memory_hygiene.

Two separate paths touch the same Memory Bank:

    1. The agent path. ADK reads and writes memories for you through
       ``VertexAiMemoryBankService``. ``PreloadMemoryTool`` pulls the relevant
       memories into context before the model runs.
    2. The hygiene path. ``MemoryBankStore`` reads the same bank as a
       plain ``MemoryStore``, so the audit pass can score duplicates,
       contradictions and TTL overdue records in it.

Run it with:
    python3 adk_agent.py
after setting the three environment variables below.
"""

import os

# ADK imports. These live at module top level on purpose, this file is an
# example and is expected to fail loudly if the extra is not installed. The
# adapter itself does the opposite and imports the SDK lazily.
import google.adk as adk
from google.adk.memory import VertexAiMemoryBankService
from google.adk.tools import PreloadMemoryTool

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
AGENT_ENGINE_ID = os.environ["AGENT_ENGINE_ID"]

# 1. The memory service ADK talks to. The Agent Engine must already exist and
#    must have a Memory Bank, this call does not create one.
memory_service = VertexAiMemoryBankService(
    project=PROJECT,
    location=LOCATION,
    agent_engine_id=AGENT_ENGINE_ID,
)

# 2. The agent. PreloadMemoryTool runs before the model and injects memories
#    that match the current scope and query, so the instruction does not have
#    to describe how retrieval works.
root_agent = adk.Agent(
    model="gemini-2.5-flash",
    name="memory_demo_agent",
    instruction=(
        "You are a support assistant. Use what you already know about the user "
        "from memory instead of asking again. If a remembered fact contradicts "
        "what the user just said, trust the user and say that you updated it."
    ),
    tools=[PreloadMemoryTool()],
)

# 3. Ingestion after each turn. Memory extraction is not automatic, something
#    has to hand the finished session to the bank. In an after agent callback,
#    call:
#
#        await callback_context.add_session_to_memory()
#
#    That is the post turn hook: it sends the session to Memory Bank, which
#    extracts and consolidates facts into the scope for that user. Without it
#    the bank stays empty and PreloadMemoryTool has nothing to preload.
#
#    Pass the callback to the agent with
#    adk.Agent(..., after_agent_callback=remember_this_turn).


async def remember_this_turn(callback_context):
    """After agent callback that pushes the finished session into Memory Bank.

    Args:
        callback_context: The ADK callback context for the turn that just
            finished.
    """
    await callback_context.add_session_to_memory()


# 4. The hygiene pass over the same bank. This part uses the stdlib only
#    adapter from this repository, not ADK. Note again the honest status: the
#    adapter is unit tested against a fake transport and has not been run
#    against a live Memory Bank.
def audit_the_bank(user_id: str) -> None:
    """Score the memory health of one user's scope and print the report.

    Args:
        user_id: The scope key value, must match what the agent writes under.
    """
    from datetime import datetime

    from memory_hygiene_audit import AuditConfig, audit_store
    from memory_hygiene_memorybank import MemoryBankStore

    store = MemoryBankStore(
        project=PROJECT,
        location=LOCATION,
        memory_bank=AGENT_ENGINE_ID,
        scope={"user_id": user_id},
    )
    report = audit_store(store, AuditConfig(now=datetime.utcnow()))
    print(report.render())
    # Expect the orphan component to report zero on this backend. Memory Bank
    # exposes no retrieval counter and no last accessed timestamp, so the
    # adapter reports those as None rather than inventing them, and orphan
    # detection has nothing to work with. Everything else in the report is
    # computed from real fields.


if __name__ == "__main__":
    print("Agent %r is wired to Memory Bank on engine %s." % (root_agent.name, AGENT_ENGINE_ID))
    print("Serve it with: adk web   (or adk run), then audit with audit_the_bank(<user id>).")
