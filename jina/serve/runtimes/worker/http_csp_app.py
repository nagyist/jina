from typing import (TYPE_CHECKING, Callable, Dict, List, Literal, Optional,
                    Union)

from jina._docarray import docarray_v2, is_pydantic_v2
from jina.importer import ImportExtensions
from jina.types.request.data import DataRequest

if TYPE_CHECKING:
    from jina.logging.logger import JinaLogger

if docarray_v2:
    from docarray import BaseDoc, DocList
    from docarray.utils._internal._typing import safe_issubclass


def get_fastapi_app(
        request_models_map: Dict,
        caller: Callable,
        logger: 'JinaLogger',
        cors: bool = False,
        **kwargs,
):
    """
    Get the app from FastAPI as the REST interface.

    :param request_models_map: Map describing the endpoints and its Pydantic models
    :param caller: Callable to be handled by the endpoints of the returned FastAPI app
    :param logger: Logger object
    :param cors: If set, a CORS middleware is added to FastAPI frontend to allow cross-origin access.
    :param kwargs: Extra kwargs to make it compatible with other methods
    :return: fastapi app
    """
    with ImportExtensions(required=True):
        import pydantic
        from fastapi import FastAPI, HTTPException, Request, status as http_status
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel, Field
        from pydantic.config import BaseConfig
        if not is_pydantic_v2:
            from pydantic.config import inherit_config

    import os

    from jina.proto import jina_pb2
    from jina.serve.runtimes.gateway.models import _to_camel_case

    if not docarray_v2:
        logger.warning('Only docarray v2 is supported with CSP. ')
        return

    class Header(BaseModel):
        request_id: Optional[str] = Field(
            description='Request ID', example=os.urandom(16).hex()
        )

        class Config(BaseConfig):
            alias_generator = _to_camel_case
            allow_population_by_field_name = True

    class InnerConfig(BaseConfig):
        alias_generator = _to_camel_case
        allow_population_by_field_name = True

    app = FastAPI()

    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=['*'],
            allow_credentials=True,
            allow_methods=['*'],
            allow_headers=['*'],
        )
        logger.warning('CORS is enabled. This service is accessible from any website!')

    def add_post_route(
            endpoint_path,
            input_model,
            output_model,
            input_doc_list_model=None,
            parameters_model=None,
    ):
        import json
        from typing import List, Type, Union

        try:
            from typing import get_args, get_origin
        except ImportError:
            from typing_extensions import get_args, get_origin

        from docarray.base_doc.docarray_response import DocArrayResponse
        from pydantic import BaseModel, ValidationError, parse_obj_as

        app_kwargs = dict(
            path=f'/{endpoint_path.strip("/")}',
            methods=['POST'],
            summary=f'Endpoint {endpoint_path}',
            response_model=Union[output_model, List[output_model]],
            response_class=DocArrayResponse,
        )

        def is_valid_csv(content: str) -> bool:
            import csv
            from io import StringIO

            try:
                f = StringIO(content)
                reader = csv.DictReader(f)
                for _ in reader:
                    pass

                return True
            except Exception:
                return False

        async def process(body) -> output_model:
            req = DataRequest()
            if body.header is not None:
                req.header.request_id = body.header.request_id

            if body.parameters is not None:
                req.parameters = body.parameters
            req.header.exec_endpoint = endpoint_path
            req.document_array_cls = DocList[input_doc_list_model]

            data = body.data
            if isinstance(data, list):
                req.data.docs = DocList[input_doc_list_model](data)
            else:
                req.data.docs = DocList[input_doc_list_model]([data])
                if body.header is None:
                    req.header.request_id = req.docs[0].id

            resp = await caller(req)
            status = resp.header.status

            if status.code == jina_pb2.StatusProto.ERROR:
                raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=status.description)
            else:
                return output_model(data=resp.docs, parameters=resp.parameters)

        @app.api_route(**app_kwargs)
        async def post(request: Request):
            content_type = request.headers.get('content-type')
            if content_type == 'application/json':
                json_body = await request.json()
                return await process(input_model(**json_body))

            elif content_type in ('text/csv', 'application/csv'):
                import csv
                from io import StringIO

                bytes_body = await request.body()
                csv_body = bytes_body.decode('utf-8')
                if not is_valid_csv(csv_body):
                    raise HTTPException(
                        status_code=http_status.HTTP_400_BAD_REQUEST,
                        detail='Invalid CSV input. Please check your input.',
                    )

                def recursive_parse(origin, field_name, field_type, field_str, parsed_fields):
                    if origin is Literal:
                        literal_values = get_args(field_type)
                        if field_str not in literal_values:
                            raise HTTPException(
                                status_code=http_status.HTTP_400_BAD_REQUEST,
                                detail=f"Invalid value '{field_str}' for field '{field_name}'. Expected one of: {literal_values}"
                            )
                        parsed_fields[field_name] = field_str

                    # Handle Union types (e.g., Optional[int, str])
                    elif origin is Union:
                        for possible_type in get_args(field_type):
                            possible_origin = get_origin(possible_type)
                            try:
                                recursive_parse(origin=possible_origin,
                                                field_name=field_name,
                                                field_type=possible_type,
                                                field_str=field_str,
                                                parsed_fields=parsed_fields)
                                success = True
                                break
                            except (ValueError, TypeError, ValidationError):
                                continue

                        if not success and field_str:  # Only raise if there's a value to parse
                            raise ValueError(
                                f"Could not parse '{field_str}' as any of the possible types for '{field_name}'"
                            )
                    elif origin is list:
                        # TODO: this may need to be also recursive
                        list_item_type = get_args(field_type)[0]
                        if field_str:
                            parsed_list = json.loads(field_str)
                            if safe_issubclass(list_item_type, BaseModel):
                                if is_pydantic_v2:
                                    parsed_fields[field_name] = [list_item_type.model_validate(item) for item in
                                                                 parsed_list]
                                else:
                                    parsed_fields[field_name] = parse_obj_as(List[list_item_type], parsed_list)
                            else:
                                parsed_fields[field_name] = parsed_list
                    elif safe_issubclass(field_type, DocList):
                        list_item_type = field_type.doc_type
                        if field_str:
                            parsed_list = json.loads(field_str)
                            if safe_issubclass(list_item_type, BaseDoc):
                                if is_pydantic_v2:
                                    parsed_fields[field_name] = DocList[list_item_type](
                                        [list_item_type.model_validate(item) for item in parsed_list])
                                else:
                                    parsed_fields[field_name] = parse_obj_as(DocList[list_item_type],
                                                                             parsed_list)
                            else:
                                parsed_fields[field_name] = parsed_list
                    # Handle other general types
                    else:
                        if field_str:
                            if field_type == bool:
                                # Special case: handle "false" and "true" as booleans
                                if field_str.lower() == "false":
                                    parsed_fields[field_name] = False
                                elif field_str.lower() == "true":
                                    parsed_fields[field_name] = True
                                else:
                                    raise HTTPException(
                                        status_code=http_status.HTTP_400_BAD_REQUEST,
                                        detail=f"Invalid value '{field_str}' for boolean field '{field_name}'. Expected 'true' or 'false'."
                                    )
                            else:
                                # General case: try converting to the target type
                                try:
                                    parsed_fields[field_name] = DocList[field_type](field_str)
                                except (ValueError, TypeError):
                                    # Fallback to parse_obj_as when type is more complex, e., AnyUrl or ImageBytes
                                    parsed_fields[field_name] = parse_obj_as(field_type, field_str)

                def construct_model_from_line(model: Type[BaseModel], line: List[str]) -> BaseModel:
                    origin = get_origin(model)
                    # If the model is of type Optional[X], unwrap it to get X
                    if origin is Union:
                        # If the model is of type Optional[X], unwrap it to get X
                        args = get_args(model)
                        if type(None) in args:
                            model = args[0]

                    parsed_fields = {}
                    if is_pydantic_v2:
                        model_fields = model.model_fields
                    else:
                        model_fields = model.__fields__

                    for idx, (field_name, field_info) in enumerate(model_fields.items()):
                        if is_pydantic_v2:
                            field_type = field_info.annotation
                        else:
                            field_type = field_info.outer_type_
                        field_str = line[idx]  # Corresponding value from the row
                        # Handle Literal types (e.g., Optional[Literal["value1", "value2"]])
                        origin = get_origin(field_type)
                        try:
                            recursive_parse(origin=origin,
                                            field_name=field_name,
                                            field_type=field_type,
                                            field_str=field_str,
                                            parsed_fields=parsed_fields)
                        except Exception as e:
                            raise HTTPException(
                                status_code=http_status.HTTP_400_BAD_REQUEST,
                                detail=f"Error parsing value '{field_str}' for field '{field_name}': {str(e)}"
                            )

                    return model(**parsed_fields)

                # NOTE: Sagemaker only supports csv files without header, so we enforce
                # the header by getting the field names from the input model.
                # This will also enforce the order of the fields in the csv file.
                # This also means, all fields in the input model must be present in the
                # csv file including the optional ones.
                # We also expect the csv file to have no quotes and use the escape char '\'
                field_names = [f for f in input_doc_list_model.__fields__]
                data = []
                parameters = None
                first_row = True
                for line in csv.reader(
                        StringIO(csv_body),
                        delimiter=',',
                        quoting=csv.QUOTE_NONE,
                        escapechar='\\',
                ):
                    if first_row:
                        first_row = False
                        if len(line) > 1 and line[
                            1] == 'params_row':  # Check if it's a parameters row by examining the 2nd text in the first line
                            parameters = construct_model_from_line(parameters_model, line[2:])
                        else:
                            if len(line) != len(field_names):
                                raise HTTPException(
                                    status_code=http_status.HTTP_400_BAD_REQUEST,
                                    detail=f'Invalid CSV format. Line {line} doesn\'t match '
                                           f'the expected field order {field_names}.',
                                )
                            data.append(construct_model_from_line(input_doc_list_model, line))
                    else:
                        # Treat it as normal data row
                        if len(line) != len(field_names):
                            raise HTTPException(
                                status_code=http_status.HTTP_400_BAD_REQUEST,
                                detail=f'Invalid CSV format. Line {line} doesn\'t match '
                                       f'the expected field order {field_names}.',
                            )
                        data.append(construct_model_from_line(input_doc_list_model, line))

                return await process(input_model(data=data, parameters=parameters))

            else:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=f'Invalid content-type: {content_type}. '
                           f'Please use either application/json or text/csv.',
                )

    for endpoint, input_output_map in request_models_map.items():
        if endpoint != '_jina_dry_run_':
            input_doc_model = input_output_map['input']['model']
            output_doc_model = input_output_map['output']['model']
            parameters_model = input_output_map['parameters']['model']
            parameters_model_needed = parameters_model is not None
            if parameters_model_needed:
                try:
                    _ = parameters_model()
                    parameters_model_needed = False
                except:
                    parameters_model_needed = True
                parameters_model = parameters_model if parameters_model_needed else Optional[parameters_model]
                default_parameters = (
                    ... if parameters_model_needed else None
                )
            else:
                parameters_model = Optional[Dict]
                default_parameters = None

            if not is_pydantic_v2:
                _config = inherit_config(InnerConfig, BaseDoc.__config__)
            else:
                _config = InnerConfig

            endpoint_input_model = pydantic.create_model(
                f'{endpoint.strip("/")}_input_model',
                data=(Union[List[input_doc_model], input_doc_model], ...),
                parameters=(parameters_model, default_parameters),
                header=(Optional[Header], None),
                __config__=_config,
            )

            endpoint_output_model = pydantic.create_model(
                f'{endpoint.strip("/")}_output_model',
                data=(Union[List[output_doc_model], output_doc_model], ...),
                parameters=(Optional[Dict], None),
                __config__=_config,
            )

            add_post_route(
                endpoint,
                input_model=endpoint_input_model,
                output_model=endpoint_output_model,
                input_doc_list_model=input_doc_model,
                parameters_model=parameters_model,
            )

    from jina.serve.runtimes.gateway.health_model import JinaHealthModel

    # `/ping` route is required by AWS Sagemaker
    @app.get(
        path='/ping',
        summary='Get the health of Jina Executor service',
        response_model=JinaHealthModel,
    )
    async def _executor_health():
        """
        Get the health of this Gateway service.
        .. # noqa: DAR201

        """
        return {}

    return app
