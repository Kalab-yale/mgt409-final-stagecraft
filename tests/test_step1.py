"""Step 1 smoke test: config loads, a pydantic-ai Agent reaches the LLM, usage is reported."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `backend` imports from any cwd

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from backend import config


def main() -> None:
    print(f"Backend : {config.get_llm_backend()}")
    print(f"Model   : {config.MODEL_NAME}")

    agent = Agent(
        config.get_pydantic_ai_model(),
        instructions="You are a connectivity check. Follow the user's instruction exactly.",
        model_settings=ModelSettings(max_tokens=50),
    )
    result = agent.run_sync("Reply with: STAGECRAFT_READY")

    print(f"Reply   : {result.output}")
    # pydantic-ai 1.x exposes usage() as a method; 2.x makes it a property.
    usage = result.usage() if callable(result.usage) else result.usage
    print(f"Usage   : {usage}")
    assert "STAGECRAFT_READY" in result.output, "Unexpected reply from the model"
    print("PASS - Step 1 verified.")


if __name__ == "__main__":
    main()
