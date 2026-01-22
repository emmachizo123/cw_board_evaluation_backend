"""
app.api.debug
-------------
Debug endpoints to verify routing and environment variables during development.
"""

import os
from typing import Any, Dict

from fastapi import APIRouter, status

router = APIRouter()


@router.get("/debug/env", status_code=status.HTTP_200_OK)
async def debug_env() -> Dict[str, Any]:
    """
    Confirm what environment variables the running server process sees.
    """
    return {
        "DEBUG_API_ERRORS": os.getenv("DEBUG_API_ERRORS"),
        "LLM_MODEL": os.getenv("LLM_MODEL"),
        "OPENAI_API_KEY_set": bool(os.getenv("OPENAI_API_KEY")),
    }


@router.get("/debug/ping", status_code=status.HTTP_200_OK)
async def ping() -> Dict[str, str]:
    """
    Simple route to confirm router inclusion.
    """
    return {"ping": "pong"}
