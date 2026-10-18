"""Safe, typed diagnostics. Never propagate upstream response bodies or secrets."""


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
