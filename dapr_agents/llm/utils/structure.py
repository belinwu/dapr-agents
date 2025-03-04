from typing import Type, TypeVar, Optional, Union, List, get_args, Any, Dict, Literal
from pydantic import BaseModel, Field, create_model, ValidationError
from dapr_agents.tool.utils.function_calling import to_function_call_definition
from dapr_agents.types import StructureError, OAIJSONSchema, OAIResponseFormatSchema
from collections.abc import Iterable
import logging
import json

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

class StructureHandler:
    @staticmethod
    def is_json_string(input_string: str) -> bool:
        """
        Check if the given input is a valid JSON string.

        Args:
            input_string (str): The string to check.

        Returns:
            bool: True if the input is a valid JSON string, False otherwise.
        """
        try:
            json.loads(input_string)
            return True
        except json.JSONDecodeError:
            return False
    
    @staticmethod
    def generate_request(
        response_format: Union[Type[T], Dict[str, Any], Iterable[Type[T]]],
        llm_provider: str,
        structured_mode: Literal["json", "function_call"] = "json",
        **params
    ) -> Dict[str, Any]:
        """
        Generates a structured request that conforms to a specified API format using the given Pydantic model.
        This function prepares a request configuration that includes the model as a tool specification and
        sets the necessary parameters for the API call.

        Args:
            response_format (Union[Type[T], Dict[str, Any], Iterable[Type[T]]]): Defines the response structure.
                - If `structured_mode="json"`: Can be a Pydantic model (converted to JSON schema) or a JSON schema dictionary.
                - If `structured_mode="function_call"`: Must be a Pydantic model or an iterable of models.
                - If an iterable of models is provided in either mode, it is treated as a list schema.
            llm_provider (str): The LLM provider (e.g., "openai", "claude").
            structured_mode (Literal["json", "function_call"]): Determines the response structure.
                - "json": Generates and enforces a strict JSON schema.
                - "function_call": Converts a Pydantic model to a function-call definition.
            **params: Additional request parameters.

        Returns:
            Dict[str, Any]: Updated request parameters, including tools or response format.

        Raises:
            ValueError: If an unsupported `structured_mode` is provided.
            TypeError: If `response_format` is invalid for the selected mode.
        """
        logger.debug(f"Structured response mode: {structured_mode}")

        # Handle iterable models in both modes
        is_iterable = isinstance(response_format, Iterable) and not isinstance(response_format, (dict, str))
        if is_iterable:
            logger.debug("Detected an iterable response format.")
            item_model = get_args(response_format)[0]
            response_format = StructureHandler.create_iterable_model(item_model)

        if structured_mode == "function_call":
            # Ensure response_format is a valid Pydantic model
            if not (isinstance(response_format, type) and issubclass(response_format, BaseModel)):
                raise TypeError("function_call mode requires a Pydantic model or an iterable of it.")

            name = response_format.__name__
            description = response_format.__doc__ or ""
            model_tool_format = to_function_call_definition(name, description, response_format, llm_provider)

            params["tools"] = [model_tool_format]
            params["tool_choice"] = {
                "type": "function",
                "function": {"name": model_tool_format["function"]["name"]},
            }
            return params

        elif structured_mode == "json":
            try:
                # If it's a dict, assume it's already a JSON schema; otherwise, try to create from model
                if isinstance(response_format, dict):
                    raw_schema = response_format
                    name = response_format.get("name", "custom_schema")
                    description = response_format.get("description")
                elif isinstance(response_format, type) and issubclass(response_format, BaseModel):
                    raw_schema = response_format.model_json_schema()
                    name = response_format.__name__
                    description = response_format.__doc__
                else:
                    raise TypeError("json mode requires a dict or a Pydantic model.")
                
                # Enforce strict JSON schema (process $refs, $defs, etc.)
                logger.debug(f"Raw Schema: {raw_schema}")
                strict_schema = StructureHandler.enforce_strict_json_schema(raw_schema)

                # Construct the JSON schema object using OAIJSONSchema
                json_schema_obj = OAIJSONSchema(
                    name=name,
                    description=description,
                    schema_=strict_schema,
                    strict=True
                )

                # Wrap it in the top-level response format object
                response_format_obj = OAIResponseFormatSchema(json_schema=json_schema_obj)

                logger.debug(f"Generated JSON schema: {response_format_obj.model_dump()}")

                # Use model_dump() to serialize the response format into a dictionary
                params["response_format"] = response_format_obj.model_dump(by_alias=True)

            except ValidationError as e:
                logger.error(f"Validation error in JSON schema: {e}")
                raise ValueError(f"Invalid response_format provided: {e}")

            return params

        else:
            raise ValueError(f"Unsupported structured_mode: {structured_mode}")

    @staticmethod
    def create_iterable_model(
        model: Type[BaseModel],
        model_name: Optional[str] = None,
        model_description: Optional[str] = None
    ) -> Type[BaseModel]:
        """
        Constructs an iterable Pydantic model for a given Pydantic model.

        Args:
            model (Type[BaseModel]): The original Pydantic model to capture a list of objects of the original model type.
            model_name (Optional[str]): The name of the new iterable model. Defaults to None.
            model_description (Optional[str]): The description of the new iterable model. Defaults to None.

        Returns:
            Type[BaseModel]: A new Pydantic model class representing a list of the original Pydantic model.
        """
        model_name = model.__name__ if model_name is None else model_name
        iterable_model_name = f"Iterable{model_name}"

        objects_field = (
            List[model], 
            Field(..., description=f"A list of `{model_name}` objects")
        )

        iterable_model = create_model(
            iterable_model_name,
            objects=objects_field,
            __base__=(BaseModel,)
        )

        iterable_model.__doc__ = (
            f"A Pydantic model to capture `{iterable_model_name}` objects"
            if model_description is None
            else model_description
        )

        return iterable_model

    @staticmethod
    def extract_structured_response(
        response: Any,
        llm_provider: str,
        structured_mode: Literal["json", "function_call"] = "json"
    ) -> Union[str, Dict[str, Any]]:
        """
        Extracts the structured JSON string or content from the response.

        Args:
            response (Any): The API response data to extract.
            llm_provider (str): The LLM provider (e.g., 'openai').
            structured_mode (Literal["json", "function_call"]): The structured response mode.

        Returns:
            Union[str, Dict[str, Any]]: The extracted structured response.

        Raises:
            StructureError: If the structured response is not found or extraction fails.
        """
        try:
            logger.debug(f"Processing structured response for mode: {structured_mode}")
            if llm_provider in ("openai", "nvidia"):
                # Extract the `choices` list from the response
                choices = getattr(response, "choices", None)
                if not choices or not isinstance(choices, list):
                    raise StructureError("Response does not contain valid 'choices'.")

                # Extract the message object
                message = getattr(choices[0], "message", None)
                if not message:
                    raise StructureError("Response message is missing.")

                if structured_mode == "function_call":
                    tool_calls = getattr(message, "tool_calls", None)
                    if tool_calls:
                        function = getattr(tool_calls[0], "function", None)
                        if function and hasattr(function, "arguments"):
                            extracted_response = function.arguments
                            logger.debug(f"Extracted function-call response: {extracted_response}")
                            return extracted_response
                    raise StructureError("No tool_calls found for function_call mode.")
                
                elif structured_mode == "json":
                    content = getattr(message, "content", None)
                    refusal = getattr(message, "refusal", None)

                    if refusal:
                        logger.warning(f"Model refused to fulfill the request: {refusal}")
                        raise StructureError(f"Request refused by the model: {refusal}")

                    if not content:
                        raise StructureError("No content found for JSON mode.")

                    logger.debug(f"Extracted JSON content: {content}")
                    return content

                else:
                    raise ValueError(f"Unsupported structured_mode: {structured_mode}")
            else:
                raise StructureError(f"Unsupported LLM provider: {llm_provider}")
        except Exception as e:
            logger.error(f"Error while extracting structured response: {e}")
            raise StructureError(f"Extraction failed: {e}")

    @staticmethod
    def validate_response(response: Union[str, dict], model: Type[T]) -> T:
        """
        Validates a JSON string or a dictionary using a specified Pydantic model.

        This method checks whether the response is a JSON string or a dictionary. 
        If the response is a JSON string, it validates it using the `model_validate_json` method.
        If the response is a dictionary, it validates it using the `model_validate` method.

        Args:
            response (Union[str, dict]): The JSON string or dictionary to validate.
            model (Type[T]): The Pydantic model that defines the expected structure of the response.

        Returns:
            T: An instance of the Pydantic model populated with the validated data.

        Raises:
            StructureError: If the validation fails.
        """
        try:
            if isinstance(response, str) and StructureHandler.is_json_string(response):
                return model.model_validate_json(response)
            elif isinstance(response, dict):
                # If it's a dictionary, use model_validate
                return model.model_validate(response)
            else:
                raise ValueError("Response must be a JSON string or a dictionary.")
        except ValidationError as e:
            logger.error(f"Validation error while parsing structured response: {e}")
            raise StructureError(f"Validation failed for structured response: {e}")
    
    @staticmethod
    def expand_local_refs(part: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively expand all local $refs in the schema, including nested references.

        Args:
            part (Dict[str, Any]): The schema part to process.
            root (Dict[str, Any]): The root schema for resolving $refs.

        Returns:
            Dict[str, Any]: The schema part with all $refs expanded.
        """
        ref = part.pop("$ref", None)
        if ref:
            logger.debug(f"Found $ref: {ref}")
            if not ref.startswith("#/$defs/"):
                raise ValueError(f"Unexpected $ref format: {ref}")
            
            ref_name = ref.split("/")[-1]
            defs_section = root.get("$defs", {})
            if ref_name not in defs_section:
                raise ValueError(f"Reference '{ref_name}' not found in $defs.")
            
            # Merge the referenced schema with the current part, resolving nested $refs
            merged = {**defs_section[ref_name], **{k: v for k, v in part.items() if k != "$ref"}}
            return StructureHandler.expand_local_refs(merged, root)

        # Process objects and their properties
        if part.get("type") == "object" and "properties" in part:
            for key, value in part["properties"].items():
                part["properties"][key] = StructureHandler.expand_local_refs(value, root)
        
        # Process arrays and their items
        if part.get("type") == "array" and "items" in part:
            part["items"] = StructureHandler.expand_local_refs(part["items"], root)
        
        # Process anyOf and allOf schemas
        for key in ("anyOf", "allOf"):
            if key in part and isinstance(part[key], list):
                part[key] = [StructureHandler.expand_local_refs(subschema, root) for subschema in part[key]]

        return part

    @staticmethod
    def enforce_strict_json_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enforces strict JSON schema constraints while making it OpenAI-compatible.

        - Expands all local $refs by resolving them from the $defs section.
        - Ensures "additionalProperties": false for object schemas.
        - Removes default values and replaces optional fields with `anyOf: [{"type": T}, {"type": "null"}]`.
        - Converts optional arrays to `anyOf: [{"type": "array", "items": T}, {"type": "null"}]`.
        - Ensures all `array` schemas define `items`.
        - Converts optional integers and floats to `anyOf: [{"type": T}, {"type": "null"}]`.
        - Prevents the use of `anyOf` at the root level of an array.

        Args:
            schema (Dict[str, Any]): The JSON schema dictionary to process.

        Returns:
            Dict[str, Any]: The updated schema with strict constraints applied.
        """
        # Expand all $refs (resolves and removes them)
        schema = StructureHandler.expand_local_refs(schema, schema)

        # Ensure "additionalProperties": false for all objects
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)

            required_fields = set(schema.get("required", []))

            for key, value in schema.get("properties", {}).items():
                schema["properties"][key] = StructureHandler.enforce_strict_json_schema(value)

                # Remove default values (not allowed by OpenAI)
                schema["properties"][key].pop("default", None)

                # Convert optional fields (string, number, integer) to `anyOf`
                if key not in required_fields:
                    field_type = schema["properties"][key].get("type")

                    if field_type and not isinstance(field_type, list):  # Ensure it's not already `anyOf`
                        if field_type in ["string", "integer", "number"]:
                            schema["properties"][key]["anyOf"] = [{"type": field_type}, {"type": "null"}]
                            schema["properties"][key].pop("type", None)  # Remove direct "type" field

                    # Ensure field is included in "required" (even if it allows null)
                    required_fields.add(key)

                # Handle optional arrays inside object properties
                if schema["properties"][key].get("anyOf") and isinstance(schema["properties"][key]["anyOf"], list):
                    for subschema in schema["properties"][key]["anyOf"]:
                        if subschema.get("type") == "array":
                            schema["properties"][key] = {
                                "anyOf": [
                                    {"type": "array", "items": subschema.get("items", {})},
                                    {"type": "null"}
                                ]
                            }

            # Ensure all required fields are explicitly listed
            schema["required"] = list(required_fields)

        # Process arrays and enforce strictness
        if schema.get("type") == "array":
            # Ensure `items` is always present in arrays
            if "items" not in schema:
                raise ValueError(f"Array schema missing 'items': {schema}")

            schema["items"] = StructureHandler.enforce_strict_json_schema(schema["items"])

            # Convert optional arrays from `anyOf` to `anyOf: [{"type": "array", "items": T}, {"type": "null"}]`
            if "anyOf" in schema and isinstance(schema["anyOf"], list):
                if any(subschema.get("type") == "array" for subschema in schema["anyOf"]):
                    schema["anyOf"] = [
                        {"type": "array", "items": schema["items"]},
                        {"type": "null"}
                    ]
                    schema.pop("type", None)  # Remove direct "type" field
                    schema.pop("minItems", None)  # Remove `minItems`, not needed with null

        # Process $defs and remove after expansion
        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                schema["$defs"][def_name] = StructureHandler.enforce_strict_json_schema(def_schema)
            schema.pop("$defs", None)

        # Process anyOf and allOf schemas recursively
        for key in ("anyOf", "allOf"):
            if key in schema and isinstance(schema[key], list):
                schema[key] = [StructureHandler.enforce_strict_json_schema(subschema) for subschema in schema[key]]

        return schema