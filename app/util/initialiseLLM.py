from langchain.chat_models import init_chat_model
import os

import os
from dotenv import load_dotenv, find_dotenv

# ---------- Env / defaults ----------

load_dotenv(find_dotenv(), override=True)

def initialize_llm(context=None):
    chosen_model = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required for real LLM calls.")

    llm = init_chat_model(
        chosen_model,
        model_provider="openai",
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0
    )

    return llm