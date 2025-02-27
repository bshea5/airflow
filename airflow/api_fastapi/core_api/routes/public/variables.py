# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select

from airflow.api_fastapi.common.db.common import SessionDep, paginated_select
from airflow.api_fastapi.common.parameters import (
    QueryLimit,
    QueryOffset,
    QueryVariableKeyPatternSearch,
    SortParam,
)
from airflow.api_fastapi.common.router import AirflowRouter
from airflow.api_fastapi.core_api.datamodels.variables import (
    VariableBody,
    VariableBulkActionResponse,
    VariableBulkBody,
    VariableBulkResponse,
    VariableCollectionResponse,
    VariableResponse,
    VariablesImportResponse,
)
from airflow.api_fastapi.core_api.openapi.exceptions import create_openapi_http_exception_doc
from airflow.api_fastapi.core_api.services.public.variables import (
    handle_bulk_create,
    handle_bulk_delete,
    handle_bulk_update,
)
from airflow.models.variable import Variable

variables_router = AirflowRouter(tags=["Variable"], prefix="/variables")


@variables_router.delete(
    "/{variable_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=create_openapi_http_exception_doc([status.HTTP_404_NOT_FOUND]),
)
def delete_variable(
    variable_key: str,
    session: SessionDep,
):
    """Delete a variable entry."""
    if Variable.delete(variable_key, session) == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"The Variable with key: `{variable_key}` was not found"
        )


@variables_router.get(
    "/{variable_key}",
    responses=create_openapi_http_exception_doc([status.HTTP_404_NOT_FOUND]),
)
def get_variable(
    variable_key: str,
    session: SessionDep,
) -> VariableResponse:
    """Get a variable entry."""
    variable = session.scalar(select(Variable).where(Variable.key == variable_key).limit(1))

    if variable is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"The Variable with key: `{variable_key}` was not found"
        )

    return variable


@variables_router.get(
    "",
)
def get_variables(
    limit: QueryLimit,
    offset: QueryOffset,
    order_by: Annotated[
        SortParam,
        Depends(
            SortParam(
                ["key", "id", "_val", "description", "is_encrypted"],
                Variable,
            ).dynamic_depends()
        ),
    ],
    session: SessionDep,
    varaible_key_pattern: QueryVariableKeyPatternSearch,
) -> VariableCollectionResponse:
    """Get all Variables entries."""
    variable_select, total_entries = paginated_select(
        statement=select(Variable),
        filters=[varaible_key_pattern],
        order_by=order_by,
        offset=offset,
        limit=limit,
        session=session,
    )

    variables = session.scalars(variable_select)

    return VariableCollectionResponse(
        variables=variables,
        total_entries=total_entries,
    )


@variables_router.patch(
    "/{variable_key}",
    responses=create_openapi_http_exception_doc(
        [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_404_NOT_FOUND,
        ]
    ),
)
def patch_variable(
    variable_key: str,
    patch_body: VariableBody,
    session: SessionDep,
    update_mask: list[str] | None = Query(None),
) -> VariableResponse:
    """Update a variable by key."""
    if patch_body.key != variable_key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid body, key from request body doesn't match uri parameter"
        )
    non_update_fields = {"key"}
    variable = session.scalar(select(Variable).filter_by(key=variable_key).limit(1))
    if not variable:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"The Variable with key: `{variable_key}` was not found"
        )

    fields_to_update = patch_body.model_fields_set
    if update_mask:
        fields_to_update = fields_to_update.intersection(update_mask)
        data = patch_body.model_dump(include=fields_to_update - non_update_fields, by_alias=True)
    else:
        try:
            VariableBody(**patch_body.model_dump())
        except ValidationError as e:
            raise RequestValidationError(errors=e.errors())
        data = patch_body.model_dump(exclude=non_update_fields, by_alias=True)

    for key, val in data.items():
        setattr(variable, key, val)

    return variable


@variables_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=create_openapi_http_exception_doc([status.HTTP_409_CONFLICT]),
)
def post_variable(
    post_body: VariableBody,
    session: SessionDep,
) -> VariableResponse:
    """Create a variable."""
    # Check if the key already exists
    existing_variable = session.scalar(select(Variable).where(Variable.key == post_body.key).limit(1))
    if existing_variable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The Variable with key: `{post_body.key}` already exists",
        )

    Variable.set(**post_body.model_dump(), session=session)

    variable = session.scalar(select(Variable).where(Variable.key == post_body.key).limit(1))

    return variable


@variables_router.post(
    "/import",
    status_code=status.HTTP_200_OK,
    responses=create_openapi_http_exception_doc(
        [status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_ENTITY]
    ),
)
def import_variables(
    file: UploadFile,
    session: SessionDep,
    action_if_exists: Literal["overwrite", "fail", "skip"] = "fail",
) -> VariablesImportResponse:
    """Import variables from a JSON file."""
    try:
        file_content = file.file.read().decode("utf-8")
        variables = json.loads(file_content)

        if not isinstance(variables, dict):
            raise ValueError("Uploaded JSON must contain key-value pairs.")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON format: {e}")

    if not variables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No variables found in the provided JSON.",
        )

    existing_keys = {variable for variable in session.execute(select(Variable.key)).scalars()}
    import_keys = set(variables.keys())

    matched_keys = existing_keys & import_keys

    if action_if_exists == "fail" and matched_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The variables with these keys: {matched_keys} already exists.",
        )
    elif action_if_exists == "skip":
        create_keys = import_keys - matched_keys
    else:
        create_keys = import_keys

    for key in create_keys:
        Variable.set(key=key, value=variables[key], session=session)

    return VariablesImportResponse(
        created_count=len(create_keys),
        import_count=len(import_keys),
        created_variable_keys=list(create_keys),
    )


@variables_router.patch("")
def bulk_variables(
    request: VariableBulkBody,
    session: SessionDep,
) -> VariableBulkResponse:
    """Bulk create, update, and delete variables."""
    results: dict[str, VariableBulkActionResponse] = {}

    for action in request.actions:
        if action.action not in results:
            results[action.action] = VariableBulkActionResponse()

        if action.action == "create":
            handle_bulk_create(session, action, results[action.action])
        elif action.action == "update":
            handle_bulk_update(session, action, results[action.action])
        elif action.action == "delete":
            handle_bulk_delete(session, action, results[action.action])

    return VariableBulkResponse(**results)
