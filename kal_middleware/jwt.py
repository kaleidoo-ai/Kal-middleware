import json
from functools import wraps
from fastapi import Request, WebSocket, status
from fastapi.security import HTTPBearer
from starlette.responses import Response
import firebase_admin
from firebase_admin import auth
from typing import Callable, Optional, Any, Awaitable, Tuple, List
import os
from jose import jwt
from jose.exceptions import JWTError
import requests
import google.auth.transport.requests
import google.oauth2.id_token
from . import get_env_var
default_app = firebase_admin.initialize_app()

HTTP_REQUEST = google.auth.transport.requests.Request()
security = HTTPBearer()

def get_allowed_accounts() -> List[str]:
    allowed_accounts = get_env_var("ALLOWED_SERVICE_ACCOUNTS", "")
    return [acc.strip() for acc in allowed_accounts.split(",")]


def decode_firebase_token(token):
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token, decoded_token['uid'], None
    except Exception as e:
        return None, None, str(e)


def decode_keycloak_token(token):
    try:
        # Get Keycloak configuration from environment variables
        keycloak_url = os.getenv('KEYCLOAK_URL')
        realm_name = os.getenv('KEYCLOAK_REALM')
        client_id = os.getenv('KEYCLOAK_CLIENT_ID')

        if not all([keycloak_url, realm_name, client_id]):
            raise ValueError("Keycloak configuration is incomplete. Please check your environment variables.")

        # Construct the full URL for the Keycloak server's public key
        key_url = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/certs"

        # Fetch the public key
        response = requests.get(key_url)
        response.raise_for_status()
        keys = response.json()['keys']
        key_id = jwt.get_unverified_header(token)['kid']
        public_key = next((key for key in keys if key['kid'] == key_id), None)

        if not public_key:
            raise ValueError("Matching public key not found")

        # Decode and verify the token
        options = {
            'verify_signature': True,
            'verify_aud': True,
            'verify_exp': True
        }
        decoded_token = jwt.decode(token, public_key, algorithms=['RS256'], options=options, audience=client_id)
        return decoded_token, decoded_token['sub'], None
    except JWTError as e:
        return None, None, f"JWT decode error: {str(e)}"
    except requests.RequestException as e:
        return None, None, f"Failed to fetch public key: {str(e)}"
    except ValueError as e:
        return None, None, str(e)
    except Exception as e:
        return None, None, f"Unexpected error: {str(e)}"


def firebase_jwt_authenticated(
    get_user_by_fb_uid: Callable[[str], Any],
    get_capability: Callable[[str, str], Any],
    check_access: Optional[Callable[[dict, Any], Awaitable[Tuple[bool, dict]]]] = None,
):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def decorated_function(request: Request, *args, **kwargs):
            # verify the token exists and validate with firebase
            header = request.headers.get("Authorization", None)
            if header:
                token = header.split(" ")[1]
                try:
                    decoded_token = auth.verify_id_token(token)
                except Exception as e:
                    return Response(
                        status_code=status.HTTP_403_FORBIDDEN, content=f"Error with authentication: {e}"
                    )
            else:
                return Response(status_code=status.HTTP_401_UNAUTHORIZED, content="Error, token not found.")

            # verify that the service and action exists in the config map
            service = kwargs.get('service')
            action = kwargs.get('action')
            objects = {}

            # verify that the user has the permission to execute the request
            user_uid = decoded_token["uid"]
            user = await get_user_by_fb_uid(user_uid)

            if not user:
                return Response(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content="User not found"
                )
            capabilities = [capability.get("id") for capability in user.get("capabilities")]
            capability = await get_capability(service, action)
            access = capability and capability.get("id") in capabilities

            if not access:
                return Response(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content=f"The user cannot access {service}/{action}."
                )

            # if the request has body and there is a need to verify the user access to the elements - verify it
            if request.method in ["POST", "PUT"]:
                if check_access:
                    # Determine content type and parse accordingly
                    if request.headers.get('Content-Type') == 'application/json':
                        body = await request.json()
                    elif 'multipart/form-data' in request.headers.get('Content-Type'):
                        body = await request.form()
                        body = dict(body)
                    else:
                        return Response(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content=f"Headers not allowed"
                        )
                    access, objects  = await check_access(user, body)
                    if not access:
                        return Response(
                            status_code=status.HTTP_403_FORBIDDEN,
                            content=f"User not permitted to perform this action. reason: {objects}",
                        )

            request.state.user = user
            for key, value in objects.items():
                setattr(request.state, key, value)

            # Process the request
            response = await func(request, *args, **kwargs)
            return response

        return decorated_function

    return decorator


provider_function = {
    "firebase": decode_firebase_token,
    "keycloak": decode_keycloak_token
}

def authenticate(
    get_user_by_uid: Callable[[str], Any],
    get_capability: Callable[[str, str, str], Any],
    check_access: Optional[Callable[[dict, Any], Awaitable[Tuple[bool, dict]]]] = None,
    product_check: Optional[bool] = True
):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def decorated_function(request: Request, *args, **kwargs):
            # Determine which provider to use
            provider = os.getenv('PROVIDER', 'firebase').lower()

            # verify the token exists and validate with the appropriate provider
            header = request.headers.get("Authorization", None)
            if header:
                token = header.split(" ")[1]
                try:
                    if provider in provider_function.keys():
                        decoded_token, user_uid, error = provider_function[provider](token)
                        if error is not None:
                            return Response(
                                status_code=status.HTTP_403_FORBIDDEN,
                                content=f"Error with authentication: {error}"
                            )
                    else:
                        return Response(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            content=f"Invalid authentication provider configured: {provider}"
                        )
                except Exception as e:
                    return Response(
                        status_code=status.HTTP_403_FORBIDDEN, content=f"Error with authentication: {e}"
                    )
            else:
                return Response(status_code=status.HTTP_401_UNAUTHORIZED, content="Error, token not found.")

            # verify that the service and action exists in the config map
            service = kwargs.get('service')
            action = kwargs.get('action')
            objects = {}

            # verify that the user has the permission to execute the request
            user = await get_user_by_uid(user_uid)

            if not user:
                return Response(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content="User not found"
                )

            if request.headers.get('Content-Type') == 'application/json':
                body = await request.json()
            elif 'multipart/form-data' in request.headers.get('Content-Type'):
                body = await request.form()
                body = dict(body)
            else:
                return Response(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content=f"Headers not allowed"
                )

            if product_check:
                product = body.get("product")
                if product is None:
                    return Response(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content=f"Product type is missing from the body"
                    )
            else:
                product = "kalsense"

            capability = await get_capability(service, action, product)
            capabilities = [capability.get("id") for capability in user.get("capabilities").get(product, [])]
            access = capability and (capability.get("id") in capabilities)

            if not access:
                return Response(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content=f"The user cannot access {service}/{action} in {product}."
                )

            # if the request has body and there is a need to verify the user access to the elements - verify it
            if request.method in ["POST", "PUT"]:
                if check_access:
                    access, objects  = await check_access(user, body)
                    if not access:
                        return Response(
                            status_code=status.HTTP_403_FORBIDDEN,
                            content=f"User not permitted to perform this action. reason: {objects}",
                        )

            request.state.user = user
            for key, value in objects.items():
                setattr(request.state, key, value)

            # Process the request
            response = await func(request, *args, **kwargs)
            return response

        return decorated_function

    return decorator


def websocket_authenticate(
    get_user_by_uid: Callable[[str], Any],
    get_capability: Callable[[str, str, str], Any],
    check_access: Optional[Callable[[dict, Any], Awaitable[Tuple[bool, dict]]]] = None,
    product_check: Optional[bool] = True
):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def decorated_function(websocket: WebSocket, *args, **kwargs):
            # Determine the provider
            provider = os.getenv('PROVIDER', 'firebase').lower()

            # verify the token exists and validate with the appropriate provider
            header = websocket.headers.get("Authorization", None)
            if header:
                token = header.split(" ")[1]
                try:
                    if provider in provider_function.keys():
                        decoded_token, user_uid, error = provider_function[provider](token)
                        if error is not None:
                            await websocket.send_json(
                                {
                                    "status_code": status.HTTP_403_FORBIDDEN,
                                    "message": f"Error with authentication: {error}",
                                }
                            )
                            await websocket.close()
                            return
                    else:
                        await websocket.send_json(
                            {
                                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                                "message": f"Invalid authentication provider configured: {provider}",
                            }
                        )
                        await websocket.close()
                        return
                except Exception as e:
                    await websocket.send_json(
                        {
                            "status_code": status.HTTP_403_FORBIDDEN,
                            "message": f"Error with authentication: {e}",
                        }
                    )
                    await websocket.close()
                    return
            else:
                await websocket.send_json(
                    {
                        "status_code": status.HTTP_401_UNAUTHORIZED,
                        "message": "Error, token not found.",
                    }
                )
                await websocket.close()
                return

            # verify that the service and action exists in the config map
            service = kwargs.get('service')
            action = kwargs.get('action')
            objects = {}

            # verify that the user has the permission to execute the request
            user = await get_user_by_uid(user_uid)

            if not user:
                await websocket.send_json(
                    {
                        "status_code": status.HTTP_404_NOT_FOUND,
                        "message": "User not found",
                    }
                )
                await websocket.close()
                return

            try:
                # Receive the initial message from the WebSocket
                message = await websocket.receive_text()
                body = json.loads(message)

                if product_check:
                    product = body.get("product")
                    if product is None:
                        await websocket.send_json(
                            {
                                "status_code": status.HTTP_401_UNAUTHORIZED,
                                "message": "Product type is missing from the body",
                            }
                        )
                        await websocket.close()
                        return
                else:
                    product = "kalsense"

                capability = await get_capability(service, action, product)
                capabilities = [
                    cap.get("id")
                    for cap in user.get("capabilities").get(product, [])
                ]
                access = capability and (capability.get("id") in capabilities)

                if not access:
                    await websocket.send_json(
                        {
                            "status_code": status.HTTP_403_FORBIDDEN,
                            "message": f"The user cannot access {service}/{action} in {product}.",
                        }
                    )
                    await websocket.close()
                    return

                # Check additional access based on request body (if applicable)
                if check_access:
                    access, objects = await check_access(user, body)
                    if not access:
                        await websocket.send_json(
                            {
                                "status_code": status.HTTP_403_FORBIDDEN,
                                "message": f"User not permitted to perform this action. reason: {objects}",
                            }
                        )
                        await websocket.close()
                        return

                # Set user and any additional objects in websocket state (as attributes)
                websocket.state.user = user
                for key, value in objects.items():
                    setattr(websocket.state, key, value)

                # Proceed with the WebSocket handler
                return await func(websocket, *args, **kwargs)

            except Exception as e:
                await websocket.send_json(
                    {
                        "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "message": f"Error processing WebSocket request: {str(e)}",
                    }
                )
                await websocket.close()

        return decorated_function

    return decorator


