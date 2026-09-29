import pytest
from pydantic import ValidationError

from agentdrive.api.model_base import (
    ForwardCompatibleResponseModel,
    StrictRequestModel,
)


class ExampleStrictRequest(StrictRequestModel):
    name: str


class ExampleForwardCompatibleResponse(ForwardCompatibleResponseModel):
    id: str


def test_strict_request_model_rejects_unknown_fields():
    with pytest.raises(ValidationError) as exc_info:
        ExampleStrictRequest.model_validate(
            {"name": "report", "future_option": True}
        )

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
    assert exc_info.value.errors()[0]["loc"] == ("future_option",)


def test_strict_request_model_marks_schema_as_closed():
    schema = ExampleStrictRequest.model_json_schema()

    assert schema["additionalProperties"] is False


def test_forward_compatible_response_model_preserves_additive_fields():
    response = ExampleForwardCompatibleResponse.model_validate(
        {"id": "art_0123456789abcdef", "future_status": "indexed"}
    )

    assert response.future_status == "indexed"
    assert response.model_extra == {"future_status": "indexed"}
    assert response.model_dump() == {
        "id": "art_0123456789abcdef",
        "future_status": "indexed",
    }


def test_forward_compatible_response_model_marks_schema_as_open():
    schema = ExampleForwardCompatibleResponse.model_json_schema()

    assert schema["additionalProperties"] is True
