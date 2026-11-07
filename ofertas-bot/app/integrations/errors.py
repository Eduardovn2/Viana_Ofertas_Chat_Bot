"""Safe, typed diagnostics. Never propagate upstream response bodies or secrets."""

import math


def http_diagnostics(error):
    """Only typed transport measurements; never exception strings/requests/headers."""
    data = getattr(error, "http_diagnostics", {})
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, allowed in {
        "timeout_phase": {"CONNECT", "READ", "WRITE", "POOL", "UNKNOWN"},
        "error_type": {
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "TimeoutException",
            "ConnectError",
            "ReadError",
            "WriteError",
            "NetworkError",
            "RequestError",
        },
    }.items():
        if isinstance(data.get(key), str) and data[key] in allowed:
            result[key] = data[key]
    for key in ("elapsed_ms", "timeout_connect", "timeout_read", "timeout_write", "timeout_pool"):
        value = data.get(key)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0:
            result[key] = value
    return result


class IntegrationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class UnsupportedCapability(IntegrationError):
    def __init__(self, message: str = "Capacidade não disponível nesta conta/API."):
        super().__init__("UNSUPPORTED_CAPABILITY", message)


class ConnectorDisabled(IntegrationError):
    def __init__(self, message: str = "Conector desativado ou sem credenciais configuradas."):
        super().__init__("CONNECTOR_DISABLED", message)


class InvalidPayload(IntegrationError):
    def __init__(self, message: str = "Resposta oficial incompatível com o contrato esperado."):
        super().__init__("INVALID_PAYLOAD", message)


class AuthorizationDenied(IntegrationError):
    def __init__(self, message: str = "Credenciais ou permissão recusadas pelo provedor."):
        super().__init__("AUTHORIZATION_DENIED", message)
