"""Reusable Pydantic policies for public request and response models.

Adoption is deliberately opt-in: changing an existing request model to inherit
from :class:`StrictRequestModel` can reject payloads that are accepted today
and therefore requires a compatibility review.
"""

from pydantic import BaseModel, ConfigDict


class StrictRequestModel(BaseModel):
    """Base for request bodies whose documented fields are the complete input.

    Unknown fields raise a Pydantic ``extra_forbidden`` validation error and
    appear as ``additionalProperties: false`` in generated JSON Schema.
    Existing public request models must migrate to this base deliberately
    because switching from Pydantic's default ignore behavior is observable.
    """

    model_config = ConfigDict(extra="forbid")


class ForwardCompatibleResponseModel(BaseModel):
    """Base for response bodies that may gain additive fields over time.

    Unknown fields are retained in ``model_extra`` and included by
    ``model_dump``. This lets response validation preserve additive server
    fields instead of silently dropping them, and advertises an open object in
    generated JSON Schema.
    """

    model_config = ConfigDict(extra="allow")
