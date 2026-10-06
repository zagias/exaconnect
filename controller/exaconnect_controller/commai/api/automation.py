"""CommAI automation API. Placeholder until the module lands."""

from fastapi import APIRouter

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: automation"])
