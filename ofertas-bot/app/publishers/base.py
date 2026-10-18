"""Contrato reservado para uma integração WhatsApp autorizada no futuro."""

from abc import ABC, abstractmethod


class WhatsAppPublisher(ABC):
    """Interface sem implementação de transporte ou automação de aplicativos."""

    @abstractmethod
    def publish(self, *, destination_id: str, message: str) -> str:
        """Envie uma mensagem e retorne seu identificador no serviço autorizado."""
        raise NotImplementedError
