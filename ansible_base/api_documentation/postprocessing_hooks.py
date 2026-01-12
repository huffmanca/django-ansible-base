import logging
from typing import Any, Optional

from inflection import singularize

from ansible_base.api_documentation.preprocessing_hooks import OPERATION_CLASS_MAP, RESOURCE_PURPOSE_MAP, SKIP_AI_DESCRIPTION_PREFIXES
from ansible_base.lib.utils.api_path_utils import (
    extract_operation_action,
    extract_operation_prefix,
    filter_api_prefixes,
    parse_path_segments,
)

logger = logging.getLogger('ansible_base.api_documentation.postprocessing_hooks')

# Valid HTTP methods for OpenAPI operations
HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']

# Operation name mappings for different HTTP methods and actions
OPERATION_DESCRIPTIVE_NAMES = {
    'list': 'List all',
    'create': 'Create new',
    'retrieve': 'Retrieve single',
    'read': 'Retrieve single',
    'update': 'Update existing',
    'partial_update': 'Partially update existing',
    'destroy': 'Delete existing',
    'delete': 'Delete existing',
}

# Operation description templates for generating descriptions from resource_purpose
# These lambdas are used to create contextual descriptions for CRUD operations
OPERATION_DESCRIPTION_TEMPLATES = {
    'list': lambda p: f"List {p}",
    'retrieve': lambda p: f"Retrieve a {singularize_resource_purpose(p)}",
    'read': lambda p: f"Retrieve a {singularize_resource_purpose(p)}",
    'create': lambda p: f"Create a {singularize_resource_purpose(p)}",
    'update': lambda p: f"Update a {singularize_resource_purpose(p)}",
    'partial_update': lambda p: f"Update a {singularize_resource_purpose(p)}",
    'destroy': lambda p: f"Delete a {singularize_resource_purpose(p)}",
    'delete': lambda p: f"Delete a {singularize_resource_purpose(p)}",
}


def _has_segments_after_placeholder(parts: list[str], placeholder_index: int) -> bool:
    """
    Check if there are non-placeholder resource segments after a URL path parameter placeholder.

    Placeholders are URL path parameters (any segment starting with '{') that represent specific resource instances.
    For example, in '/api/v1/teams/{id}/users/', '{id}' is a placeholder for a specific team's ID.
    This function checks if there are resource segments (like 'users') after the placeholder.
    """
    remaining_parts = [p for p in parts[placeholder_index + 1 :] if p and not p.startswith('{')]
    return bool(remaining_parts)


def _extract_parent_from_prefix(parts: list[str], placeholder_index: int) -> Optional[str]:
    """
    Extract parent resource name (singular) from path segments before a URL path parameter placeholder.

    Placeholders (any segment starting with '{') represent specific resource instances.
    For example, in '/api/v1/teams/{id}/users/', this extracts 'team' (singular)
    from the segments before the {id} placeholder.
    """
    parent_prefix = '/'.join(parts[: placeholder_index + 1])
    parent_segments = parse_path_segments(parent_prefix)

    if parent_segments:
        return singularize(parent_segments[-1])

    return None


def _find_parent_resource_from_path(path: str) -> Optional[str]:
    """
    Find parent resource in nested REST path by detecting URL path parameter placeholders with segments after.

    Placeholders are URL path parameters (like {id}, {pk}, {name}, {slug}, etc.) that represent specific resource instances.
    If there are additional resource segments after a placeholder, this indicates a nested parent-child relationship.

    Examples:
        '/api/v1/teams/{id}/users/' -> Returns 'team' (parent of users)
        '/api/v1/teams/{name}/members/' -> Returns 'team' (parent of members)
        '/api/v1/teams/' -> Returns None (not nested)
        '/api/v1/teams/{id}/' -> Returns None (no child resource)

    Args:
        path: The URL path to analyze

    Returns:
        Singular parent resource name or None if not nested.
    """
    # Split path into segments
    parts = path.split('/')

    # Find the first placeholder (any segment starting with '{')
    placeholder_index = next((i for i, part in enumerate(parts) if part.startswith('{')), -1)

    if placeholder_index == -1:
        return None

    # Check if there are resource segments after the placeholder
    if not _has_segments_after_placeholder(parts, placeholder_index):
        return None  # Not truly nested - no child resources after placeholder

    # Extract and return parent resource name
    return _extract_parent_from_prefix(parts, placeholder_index)


def extract_action_and_resource(operation_id: str, path: str) -> tuple[str, list[str], Optional[str]]:
    """
    Extract action, resource parts, and parent resource from operation_id and path.
    Returns: (action, resource_parts, parent_resource)
    """
    # Extract action using utility function
    action = extract_operation_action(operation_id)

    # Extract resource parts from operation_id (prefix before action, split into parts)
    # Example: 'teams_users_create' -> ['teams', 'users']  (removes 'create')
    # If prefix equals action, there are no resource parts (e.g., 'retrieve' -> no resources)
    prefix = extract_operation_prefix(operation_id)
    resource_parts = prefix.split('_') if prefix and prefix != action else []

    # Detect parent-child relationships from path
    parent_resource = _find_parent_resource_from_path(path)

    # Fallback to extracting from path if no resource_parts
    if not resource_parts:
        path_parts = parse_path_segments(path)
        if path_parts:
            resource_parts = [path_parts[-1]]
        else:
            resource_parts = ['resource']

    return action, resource_parts, parent_resource


def format_compound_resource(resource_parts: list[str], parent_resource: Optional[str], action: str) -> str:
    """
    Format compound resource names with proper prepositions for nested resources.
    E.g., "users for a team", "route for an HTTP port"
    """
    resource_name = ' '.join(resource_parts).replace('_', ' ')

    # Handle nested resources with generic "for" preposition
    if len(resource_parts) >= 2 and parent_resource:
        child_resource = resource_parts[-1]

        # Determine article (a/an) based on parent resource
        if parent_resource[0].lower() in 'aeiou':
            article = 'an'
        else:
            article = 'a'

        # For list actions, use plural child
        if action == 'list':
            return f"{child_resource} for {article} {parent_resource}"
        else:
            # For singular actions, singularize child
            singular_child = singularize(child_resource)
            return f"{singular_child} for {article} {parent_resource}"

    return resource_name


def generate_description_from_purpose(resource_purpose: str, action: str) -> str:
    """Generate x-ai-description from resource_purpose using action templates."""
    template = OPERATION_DESCRIPTION_TEMPLATES.get(action)
    if template:
        return template(resource_purpose)
    return resource_purpose


def singularize_resource_purpose(purpose: str) -> str:
    """
    Singularize resource_purpose by finding prepositions and singularizing the noun phrase before.
    E.g., "audit trail entries for tracking..." -> "audit trail entry for tracking..."
    """
    # Common prepositions that separate noun phrase from context
    prepositions = [' for ', ' in ', ' of ', ' from ', ' to ', ' with ', ' on ']

    # Try to find a preposition to split on
    for prep in prepositions:
        if prep in purpose:
            # Split on first occurrence to separate noun phrase from context
            parts = purpose.split(prep, 1)
            noun_phrase = parts[0]
            if len(parts) > 1:
                context = parts[1]
            else:
                context = ''

            # Singularize the noun phrase (last word)
            words = noun_phrase.split()
            if words:
                last_word = words[-1]
                singular_last = singularize(last_word)
                words[-1] = singular_last
                singular_noun = ' '.join(words)
            else:
                singular_noun = noun_phrase

            # Reconstruct with preposition
            return f"{singular_noun}{prep}{context}"

    # No preposition found, singularize the last word of the entire string
    words = purpose.split()
    if not words:
        return purpose

    last_word = words[-1]
    singular_last = singularize(last_word)
    words[-1] = singular_last
    return ' '.join(words)


def generate_associate_description(operation_id: str, path: str, resource_name: str) -> str:
    """Generate description for associate/disassociate operations."""
    is_associate = operation_id.endswith('_associate_create')

    # Filter out API prefixes (everything up to version string) using generic approach
    path_parts = filter_api_prefixes(parse_path_segments(path))

    # Remove associate/disassociate if it's the last segment (special action marker)
    if path_parts and path_parts[-1] in ['associate', 'disassociate']:
        path_parts = path_parts[:-1]

    if is_associate:
        verb = "Associate"
        preposition = "with"
    else:
        verb = "Disassociate"
        preposition = "from"

    if len(path_parts) >= 2:
        child = path_parts[-1].replace('_', ' ')
        parent_singular = singularize(path_parts[-2])
        parent_readable = parent_singular.replace('_', ' ')

        if parent_readable[0].lower() in 'aeiou':
            article = 'an'
        else:
            article = 'a'
        return f"{verb} {child} {preposition} {article} {parent_readable}"

    # Fallback if pattern doesn't match
    return f"{verb} {resource_name}"


def generate_crud_description(action: str, operation_name: str, resource_name: str, parent_resource: Optional[str]) -> Optional[str]:
    """Generate description for standard CRUD operations."""
    # For list operations, use the name as-is (already formatted)
    if action == 'list':
        return f"{operation_name} {resource_name}"

    # For partial_update, use special formatting
    if action == 'partial_update':
        if parent_resource:
            return f"Partially update existing {resource_name}"
        return f"Partially update existing {singularize(resource_name)}"

    # For other CRUD operations supported by OPERATION_DESCRIPTION_TEMPLATES
    if action in OPERATION_DESCRIPTION_TEMPLATES.keys():
        if parent_resource:
            # Compound resources, which imply a relationship between two elements,
            # are already formatted by format_compound_resource()
            return f"{operation_name} {resource_name}"
        # Simple resources need singularization
        return f"{operation_name} {singularize(resource_name)}"

    return None  # Indicate that this isn't a standard CRUD operation


def generate_custom_action_description(operation_name: str, resource_name: str, operation: dict) -> str:
    """Generate description for custom actions."""
    base_description = operation.get('description', '').strip()
    clean_desc = clean_base_description(base_description)
    if clean_desc:
        return f"{operation_name} {clean_desc}"
    return f"{operation_name} {resource_name}"


def clean_base_description(description: str) -> str:
    """Clean up base description by removing common boilerplate."""
    if not description:
        return ""

    clean_desc = description.strip()

    # Remove common prefixes
    prefixes_to_remove = [
        'API endpoint that allows',
        'API endpoint that',
        'API endpoint for',
        'A view class for managing and displaying',
        'Endpoint:',
    ]

    for prefix in prefixes_to_remove:
        if clean_desc.lower().startswith(prefix.lower()):
            clean_desc = clean_desc[len(prefix) :].strip()
            break

    # Obtain the full multi-line description by joining lines
    clean_desc = ' '.join(clean_desc.split('\n'))

    # Obtain the first sentence by splitting on sentence-ending punctuation
    for punct in ['.', '!', '?']:
        if punct in clean_desc:
            clean_desc = clean_desc.split(punct)[0]
            break

    # Remove trailing punctuation
    clean_desc = clean_desc.rstrip('.,;:!?')

    # We enforce a hard limit at the end of our processing; no need to repeat it here.
    return clean_desc.strip()


def _lookup_resource_purpose(operation_id: str) -> tuple[Optional[str], Optional[str]]:
    """Look up resource_purpose for an operation via its prefix and ViewSet class."""
    prefix = extract_operation_prefix(operation_id)
    class_info = OPERATION_CLASS_MAP.get(prefix)

    if not class_info:
        return None, None

    class_name, _, _ = class_info  # Extract class name from (class_name, path_parts_count, path_parts) tuple
    resource_purpose = RESOURCE_PURPOSE_MAP.get(class_name)
    return resource_purpose, class_name


def _generate_from_resource_purpose(resource_purpose: Optional[str], action: str) -> Optional[str]:
    """Generate description from resource_purpose if action is supported."""
    if resource_purpose and action in OPERATION_DESCRIPTION_TEMPLATES.keys():
        return generate_description_from_purpose(resource_purpose, action)

    return None


def _generate_description_auto(operation_id: str, action: str, resource_parts: list[str], parent_resource: Optional[str], path: str, operation: dict) -> str:
    """Auto-generate description from resource names and operation type."""
    # Format the resource name with proper grammar
    resource_name = format_compound_resource(resource_parts, parent_resource, action)

    # Get the descriptive operation name
    operation_name = OPERATION_DESCRIPTIVE_NAMES.get(action, action.replace('_', ' ').capitalize())

    # Handle associate/disassociate operations
    if operation_id.endswith('_associate_create') or operation_id.endswith('_disassociate_create'):
        return generate_associate_description(operation_id, path, resource_name)

    # Try standard CRUD operation
    ai_description = generate_crud_description(action, operation_name, resource_name, parent_resource)

    # Fall back to custom action if not CRUD
    if ai_description is None:
        ai_description = generate_custom_action_description(operation_name, resource_name, operation)

    return ai_description


def _enforce_character_limit(description: str, max_length: int = 300) -> str:
    """Enforce character limit on description, truncating if necessary."""
    if len(description) < max_length:
        return description

    # Break at the last word before max_length:
    truncated = description[: max_length - 3]
    last_space = truncated.rfind(' ')
    if last_space > max_length * 0.7:  # Only use word boundary if we're not too far back
        return f"{truncated[:last_space]}..."
    return f"{truncated}..."


def _process_operation(operation: dict, method: str, path: str) -> None:
    """Process a single operation to add x-ai-description (modifies operation in-place)."""
    # Skip if already has x-ai-description (respect explicit definitions)
    if 'x-ai-description' in operation:
        logger.debug(f"x-ai-description already defined for {path} {method.upper()}. Respecting existing value.")
        return

    # Get operation ID
    operation_id = operation.get('operationId', '')

    # Check if the ViewSet has opted out of AI description generation
    if extract_operation_prefix(operation_id) in SKIP_AI_DESCRIPTION_PREFIXES:
        logger.debug(f'ViewSet at {path} {method.upper()} is intentionally not generating a description')
        return

    # Extract action, resource parts, and parent resource
    action, resource_parts, parent_resource = extract_action_and_resource(operation_id, path)

    # Try to generate description with priority order:
    # 1. resource_purpose field (if defined on ViewSet)
    # 2. Auto-generation from resource names and operation type

    # Priority 1: Check for resource_purpose
    resource_purpose, _ = _lookup_resource_purpose(operation_id)
    ai_description = _generate_from_resource_purpose(resource_purpose, action)

    # Priority 2: Fall back to auto-generation
    if ai_description is None:
        ai_description = _generate_description_auto(operation_id, action, resource_parts, parent_resource, path, operation)

    # Enforce character limit and add to operation
    operation['x-ai-description'] = _enforce_character_limit(ai_description)


def add_x_ai_description(result: dict, generator: Any, request: Any, public: Optional[bool]) -> dict:
    """
    Postprocessing hook for drf-spectacular that adds x-ai-description fields to all operations.

    This hook runs after OpenAPI schema generation and automatically generates x-ai-description
    fields for all API operations. These descriptions are used by MCP (Model Context Protocol)
    servers to provide better context to AI tools when selecting which API endpoints to call.

    The hook uses a three-tier priority system for generating descriptions:
    1. Explicit x-ai-description - If defined via @extend_schema, use as-is (highest priority)
    2. resource_purpose field - Generate from ViewSet's resource_purpose using action templates
    3. Auto-generation - Generate from resource names, operation types, and docstrings (fallback)

    ViewSets can opt out of automatic generation by setting skip_ai_description = True.
    ViewSets can provide custom purpose-based descriptions by defining a resource_purpose field.

    All generated descriptions are enforced to be under 300 characters (with 200 preferred).

    Example ViewSet configurations:

        # Opt out of AI description generation
        class MyViewSet(ModelViewSet):
            skip_ai_description = True

        # Provide purpose-based descriptions for CRUD operations
        class MyViewSet(ModelViewSet):
            resource_purpose = "audit trail entries for tracking system changes"

    Args:
        result: The generated OpenAPI schema dictionary
        generator: The SchemaGenerator instance
        request: The HTTP request (if available)
        public: Boolean indicating if this is for public schema

    Returns:
        The modified schema dictionary with x-ai-description fields added
    """
    paths = result.get('paths', {})

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            # Ensure we're only generating values for methods we support
            if method in HTTP_METHODS:
                _process_operation(operation, method, path)

    return result


def inject_oauth_endpoints(result: dict, generator: Any, request: Any, public: Optional[bool]) -> dict:
    """
    Inject OAuth 2.0 endpoint schemas into OpenAPI spec.

    OAuth endpoints (/o/authorize/, /o/token/, /o/revoke_token/) use Django views
    via django-oauth-toolkit and don't auto-generate schemas. This hook manually
    adds their documentation per RFC 6749 (OAuth 2.0) and RFC 7009 (Token Revocation).
    """
    # Common values for OAuth endpoints
    oauth_scopes = {'read': 'Read access to resources', 'write': 'Write access to resources (includes read)'}
    auth_tag = ['Authentication']
    no_auth = []

    # Added for SonarQube compliance
    token_url = '/o/token/'
    oauth_content_type = 'application/x-www-form-urlencoded'

    # Only inject if the root 'paths' key exists in the schema.
    if 'paths' not in result:
        return result

    # Update OAuth2_Authentication security scheme to use oauth2 type
    if 'components' in result and 'securitySchemes' in result['components']:
        if 'OAuth2_Authentication' in result['components']['securitySchemes']:
            result['components']['securitySchemes']['OAuth2_Authentication'] = {
                'type': 'oauth2',
                'description': 'OAuth 2.0 authentication with supported grant types: authorization_code and password',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': '/o/authorize/',
                        'tokenUrl': token_url,
                        'scopes': oauth_scopes,
                    },
                    'password': {
                        'tokenUrl': token_url,
                        'scopes': oauth_scopes,
                    },
                },
            }

    # /o/authorize/ - OAuth 2.0 Authorization Endpoint
    result['paths']['/o/authorize/'] = {
        'get': {
            'operationId': 'oauth2_authorize_retrieve',
            'description': (
                'OAuth 2.0 authorization endpoint for browser-based flows. '
                'Returns an HTML form for user consent. '
                'For programmatic access, use /api/gateway/v1/tokens/ to create Personal Access Tokens instead.'
            ),
            'x-ai-description': 'Browser-only OAuth authorization form. Not usable programmatically. Use /api/gateway/v1/tokens/ for API access instead.',
            'parameters': [
                {'name': 'client_id', 'in': 'query', 'required': True, 'schema': {'type': 'string'}, 'description': 'OAuth application client ID'},
                {
                    'name': 'response_type',
                    'in': 'query',
                    'required': True,
                    'schema': {'type': 'string', 'enum': ['code', 'token']},
                    'description': 'OAuth response type',
                },
                {
                    'name': 'redirect_uri',
                    'in': 'query',
                    'required': True,
                    'schema': {'type': 'string', 'format': 'uri'},
                    'description': 'Redirect URL after authorization',
                },
                {'name': 'scope', 'in': 'query', 'required': False, 'schema': {'type': 'string'}, 'description': 'Space-separated scopes (read write)'},
                {'name': 'state', 'in': 'query', 'required': False, 'schema': {'type': 'string'}, 'description': 'CSRF protection state'},
            ],
            'tags': auth_tag,
            'security': no_auth,
            'responses': {
                '200': {
                    'description': 'HTML authorization form',
                    'content': {'text/html': {'schema': {'type': 'string'}}},
                },
                '302': {
                    'description': 'Redirect after authorization',
                },
            },
        },
        'post': {
            'operationId': 'oauth2_authorize_create',
            'description': 'Process OAuth 2.0 authorization decision (user clicked Allow/Deny)',
            'x-ai-description': 'Browser-only OAuth authorization submission. Not usable programmatically. Requires user interaction.',
            'requestBody': {
                'content': {
                    oauth_content_type: {
                        'schema': {
                            'type': 'object',
                            'properties': {
                                'client_id': {'type': 'string'},
                                'redirect_uri': {'type': 'string', 'format': 'uri'},
                                'response_type': {'type': 'string'},
                                'scope': {'type': 'string'},
                                'state': {'type': 'string'},
                                'allow': {'type': 'string', 'description': 'User consent (on/off)'},
                            },
                            'required': ['client_id', 'redirect_uri', 'response_type'],
                        },
                    },
                },
            },
            'tags': auth_tag,
            'security': no_auth,
            'responses': {
                '302': {
                    'description': 'Redirect to redirect_uri with authorization code or error',
                },
            },
        },
    }

    # /o/token/ - OAuth 2.0 Token Endpoint
    result['paths'][token_url] = {
        'post': {
            'operationId': 'oauth2_token_create',
            'description': (
                'OAuth 2.0 token endpoint for programmatic token creation and refresh. Supports authorization_code, password, and refresh_token grant types.'
            ),
            'x-ai-description': (
                'Exchange OAuth credentials or refresh token for access token. Use refresh_token grant to renew expired tokens programmatically.'
            ),
            'requestBody': {
                'content': {
                    oauth_content_type: {
                        'schema': {
                            'type': 'object',
                            'properties': {
                                'grant_type': {
                                    'type': 'string',
                                    'enum': ['authorization_code', 'password', 'refresh_token'],
                                    'description': 'OAuth grant type',
                                },
                                'client_id': {'type': 'string', 'description': 'OAuth application client ID'},
                                'client_secret': {'type': 'string', 'description': 'OAuth application secret'},
                                'code': {'type': 'string', 'description': 'Authorization code (for authorization_code grant)'},
                                'redirect_uri': {'type': 'string', 'format': 'uri', 'description': 'Redirect URI (for authorization_code grant)'},
                                'username': {'type': 'string', 'description': 'Username (for password grant)'},
                                'password': {'type': 'string', 'description': 'Password (for password grant)'},
                                'refresh_token': {'type': 'string', 'description': 'Refresh token (for refresh_token grant)'},
                                'scope': {'type': 'string', 'description': 'Space-separated scopes (read write)'},
                            },
                            'required': ['grant_type'],
                        },
                    },
                },
            },
            'tags': auth_tag,
            'security': no_auth,  # OAuth protocol endpoint - accepts client/user credentials, not API auth tokens
            'responses': {
                '201': {
                    'description': 'Token created successfully',
                    'content': {
                        'application/json': {
                            'schema': {
                                'type': 'object',
                                'properties': {
                                    'access_token': {'type': 'string', 'description': 'OAuth access token'},
                                    'token_type': {'type': 'string', 'enum': ['Bearer'], 'description': 'Token type'},
                                    'expires_in': {'type': 'integer', 'description': 'Seconds until expiration'},
                                    'refresh_token': {'type': 'string', 'description': 'Refresh token (if applicable)'},
                                    'scope': {'type': 'string', 'description': 'Granted scopes'},
                                },
                                'required': ['access_token', 'token_type', 'expires_in'],
                            },
                        },
                    },
                },
                '400': {
                    'description': 'Invalid request',
                    'content': {
                        'application/json': {
                            'schema': {
                                'type': 'object',
                                'properties': {
                                    'error': {'type': 'string', 'description': 'OAuth error code'},
                                    'error_description': {'type': 'string', 'description': 'Human-readable error'},
                                },
                            },
                        },
                    },
                },
                '403': {
                    'description': 'Access denied or token expired',
                },
            },
        },
    }

    # /o/revoke_token/ - OAuth 2.0 Token Revocation
    result['paths']['/o/revoke_token/'] = {
        'post': {
            'operationId': 'oauth2_revoke_token_create',
            'description': 'OAuth 2.0 token revocation endpoint. Revokes access or refresh tokens.',
            'x-ai-description': 'Revoke an OAuth access or refresh token to invalidate it immediately. Use for cleanup or security purposes.',
            'requestBody': {
                'content': {
                    oauth_content_type: {
                        'schema': {
                            'type': 'object',
                            'properties': {
                                'token': {'type': 'string', 'description': 'Access or refresh token to revoke'},
                                'token_type_hint': {
                                    'type': 'string',
                                    'enum': ['access_token', 'refresh_token'],
                                    'description': 'Hint about token type (optional)',
                                },
                            },
                            'required': ['token'],
                        },
                    },
                },
            },
            'tags': auth_tag,
            'security': no_auth,
            'responses': {
                '200': {
                    'description': 'Token revoked successfully (empty response)',
                },
            },
        },
    }

    return result
