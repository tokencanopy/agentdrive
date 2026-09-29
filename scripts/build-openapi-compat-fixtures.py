"""Build deliberate compatibility-policy mutations from one canonical fixture."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path


def main() -> None:
    base_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    base = json.loads(base_path.read_text(encoding="utf-8"))

    def write(name: str, mutate: Callable[[dict], None]) -> None:
        document = deepcopy(base)
        mutate(document)
        (output_dir / name).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def request_field_removed(document: dict) -> None:
        schema = document["components"]["schemas"]["WidgetIn"]
        schema["properties"].pop("name")
        schema["required"].remove("name")

    write("request-field-removed.json", request_field_removed)
    write(
        "response-type-changed.json",
        lambda document: document["components"]["schemas"]["WidgetOut"][
            "properties"
        ]["id"].update(type="integer"),
    )
    write(
        "response-nullability-changed.json",
        lambda document: document["components"]["schemas"]["WidgetOut"][
            "properties"
        ]["id"].update(type=["string", "null"]),
    )
    write(
        "operation-id-renamed.json",
        lambda document: document["paths"]["/stable/widgets"]["post"].update(
            operationId="submitWidget"
        ),
    )
    write(
        "operation-tag-changed.json",
        lambda document: document["paths"]["/stable/widgets"]["post"].update(
            tags=["objects"]
        ),
    )

    def schema_renamed(document: dict) -> None:
        schemas = document["components"]["schemas"]
        schemas["RenamedWidgetOut"] = schemas.pop("WidgetOut")
        response_schema = document["paths"]["/stable/widgets"]["post"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        response_schema["$ref"] = "#/components/schemas/RenamedWidgetOut"

    write("schema-renamed.json", schema_renamed)

    def schema_retargeted_with_alias(document: dict) -> None:
        schemas = document["components"]["schemas"]
        schemas["RenamedWidgetOut"] = deepcopy(schemas["WidgetOut"])
        response_schema = document["paths"]["/stable/widgets"]["post"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        response_schema["$ref"] = "#/components/schemas/RenamedWidgetOut"

    write("schema-retargeted-with-alias.json", schema_retargeted_with_alias)
    write(
        "security-changed.json",
        lambda document: document["components"]["securitySchemes"][
            "BearerAuth"
        ].update(scheme="basic"),
    )
    write(
        "stability-decreased.json",
        lambda document: document["paths"]["/stable/widgets"]["post"].update(
            {"x-stability-level": "beta"}
        ),
    )


if __name__ == "__main__":
    main()
